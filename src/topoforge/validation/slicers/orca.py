"""OrcaSlicer headless CLI adapter."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from topoforge.validation.slicers.base import (
    BaseSlicerAdapter,
    SliceResult,
    SlicerProfile,
    SliceStatus,
    parse_gcode_metrics,
)


class OrcaSlicerAdapter(BaseSlicerAdapter):
    """Slice models with OrcaSlicer's ``--slice`` batch interface.

    OrcaSlicer accepts an output directory rather than an exact G-code file.
    The adapter slices into an isolated temporary directory, verifies
    ``result.json``, and atomically moves the selected plate to the exact path
    requested by TopoForge.
    """

    display_name = "OrcaSlicer"
    executable_candidates = ("orca-slicer", "OrcaSlicer", "orcaslicer")
    environment_keys = ("TOPOFORGE_ORCA_SLICER", "ORCA_SLICER")

    def _version_from_output(self, output: str) -> str | None:
        if self.executable is not None:
            filename_match = re.search(
                r"(?:^|[_-])V?([0-9]+\.[0-9]+(?:\.[0-9]+)?)(?:[_.-]|$)",
                self.executable.name,
                flags=re.IGNORECASE,
            )
            if filename_match is not None:
                return filename_match.group(1)
        banner_match = re.search(r"OrcaSlicer-([0-9][0-9.]*)", output, re.IGNORECASE)
        return None if banner_match is None else banner_match.group(1).rstrip(".")

    def _execution_environment(self) -> Mapping[str, str] | None:
        if self.executable is None or self.executable.suffix.casefold() != ".appimage":
            return None
        environment = dict(os.environ)
        environment["APPIMAGE_EXTRACT_AND_RUN"] = "1"
        return environment

    def slice(
        self,
        input_model: Path,
        output_gcode: Path,
        *,
        profile: SlicerProfile | None = None,
        extra_args: Sequence[str] = (),
        timeout_seconds: float = 600.0,
    ) -> SliceResult:
        """Slice one STL/3MF and publish a verified plate G-code file."""
        resolved_profile = profile or SlicerProfile()
        input_model = input_model.expanduser().resolve()
        output_gcode = output_gcode.expanduser().resolve()
        preflight = self._preflight_failure(input_model, output_gcode, resolved_profile)
        if preflight is not None:
            return preflight
        info = self.probe()
        if self.executable is None:
            return self._failure_result(
                info,
                input_model,
                output_gcode,
                resolved_profile,
                error="OrcaSlicer executable disappeared after probing",
            )

        output_gcode.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".topoforge-orca-", dir=output_gcode.parent
        ) as temporary_name:
            temporary_dir = Path(temporary_name)
            output_dir = temporary_dir / "output"
            data_dir = temporary_dir / "data"
            output_dir.mkdir()
            data_dir.mkdir()
            command = self._slice_command(
                input_model,
                output_dir,
                data_dir,
                resolved_profile,
                extra_args,
            )
            execution = self._runner(
                command,
                timeout_seconds=timeout_seconds,
                env=self._execution_environment(),
            )
            metadata, metadata_error = _load_result_json(output_dir / "result.json")
            diagnostics = _diagnostics(execution.stdout, execution.stderr, metadata)
            generated_paths = sorted(output_dir.glob("*.gcode"))
            effective_code = _metadata_return_code(metadata, execution.returncode)
            reported_error = _metadata_error(metadata)
            if (
                metadata_error is not None
                or reported_error is not None
                or effective_code != 0
                or not generated_paths
            ):
                reason = metadata_error or reported_error
                if reason is None:
                    reason = (
                        f"OrcaSlicer exited {effective_code}"
                        if effective_code != 0
                        else "OrcaSlicer did not generate non-empty G-code"
                    )
                return self._failure_result(
                    info,
                    input_model,
                    output_gcode,
                    resolved_profile,
                    error=reason,
                    command=command,
                    execution=execution,
                    metrics=parse_gcode_metrics("", diagnostics=diagnostics),
                )

            generated_path = _select_plate(generated_paths)
            if generated_path.stat().st_size == 0:
                return self._failure_result(
                    info,
                    input_model,
                    output_gcode,
                    resolved_profile,
                    error="OrcaSlicer generated an empty G-code file",
                    command=command,
                    execution=execution,
                )
            gcode_text = generated_path.read_text(encoding="utf-8", errors="replace")
            metrics = parse_gcode_metrics(gcode_text, diagnostics=diagnostics)
            generated_path.replace(output_gcode)

        updated_info = self._info_from_gcode(info, gcode_text)
        self._probe_cache = updated_info
        return SliceResult(
            status=SliceStatus.SUCCEEDED,
            slicer=updated_info,
            profile=resolved_profile.label,
            input_model=input_model,
            output_gcode=output_gcode,
            command=tuple(command),
            exit_code=effective_code,
            duration_seconds=execution.duration_seconds,
            stdout=execution.stdout,
            stderr=execution.stderr,
            gcode_generated=True,
            gcode_size_bytes=output_gcode.stat().st_size,
            metrics=metrics,
        )

    def _slice_command(
        self,
        input_model: Path,
        output_dir: Path,
        data_dir: Path,
        profile: SlicerProfile,
        extra_args: Sequence[str],
    ) -> list[str]:
        if self.executable is None:
            return []
        command = [
            str(self.executable),
            "--debug",
            "2",
            "--datadir",
            str(data_dir),
        ]
        if profile.settings:
            command.extend(
                ("--load-settings", ";".join(str(path.resolve()) for path in profile.settings))
            )
        if profile.filaments:
            command.extend(
                ("--load-filaments", ";".join(str(path.resolve()) for path in profile.filaments))
            )
        command.extend(("--slice", "0", "--outputdir", str(output_dir)))
        command.extend(str(argument) for argument in extra_args)
        command.append(str(input_model))
        return command


def _load_result_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"OrcaSlicer result.json is unreadable: {exc}"
    if not isinstance(value, dict):
        return None, "OrcaSlicer result.json must contain a JSON object"
    return value, None


def _metadata_return_code(metadata: dict[str, Any] | None, process_code: int) -> int:
    if process_code != 0:
        return process_code
    if metadata is None:
        return process_code
    value = metadata.get("return_code")
    return value if isinstance(value, int) else process_code


def _metadata_error(metadata: dict[str, Any] | None) -> str | None:
    if metadata is None:
        return None
    value = metadata.get("error_string")
    if (
        isinstance(value, str)
        and value.strip()
        and value.strip().casefold() not in {"success", "success."}
    ):
        return value.strip()
    return None


def _diagnostics(stdout: str, stderr: str, metadata: dict[str, Any] | None) -> str:
    parts = [stdout, stderr]
    if metadata is not None:
        plates = metadata.get("sliced_plates")
        if isinstance(plates, list):
            for plate in plates:
                if not isinstance(plate, dict):
                    continue
                warning = plate.get("warning_message")
                if isinstance(warning, str) and warning.strip():
                    parts.append(f"WARNING: {warning.strip()}")
    return "\n".join(part for part in parts if part)


def _select_plate(paths: Sequence[Path]) -> Path:
    for path in paths:
        if path.name == "plate_1.gcode":
            return path
    return paths[0]
