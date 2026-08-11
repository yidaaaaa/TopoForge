"""Official Bambu Studio headless CLI adapter."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from topoforge.validation.slicers._bambu_windows import discover_windows_bambu_studio
from topoforge.validation.slicers.base import CommandRunner, SlicerProfile, run_command
from topoforge.validation.slicers.orca import OrcaSlicerAdapter


def parse_bambu_studio_version(output: str) -> str | None:
    """Return the Bambu Studio version parsed from a literal CLI banner."""
    match = re.search(r"BambuStudio-([0-9][0-9.]*)", output, re.IGNORECASE)
    return None if match is None else match.group(1).rstrip(".")


def macos_bambu_executable_candidates(
    *,
    home: Path | None = None,
    applications_root: Path = Path("/Applications"),
) -> tuple[Path, ...]:
    """Return standard official Bambu Studio executable locations on macOS."""
    resolved_home = Path.home() if home is None else home
    bundle_executable = Path("BambuStudio.app/Contents/MacOS/BambuStudio")
    return (
        applications_root / bundle_executable,
        resolved_home / "Applications" / bundle_executable,
    )


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
        platform_name: str | None = None,
        home: Path | None = None,
        applications_root: Path = Path("/Applications"),
    ) -> None:
        """Resolve explicit/env settings before standard platform installations."""
        resolved_platform = sys.platform if platform_name is None else platform_name
        has_environment_override = any(os.environ.get(key) for key in self.environment_keys)
        if executable is None and resolved_platform == "darwin" and not has_environment_override:
            executable = next(
                (
                    candidate
                    for candidate in macos_bambu_executable_candidates(
                        home=home,
                        applications_root=applications_root,
                    )
                    if candidate.is_file() and os.access(candidate, os.X_OK)
                ),
                None,
            )
        super().__init__(executable, runner=runner)
        configured_override = executable is not None or has_environment_override
        if self.executable is not None or configured_override:
            return
        discovered = discover_windows_bambu_studio()
        if discovered is not None:
            self.executable = discovered
            self._resolution_detail = None

    def _version_from_output(self, output: str) -> str | None:
        return parse_bambu_studio_version(output)

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
