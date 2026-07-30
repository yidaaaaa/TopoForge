"""PrusaSlicer headless CLI adapter."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

from topoforge.validation.slicers.base import (
    BaseSlicerAdapter,
    SliceResult,
    SlicerProfile,
    SliceStatus,
    parse_gcode_metrics,
)


class PrusaSlicerAdapter(BaseSlicerAdapter):
    """Slice models with PrusaSlicer's ``--export-gcode`` batch action."""

    display_name = "PrusaSlicer"
    executable_candidates = ("prusa-slicer", "PrusaSlicer", "prusaslicer")
    environment_keys = ("TOPOFORGE_PRUSA_SLICER", "PRUSA_SLICER")

    def _version_from_output(self, output: str) -> str | None:
        match = re.search(
            r"PrusaSlicer(?:-|\s+)([0-9][0-9A-Za-z.+_-]*)",
            output,
            flags=re.IGNORECASE,
        )
        return None if match is None else match.group(1)

    def slice(
        self,
        input_model: Path,
        output_gcode: Path,
        *,
        profile: SlicerProfile | None = None,
        extra_args: Sequence[str] = (),
        timeout_seconds: float = 600.0,
    ) -> SliceResult:
        """Slice one STL/3MF and atomically publish explicit output G-code."""
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
                error="PrusaSlicer executable disappeared after probing",
            )

        output_gcode.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = _temporary_gcode_path(output_gcode)
        command: list[str] = [
            str(self.executable),
            "--export-gcode",
            "--output",
            str(temporary_path),
        ]
        for config_path in (*resolved_profile.settings, *resolved_profile.filaments):
            command.extend(("--load", str(config_path.resolve())))
        command.extend(str(argument) for argument in extra_args)
        command.append(str(input_model))

        execution = self._runner(
            command,
            timeout_seconds=timeout_seconds,
            env=self._execution_environment(),
        )
        generated = temporary_path.is_file() and temporary_path.stat().st_size > 0
        if execution.returncode != 0 or not generated:
            temporary_path.unlink(missing_ok=True)
            reason = (
                f"PrusaSlicer exited {execution.returncode}"
                if execution.returncode != 0
                else "PrusaSlicer did not generate non-empty G-code"
            )
            return self._failure_result(
                info,
                input_model,
                output_gcode,
                resolved_profile,
                error=reason,
                command=command,
                execution=execution,
                metrics=parse_gcode_metrics(
                    "", diagnostics="\n".join((execution.stdout, execution.stderr))
                ),
            )

        gcode_text = temporary_path.read_text(encoding="utf-8", errors="replace")
        metrics = parse_gcode_metrics(
            gcode_text,
            diagnostics="\n".join((execution.stdout, execution.stderr)),
        )
        temporary_path.replace(output_gcode)
        updated_info = self._info_from_gcode(info, gcode_text)
        self._probe_cache = updated_info
        return SliceResult(
            status=SliceStatus.SUCCEEDED,
            slicer=updated_info,
            profile=resolved_profile.label,
            input_model=input_model,
            output_gcode=output_gcode,
            command=tuple(command),
            exit_code=execution.returncode,
            duration_seconds=execution.duration_seconds,
            stdout=execution.stdout,
            stderr=execution.stderr,
            gcode_generated=True,
            gcode_size_bytes=output_gcode.stat().st_size,
            metrics=metrics,
        )


def _temporary_gcode_path(output_gcode: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output_gcode.stem}-",
        suffix=".gcode",
        dir=output_gcode.parent,
    )
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path
