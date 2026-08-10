"""Official Bambu Studio headless CLI adapter."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from topoforge.validation.slicers._bambu_windows import discover_windows_bambu_studio
from topoforge.validation.slicers.base import CommandRunner, SlicerProfile, run_command
from topoforge.validation.slicers.orca import OrcaSlicerAdapter


class BambuStudioAdapter(OrcaSlicerAdapter):
    """Slice models with Bambu Lab's official Bambu Studio batch interface."""

    display_name = "BambuStudio"
    executable_candidates = ("bambu-studio", "BambuStudio")
    environment_keys = ("TOPOFORGE_BAMBU_STUDIO", "BAMBU_STUDIO")

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        runner: CommandRunner = run_command,
    ) -> None:
        super().__init__(executable, runner=runner)
        configured_override = executable is not None or any(
            os.environ.get(key) for key in self.environment_keys
        )
        if self.executable is not None or configured_override:
            return
        discovered = discover_windows_bambu_studio()
        if discovered is not None:
            self.executable = discovered
            self._resolution_detail = None

    def _version_from_output(self, output: str) -> str | None:
        match = re.search(r"BambuStudio-([0-9][0-9.]*)", output, re.IGNORECASE)
        return None if match is None else match.group(1).rstrip(".")

    def _slice_command(
        self,
        input_model: Path,
        output_dir: Path,
        data_dir: Path,
        profile: SlicerProfile,
        extra_args: Sequence[str],
    ) -> list[str]:
        del data_dir
        if self.executable is None:
            return []
        command = [str(self.executable), "--debug", "2"]
        if profile.settings:
            command.extend(
                ("--load-settings", ";".join(str(path.resolve()) for path in profile.settings))
            )
        if profile.filaments:
            command.extend(
                ("--load-filaments", ";".join(str(path.resolve()) for path in profile.filaments))
            )
        command.extend(
            (
                "--load-defaultfila",
                "--curr-bed-type",
                "Textured PEI Plate",
                "--normative-check",
                "--ensure-on-bed",
                "--arrange",
                "1",
            )
        )
        command.extend(("--slice", "0", "--outputdir", str(output_dir)))
        command.extend(str(argument) for argument in extra_args)
        command.append(str(input_model))
        return command
