"""Typed contracts and shared parsers for external FFF slicers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class SlicerAvailability(StrEnum):
    """Result of probing one slicer executable."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class SliceStatus(StrEnum):
    """Terminal state of a requested slice."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class SlicerInfo(BaseModel):
    """Discovered slicer identity and availability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str | None = None
    executable: Path | None = None
    status: SlicerAvailability
    detail: str | None = None


class SlicerProfile(BaseModel):
    """External slicer configuration files used for one invocation.

    ``settings`` holds process and machine configuration files. ``filaments``
    is separate because OrcaSlicer exposes a distinct CLI option for it.
    PrusaSlicer loads both collections through repeated ``--load`` options.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    settings: tuple[Path, ...] = ()
    filaments: tuple[Path, ...] = ()

    @property
    def label(self) -> str:
        """Return a stable human-readable profile label."""
        if self.name:
            return self.name
        paths = (*self.settings, *self.filaments)
        return "; ".join(path.name for path in paths) if paths else "slicer defaults"


class SliceMetrics(BaseModel):
    """Useful statistics and warnings parsed from slicer output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    estimated_time_seconds: int | None = Field(default=None, ge=0)
    estimated_time_text: str | None = None
    filament_used_mm: float | None = Field(default=None, ge=0)
    filament_used_cm3: float | None = Field(default=None, ge=0)
    filament_used_g: float | None = Field(default=None, ge=0)
    layer_count: int | None = Field(default=None, ge=0)
    support_material: bool | None = None
    out_of_bed: bool = False
    empty_layer_warning: bool = False
    floating_region_warning: bool = False
    warnings: tuple[str, ...] = ()


class SliceResult(BaseModel):
    """Complete, serializable result of one slicer invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SliceStatus
    slicer: SlicerInfo
    profile: str
    input_model: Path
    output_gcode: Path
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    duration_seconds: float = Field(default=0.0, ge=0)
    stdout: str = ""
    stderr: str = ""
    gcode_generated: bool = False
    gcode_size_bytes: int | None = Field(default=None, ge=0)
    metrics: SliceMetrics = Field(default_factory=SliceMetrics)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CommandExecution:
    """Normalized subprocess result used by production and test runners."""

    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


class CommandRunner(Protocol):
    """Callable boundary around subprocess execution."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution: ...


class SlicerAdapter(Protocol):
    """Public adapter interface shared by OrcaSlicer and PrusaSlicer."""

    @property
    def name(self) -> str: ...

    def probe(self, *, refresh: bool = False) -> SlicerInfo: ...

    def slice(
        self,
        input_model: Path,
        output_gcode: Path,
        *,
        profile: SlicerProfile | None = None,
        extra_args: Sequence[str] = (),
        timeout_seconds: float = 600.0,
    ) -> SliceResult: ...


def run_command(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> CommandExecution:
    """Run a slicer command without a shell and normalize failures."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=None if env is None else dict(env),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandExecution(
            returncode=124,
            stdout=_stream_text(exc.stdout),
            stderr=_stream_text(exc.stderr) + f"\nTimed out after {timeout_seconds:g} seconds.",
            duration_seconds=time.monotonic() - started,
        )
    except OSError as exc:
        return CommandExecution(
            returncode=127,
            stdout="",
            stderr=str(exc),
            duration_seconds=time.monotonic() - started,
        )
    return CommandExecution(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_seconds=time.monotonic() - started,
    )


def resolve_executable(
    explicit: str | Path | None,
    *,
    environment_keys: Sequence[str],
    candidates: Sequence[str],
) -> tuple[Path | None, str | None]:
    """Resolve an explicit, environment-provided, or PATH slicer executable."""
    configured: str | Path | None = explicit
    source = "explicit path"
    if configured is None:
        for key in environment_keys:
            value = os.environ.get(key)
            if value:
                configured = value
                source = key
                break

    if configured is not None:
        configured_text = os.fspath(configured)
        if os.sep not in configured_text:
            found = shutil.which(configured_text)
            if found:
                return Path(found).resolve(), None
        path = Path(configured_text).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve(), None
        return path, f"{source} does not identify an executable file: {path}"

    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return Path(found).resolve(), None
    return None, f"none of these executables were found on PATH: {', '.join(candidates)}"


class BaseSlicerAdapter(ABC):
    """Shared executable discovery, probing, and preflight behavior."""

    display_name: str
    executable_candidates: tuple[str, ...]
    environment_keys: tuple[str, ...]

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        runner: CommandRunner = run_command,
    ) -> None:
        self.executable, self._resolution_detail = resolve_executable(
            executable,
            environment_keys=self.environment_keys,
            candidates=self.executable_candidates,
        )
        self._runner = runner
        self._probe_cache: SlicerInfo | None = None

    @property
    def name(self) -> str:
        """Return the slicer's display name."""
        return self.display_name

    def probe(self, *, refresh: bool = False) -> SlicerInfo:
        """Discover the executable and parse its CLI version banner."""
        if self._probe_cache is not None and not refresh:
            return self._probe_cache
        if self.executable is None or self._resolution_detail is not None:
            info = SlicerInfo(
                name=self.name,
                executable=self.executable,
                status=SlicerAvailability.UNAVAILABLE,
                detail=self._resolution_detail,
            )
            self._probe_cache = info
            return info

        execution = self._runner(
            self._probe_command(),
            timeout_seconds=30.0,
            env=self._execution_environment(),
        )
        combined = _combine_output(execution.stdout, execution.stderr)
        version = self._version_from_output(combined)
        if execution.returncode == 0:
            status = SlicerAvailability.AVAILABLE
            detail = None
        else:
            status = SlicerAvailability.FAILED
            detail = _last_nonempty_line(combined) or f"version probe exited {execution.returncode}"
        info = SlicerInfo(
            name=self.name,
            version=version,
            executable=self.executable,
            status=status,
            detail=detail,
        )
        self._probe_cache = info
        return info

    def _probe_command(self) -> tuple[str, ...]:
        if self.executable is None:
            return ()
        return (str(self.executable), "--help")

    def _execution_environment(self) -> Mapping[str, str] | None:
        return None

    @abstractmethod
    def _version_from_output(self, output: str) -> str | None:
        """Parse a version from CLI output."""

    @abstractmethod
    def slice(
        self,
        input_model: Path,
        output_gcode: Path,
        *,
        profile: SlicerProfile | None = None,
        extra_args: Sequence[str] = (),
        timeout_seconds: float = 600.0,
    ) -> SliceResult:
        """Slice one model and place G-code at the exact requested path."""

    def _preflight_failure(
        self,
        input_model: Path,
        output_gcode: Path,
        profile: SlicerProfile,
    ) -> SliceResult | None:
        info = self.probe()
        if info.status is not SlicerAvailability.AVAILABLE:
            status = (
                SliceStatus.UNAVAILABLE
                if info.status is SlicerAvailability.UNAVAILABLE
                else SliceStatus.FAILED
            )
            return SliceResult(
                status=status,
                slicer=info,
                profile=profile.label,
                input_model=input_model,
                output_gcode=output_gcode,
                error=info.detail or f"{self.name} is not ready",
            )
        if not input_model.is_file():
            return self._failure_result(
                info,
                input_model,
                output_gcode,
                profile,
                error=f"input model does not exist: {input_model}",
            )
        missing_profiles = [
            path for path in (*profile.settings, *profile.filaments) if not path.is_file()
        ]
        if missing_profiles:
            missing = ", ".join(str(path) for path in missing_profiles)
            return self._failure_result(
                info,
                input_model,
                output_gcode,
                profile,
                error=f"slicer profile file does not exist: {missing}",
            )
        return None

    def _failure_result(
        self,
        info: SlicerInfo,
        input_model: Path,
        output_gcode: Path,
        profile: SlicerProfile,
        *,
        error: str,
        command: Sequence[str] = (),
        execution: CommandExecution | None = None,
        metrics: SliceMetrics | None = None,
    ) -> SliceResult:
        return SliceResult(
            status=SliceStatus.FAILED,
            slicer=info,
            profile=profile.label,
            input_model=input_model,
            output_gcode=output_gcode,
            command=tuple(command),
            exit_code=None if execution is None else execution.returncode,
            duration_seconds=0.0 if execution is None else execution.duration_seconds,
            stdout="" if execution is None else execution.stdout,
            stderr="" if execution is None else execution.stderr,
            metrics=SliceMetrics() if metrics is None else metrics,
            error=error,
        )

    def _info_from_gcode(self, info: SlicerInfo, gcode_text: str) -> SlicerInfo:
        generator = parse_gcode_generator(gcode_text)
        if generator is None:
            return info
        generator_name, generator_version = generator
        if (
            self.name.casefold() not in generator_name.casefold()
            and generator_name.casefold() not in self.name.casefold()
        ):
            return info
        return info.model_copy(update={"version": generator_version})


_FLOAT = r"([0-9]+(?:\.[0-9]+)?)"


def parse_gcode_generator(gcode_text: str) -> tuple[str, str] | None:
    """Return slicer name and version from a generated-by G-code comment."""
    match = re.search(
        r"^;\s*generated by\s+([A-Za-z][A-Za-z ]*?Slicer)\s+([^\s;]+)",
        gcode_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if match is None:
        return None
    return match.group(1).strip(), match.group(2).strip()


def parse_gcode_metrics(gcode_text: str, diagnostics: str = "") -> SliceMetrics:
    """Parse metrics emitted by OrcaSlicer, PrusaSlicer, and close forks."""
    estimated_time_text = _first_group(
        gcode_text,
        (
            r"^;\s*estimated printing time(?:\s*\([^)]*\))?\s*=\s*([^\r\n]+)",
            r"^;\s*(?:model|total) printing time\s*[:=]\s*([^\r\n]+)",
        ),
    )
    filament_used_mm = _float_metric(gcode_text, rf"^;\s*filament used \[mm\]\s*=\s*{_FLOAT}")
    filament_used_cm3 = _float_metric(gcode_text, rf"^;\s*filament used \[cm3\]\s*=\s*{_FLOAT}")
    filament_used_g = _float_metric(
        gcode_text, rf"^;\s*(?:total )?filament used \[g\]\s*=\s*{_FLOAT}"
    )

    layer_text = _first_group(
        gcode_text,
        (
            r"^;\s*total layers? count\s*=\s*([0-9]+)",
            r"^;\s*total layer number\s*:\s*([0-9]+)",
            r"^;\s*total_layer_count\s*=\s*([0-9]+)",
        ),
    )
    layer_count = int(layer_text) if layer_text is not None else None
    if layer_count is None:
        counted_layers = len(re.findall(r"^;LAYER_CHANGE\s*$", gcode_text, re.MULTILINE))
        layer_count = counted_layers or None

    support_paths = re.search(
        r"^;TYPE:(?:Support|Support material|Support interface)",
        gcode_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    support_setting = _first_group(
        gcode_text,
        (
            r"^;\s*enable_support\s*=\s*([^\r\n]+)",
            r"^;\s*support_material\s*=\s*([^\r\n]+)",
        ),
    )
    support_material: bool | None
    if support_paths is not None:
        support_material = True
    elif support_setting is not None:
        support_material = support_setting.strip().casefold() in {"1", "true", "yes", "on"}
    else:
        support_material = None

    warning_comments = "\n".join(
        line for line in gcode_text.splitlines() if line.lstrip().upper().startswith("; WARNING")
    )
    warning_source = _combine_output(diagnostics, warning_comments)
    lowered = warning_source.casefold()
    warnings = _extract_warning_lines(warning_source)
    return SliceMetrics(
        estimated_time_seconds=_duration_seconds(estimated_time_text),
        estimated_time_text=estimated_time_text,
        filament_used_mm=filament_used_mm,
        filament_used_cm3=filament_used_cm3,
        filament_used_g=filament_used_g,
        layer_count=layer_count,
        support_material=support_material,
        out_of_bed=any(
            phrase in lowered
            for phrase in (
                "outside of the print volume",
                "outside the print area",
                "outside of the print area",
                "out of bed",
                "does not fit in the print volume",
            )
        ),
        empty_layer_warning="empty layer" in lowered,
        floating_region_warning=any(
            phrase in lowered
            for phrase in ("floating region", "floating object", "not connected to the bed")
        ),
        warnings=warnings,
    )


def _duration_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    colon_match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", value)
    if colon_match is not None:
        hours = int(colon_match.group(1) or 0)
        return hours * 3600 + int(colon_match.group(2)) * 60 + int(colon_match.group(3))
    units = {unit: int(number) for number, unit in re.findall(r"(\d+)\s*([dhms])", value)}
    if not units:
        return None
    return (
        units.get("d", 0) * 86400
        + units.get("h", 0) * 3600
        + units.get("m", 0) * 60
        + units.get("s", 0)
    )


def _first_group(text: str, patterns: Sequence[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match is not None:
            return match.group(1).strip()
    return None


def _float_metric(text: str, pattern: str) -> float | None:
    value = _first_group(text, (pattern,))
    return None if value is None else float(value)


def _extract_warning_lines(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.casefold()
        if not stripped:
            continue
        if (
            "[warning]" in lowered
            or " warning:" in lowered
            or lowered.startswith("warning:")
            or lowered.startswith("; warning")
            or "[error]" in lowered
            or any(
                phrase in lowered
                for phrase in (
                    "outside of the print volume",
                    "outside the print area",
                    "empty layer",
                    "floating region",
                    "floating object",
                    "not connected to the bed",
                )
            )
        ):
            normalized = re.sub(r"^\[[^]]+\]\s*\[[^]]+\]\s*", "", stripped)
            if normalized not in found:
                found.append(normalized)
    return tuple(found)


def _stream_text(stream: bytes | str | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode(errors="replace")
    return stream


def _combine_output(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def _last_nonempty_line(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else None
