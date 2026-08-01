"""Actual per-tile slicer execution and checksummed manufacturing evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.exceptions import ConfigurationError, SlicerError
from topoforge.exporters.three_mf import inspect_3mf
from topoforge.tiling.connectors import (
    ConnectorPlan,
    PrintTileArtifactManifest,
    PrintTileAssemblyManifest,
    verify_print_tile_set,
)
from topoforge.validation.manufacturing import evaluate_bambu_p2s_release_gate
from topoforge.validation.slicers import (
    SliceMetrics,
    SlicerAdapter,
    SliceResult,
    SlicerInfo,
    SlicerProfile,
    SliceStatus,
    parse_gcode_metrics,
)

_SLICE_TILE_SCHEMA_VERSION = "topoforge-print-tile-slice-v1"
_SLICE_ASSEMBLY_SCHEMA_VERSION = "topoforge-print-tile-slice-assembly-v1"
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SliceProfileFile(BaseModel):
    """One exact settings or filament file copied into the slice evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    index: int = Field(ge=0)
    path: str
    sha256: Sha256Hex


class PrintTileSliceReport(BaseModel):
    """Literal slicer result and reopened checks for one print-local tile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _SLICE_TILE_SCHEMA_VERSION
    tile_id: str
    source_print_tile_manifest_sha256: Sha256Hex
    source_print_local_3mf_path: str
    source_print_local_3mf_sha256: Sha256Hex
    gcode_path: str
    gcode_sha256: Sha256Hex
    gcode_size_bytes: int = Field(gt=0)
    input_strict_3mf_warning_count: int = Field(ge=0)
    slicer_result: SliceResult
    reopened_metrics: SliceMetrics
    support_policy: str = "support material is forbidden for verified bottom-open connectors"
    exit_code_zero: bool
    gcode_generated: bool
    metrics_reopen_match: bool
    layer_count_positive: bool
    out_of_bed: bool
    empty_layer_warning: bool
    floating_region_warning: bool
    support_material: bool | None
    release_role: str
    manufacturing_release_gate: dict[str, Any] | None = None
    official_p2s_release_gate_passed: bool
    required_checks_passed: bool


class PrintTileSliceRecord(BaseModel):
    """Root checksummed record for one tile G-code and report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tile_id: str
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    directory: str
    report_path: str
    report_sha256: Sha256Hex
    gcode_path: str
    gcode_sha256: Sha256Hex
    source_print_tile_manifest_sha256: Sha256Hex
    source_print_local_3mf_sha256: Sha256Hex
    layer_count: int = Field(gt=0)
    estimated_time_seconds: int | None = Field(default=None, ge=0)
    filament_used_mm: float | None = Field(default=None, ge=0)
    filament_used_cm3: float | None = Field(default=None, ge=0)
    filament_used_g: float | None = Field(default=None, ge=0)
    required_checks_passed: bool


class PrintTileSliceManifest(BaseModel):
    """Aggregate source-bound evidence that every print-local tile was sliced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _SLICE_ASSEMBLY_SCHEMA_VERSION
    layout_id: str
    source_print_tile_assembly_sha256: Sha256Hex
    source_connector_plan_sha256: Sha256Hex
    slicer: SlicerInfo
    slicer_executable_sha256: Sha256Hex | None = None
    profile_name: str
    profile_files: tuple[SliceProfileFile, ...]
    printer_profile_id: str
    release_role: str
    official_p2s_release_gate_passed: bool
    all_parameter_checks_passed: bool
    tile_grid_shape: tuple[int, int]
    tile_count: int = Field(gt=0)
    total_gcode_size_bytes: int = Field(gt=0)
    total_estimated_time_seconds: int | None = Field(default=None, ge=0)
    total_filament_used_mm: float | None = Field(default=None, ge=0)
    total_filament_used_cm3: float | None = Field(default=None, ge=0)
    total_filament_used_g: float | None = Field(default=None, ge=0)
    maximum_layer_count: int = Field(gt=0)
    all_exit_codes_zero: bool
    no_out_of_bed: bool
    no_empty_layers: bool
    no_floating_regions: bool
    no_support_material: bool
    required_checks_passed: bool
    tiles: tuple[PrintTileSliceRecord, ...]

    @model_validator(mode="after")
    def validate_tiles(self) -> Self:
        rows, columns = self.tile_grid_shape
        expected = [(row, column) for row in range(rows) for column in range(columns)]
        actual = [(tile.row, tile.column) for tile in self.tiles]
        if self.tile_count != rows * columns or len(self.tiles) != self.tile_count:
            raise ValueError("slice manifest count does not match its tile grid")
        if actual != expected or len({tile.tile_id for tile in self.tiles}) != self.tile_count:
            raise ValueError("slice records must be unique and in row-major order")
        return self


class PrintTileSliceResult(BaseModel):
    """Published paths for one actual per-tile slice run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_dir: Path
    manifest_path: Path
    report_paths: tuple[Path, ...]
    gcode_paths: tuple[Path, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json_bytes(value: BaseModel) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_canonical_json(path: Path, value: BaseModel) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_canonical_json_bytes(value))
    temporary.replace(path)
    return path


def _read_canonical_json(path: Path, model: type[BaseModel]) -> BaseModel:
    value = model.model_validate_json(path.read_text(encoding="utf-8"))
    if path.read_bytes() != _canonical_json_bytes(value):
        raise ConfigurationError(f"JSON is not canonical: {path}")
    return value


def _resolve_relative(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise ConfigurationError(f"tile slice path escapes output directory: {relative}")
    return candidate


def _copy_profile_files(
    staging: Path, profile: SlicerProfile
) -> tuple[SlicerProfile, tuple[SliceProfileFile, ...]]:
    settings: list[Path] = []
    filaments: list[Path] = []
    records: list[SliceProfileFile] = []
    profile_dir = staging / "profiles"
    profile_dir.mkdir()
    for role, sources, destinations in (
        ("settings", profile.settings, settings),
        ("filament", profile.filaments, filaments),
    ):
        for index, source_value in enumerate(sources):
            source = source_value.expanduser().resolve()
            if not source.is_file():
                raise ConfigurationError(f"slicer profile file does not exist: {source}")
            name = f"{role}-{index:02d}-{source.name}"
            destination = profile_dir / name
            shutil.copyfile(source, destination)
            destinations.append(destination)
            records.append(
                SliceProfileFile(
                    role=role,
                    index=index,
                    path=f"profiles/{name}",
                    sha256=_sha256(destination),
                )
            )
    return (
        SlicerProfile(
            name=profile.name,
            settings=tuple(settings),
            filaments=tuple(filaments),
        ),
        tuple(records),
    )


def _normalized_result(
    result: SliceResult, *, input_relative: str, gcode_relative: str
) -> SliceResult:
    return result.model_copy(
        update={
            "input_model": Path(input_relative),
            "output_gcode": Path(gcode_relative),
        }
    )


def _report(
    *,
    tile_id: str,
    source_manifest_sha256: str,
    input_relative: str,
    input_path: Path,
    gcode_relative: str,
    gcode_path: Path,
    result: SliceResult,
    printer_profile_id: str,
) -> PrintTileSliceReport:
    if not gcode_path.is_file() or gcode_path.stat().st_size <= 0:
        raise SlicerError(f"slicer did not publish non-empty G-code for {tile_id}")
    input_inspection = inspect_3mf(input_path)
    gcode_text = gcode_path.read_text(encoding="utf-8", errors="replace")
    reopened_metrics = parse_gcode_metrics(
        gcode_text,
        diagnostics="\n".join((result.stdout, result.stderr)),
    )
    normalized = _normalized_result(
        result,
        input_relative=input_relative,
        gcode_relative=gcode_relative,
    )
    metrics_match = reopened_metrics == result.metrics
    layers_positive = reopened_metrics.layer_count is not None and reopened_metrics.layer_count > 0
    support_free = reopened_metrics.support_material is False
    release_gate = (
        evaluate_bambu_p2s_release_gate(
            result.model_dump(mode="json"),
            printer_profile_id=printer_profile_id,
        )
        if result.slicer.name == "BambuStudio"
        else None
    )
    release_passed = bool(
        release_gate is not None and release_gate.get("release_gate_passed") is True
    )
    release_role = "official-p2s-release" if release_gate is not None else "diagnostic"
    required = bool(
        result.status is SliceStatus.SUCCEEDED
        and result.exit_code == 0
        and result.gcode_generated
        and result.gcode_size_bytes == gcode_path.stat().st_size
        and input_inspection.strict_warning_count == 0
        and metrics_match
        and layers_positive
        and not reopened_metrics.out_of_bed
        and not reopened_metrics.empty_layer_warning
        and not reopened_metrics.floating_region_warning
        and support_free
        and (release_gate is None or release_passed)
    )
    return PrintTileSliceReport(
        tile_id=tile_id,
        source_print_tile_manifest_sha256=source_manifest_sha256,
        source_print_local_3mf_path=input_relative,
        source_print_local_3mf_sha256=_sha256(input_path),
        gcode_path=gcode_relative,
        gcode_sha256=_sha256(gcode_path),
        gcode_size_bytes=gcode_path.stat().st_size,
        input_strict_3mf_warning_count=input_inspection.strict_warning_count,
        slicer_result=normalized,
        reopened_metrics=reopened_metrics,
        exit_code_zero=result.exit_code == 0,
        gcode_generated=result.gcode_generated,
        metrics_reopen_match=metrics_match,
        layer_count_positive=layers_positive,
        out_of_bed=reopened_metrics.out_of_bed,
        empty_layer_warning=reopened_metrics.empty_layer_warning,
        floating_region_warning=reopened_metrics.floating_region_warning,
        support_material=reopened_metrics.support_material,
        release_role=release_role,
        manufacturing_release_gate=release_gate,
        official_p2s_release_gate_passed=release_passed,
        required_checks_passed=required,
    )


def _optional_sum(values: list[float | int | None]) -> float | int | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def slice_print_tile_set(
    print_set_dir: Path,
    source_mesh_set_dir: Path,
    source_tile_set_dir: Path,
    source_bundle_dir: Path,
    output_dir: Path,
    *,
    adapter: SlicerAdapter,
    profile: SlicerProfile | None = None,
    timeout_seconds: float = 1200.0,
) -> PrintTileSliceResult:
    """Actually slice every print-local 3MF and publish strict evidence."""
    print_set = print_set_dir.expanduser().resolve()
    mesh_set = source_mesh_set_dir.expanduser().resolve()
    tile_set = source_tile_set_dir.expanduser().resolve()
    bundle = source_bundle_dir.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    if output.exists():
        raise ConfigurationError(f"tile slice destination already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    verify_print_tile_set(print_set, mesh_set, tile_set, bundle)
    source_manifest = PrintTileAssemblyManifest.model_validate_json(
        (print_set / "print-tile-assembly-manifest.json").read_text(encoding="utf-8")
    )
    connector_plan = ConnectorPlan.model_validate_json(
        (print_set / source_manifest.connector_plan_path).read_text(encoding="utf-8")
    )
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.topoforge-stage-", dir=output.parent))
    try:
        effective_profile, profile_files = _copy_profile_files(staging, profile or SlicerProfile())
        records: list[PrintTileSliceRecord] = []
        for tile in source_manifest.tiles:
            artifact_path = _resolve_relative(print_set, tile.tile_manifest)
            artifact = PrintTileArtifactManifest.model_validate_json(
                artifact_path.read_text(encoding="utf-8")
            )
            input_relative = tile.files["print_local_3mf"]
            input_path = _resolve_relative(print_set, input_relative)
            tile_dir_relative = f"tiles/{tile.tile_id}"
            tile_dir = staging / tile_dir_relative
            tile_dir.mkdir(parents=True)
            gcode_relative = f"{tile_dir_relative}/model.gcode"
            gcode_path = staging / gcode_relative
            result = adapter.slice(
                input_path,
                gcode_path,
                profile=effective_profile,
                timeout_seconds=timeout_seconds,
            )
            if result.status is not SliceStatus.SUCCEEDED:
                raise SlicerError(
                    f"tile slicer failed for {tile.tile_id}: {result.error or result.status.value}"
                )
            report = _report(
                tile_id=tile.tile_id,
                source_manifest_sha256=tile.tile_manifest_sha256,
                input_relative=input_relative,
                input_path=input_path,
                gcode_relative=gcode_relative,
                gcode_path=gcode_path,
                result=result,
                printer_profile_id=connector_plan.policy.printer_profile.profile_id,
            )
            if not report.required_checks_passed:
                raise SlicerError(f"tile slicer quality gate failed for {tile.tile_id}")
            report_relative = f"{tile_dir_relative}/slice_report.json"
            report_path = _write_canonical_json(staging / report_relative, report)
            metrics = report.reopened_metrics
            if metrics.layer_count is None or metrics.layer_count <= 0:
                raise AssertionError("required slice layer count disappeared")
            records.append(
                PrintTileSliceRecord(
                    tile_id=tile.tile_id,
                    row=tile.row,
                    column=tile.column,
                    directory=tile_dir_relative,
                    report_path=report_relative,
                    report_sha256=_sha256(report_path),
                    gcode_path=gcode_relative,
                    gcode_sha256=report.gcode_sha256,
                    source_print_tile_manifest_sha256=tile.tile_manifest_sha256,
                    source_print_local_3mf_sha256=artifact.sha256["print_local_3mf"],
                    layer_count=metrics.layer_count,
                    estimated_time_seconds=metrics.estimated_time_seconds,
                    filament_used_mm=metrics.filament_used_mm,
                    filament_used_cm3=metrics.filament_used_cm3,
                    filament_used_g=metrics.filament_used_g,
                    required_checks_passed=True,
                )
            )
        probe = adapter.probe()
        executable_hash = (
            _sha256(probe.executable)
            if probe.executable is not None and probe.executable.is_file()
            else None
        )
        time_sum = _optional_sum([record.estimated_time_seconds for record in records])
        filament_mm_sum = _optional_sum([record.filament_used_mm for record in records])
        filament_cm3_sum = _optional_sum([record.filament_used_cm3 for record in records])
        filament_g_sum = _optional_sum([record.filament_used_g for record in records])
        official_release = probe.name == "BambuStudio"
        release_gates = [
            PrintTileSliceReport.model_validate_json(
                (staging / record.report_path).read_text(encoding="utf-8")
            )
            for record in records
        ]
        manifest = PrintTileSliceManifest(
            layout_id=source_manifest.layout_id,
            source_print_tile_assembly_sha256=_sha256(
                print_set / "print-tile-assembly-manifest.json"
            ),
            source_connector_plan_sha256=source_manifest.connector_plan_sha256,
            slicer=probe,
            slicer_executable_sha256=executable_hash,
            profile_name=effective_profile.label,
            profile_files=profile_files,
            printer_profile_id=connector_plan.policy.printer_profile.profile_id,
            release_role="official-p2s-release" if official_release else "diagnostic",
            official_p2s_release_gate_passed=bool(
                official_release
                and all(report.official_p2s_release_gate_passed for report in release_gates)
            ),
            all_parameter_checks_passed=bool(
                official_release
                and all(
                    report.manufacturing_release_gate is not None
                    and report.manufacturing_release_gate.get("parameter_checks_passed") is True
                    for report in release_gates
                )
            ),
            tile_grid_shape=source_manifest.tile_grid_shape,
            tile_count=source_manifest.tile_count,
            total_gcode_size_bytes=sum(
                (staging / record.gcode_path).stat().st_size for record in records
            ),
            total_estimated_time_seconds=None if time_sum is None else int(time_sum),
            total_filament_used_mm=None if filament_mm_sum is None else float(filament_mm_sum),
            total_filament_used_cm3=(None if filament_cm3_sum is None else float(filament_cm3_sum)),
            total_filament_used_g=None if filament_g_sum is None else float(filament_g_sum),
            maximum_layer_count=max(record.layer_count for record in records),
            all_exit_codes_zero=True,
            no_out_of_bed=True,
            no_empty_layers=True,
            no_floating_regions=True,
            no_support_material=True,
            required_checks_passed=True,
            tiles=tuple(records),
        )
        _write_canonical_json(staging / "tile-slice-manifest.json", manifest)
        verify_tile_slice_set(staging, print_set, mesh_set, tile_set, bundle)
        staging.replace(output)
        return PrintTileSliceResult(
            output_dir=output,
            manifest_path=output / "tile-slice-manifest.json",
            report_paths=tuple(output / record.report_path for record in records),
            gcode_paths=tuple(output / record.gcode_path for record in records),
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_tile_slice_set(
    slice_set_dir: Path,
    source_print_set_dir: Path,
    source_mesh_set_dir: Path,
    source_tile_set_dir: Path,
    source_bundle_dir: Path,
) -> dict[str, Any]:
    """Reopen every G-code/report and verify source, profile, and result hashes."""
    root = slice_set_dir.expanduser().resolve()
    print_set = source_print_set_dir.expanduser().resolve()
    mesh_set = source_mesh_set_dir.expanduser().resolve()
    tile_set = source_tile_set_dir.expanduser().resolve()
    bundle = source_bundle_dir.expanduser().resolve()
    verify_print_tile_set(print_set, mesh_set, tile_set, bundle)
    manifest_value = _read_canonical_json(root / "tile-slice-manifest.json", PrintTileSliceManifest)
    if not isinstance(manifest_value, PrintTileSliceManifest):
        raise AssertionError("unexpected tile slice manifest model")
    manifest = manifest_value
    source_manifest = PrintTileAssemblyManifest.model_validate_json(
        (print_set / "print-tile-assembly-manifest.json").read_text(encoding="utf-8")
    )
    connector_plan = ConnectorPlan.model_validate_json(
        (print_set / source_manifest.connector_plan_path).read_text(encoding="utf-8")
    )
    if (
        manifest.layout_id != source_manifest.layout_id
        or manifest.source_print_tile_assembly_sha256
        != _sha256(print_set / "print-tile-assembly-manifest.json")
        or manifest.source_connector_plan_sha256 != source_manifest.connector_plan_sha256
        or manifest.tile_grid_shape != source_manifest.tile_grid_shape
        or manifest.tile_count != source_manifest.tile_count
    ):
        raise ConfigurationError("tile slice manifest does not match source print identities")
    if manifest.slicer_executable_sha256 is not None:
        executable = manifest.slicer.executable
        if (
            executable is None
            or not executable.is_file()
            or _sha256(executable) != manifest.slicer_executable_sha256
        ):
            raise ConfigurationError("tile slice executable checksum mismatch")
    for profile_file in manifest.profile_files:
        path = _resolve_relative(root, profile_file.path)
        if _sha256(path) != profile_file.sha256:
            raise ConfigurationError(f"tile slice profile checksum mismatch: {profile_file.path}")
    source_record_by_id = {tile.tile_id: tile for tile in source_manifest.tiles}
    reports: list[PrintTileSliceReport] = []
    total_size = 0
    for record in manifest.tiles:
        source_record = source_record_by_id[record.tile_id]
        if (
            (record.row, record.column) != (source_record.row, source_record.column)
            or record.source_print_tile_manifest_sha256 != source_record.tile_manifest_sha256
            or record.source_print_local_3mf_sha256 != source_record.sha256["print_local_3mf"]
        ):
            raise ConfigurationError(f"tile slice source identity mismatch: {record.tile_id}")
        report_path = _resolve_relative(root, record.report_path)
        gcode_path = _resolve_relative(root, record.gcode_path)
        if _sha256(report_path) != record.report_sha256:
            raise ConfigurationError(f"tile slice report checksum mismatch: {record.tile_id}")
        if _sha256(gcode_path) != record.gcode_sha256:
            raise ConfigurationError(f"tile slice G-code checksum mismatch: {record.tile_id}")
        report_value = _read_canonical_json(report_path, PrintTileSliceReport)
        if not isinstance(report_value, PrintTileSliceReport):
            raise AssertionError("unexpected tile slice report model")
        report = report_value
        source_input = _resolve_relative(print_set, source_record.files["print_local_3mf"])
        if (
            report.tile_id != record.tile_id
            or report.source_print_tile_manifest_sha256 != source_record.tile_manifest_sha256
            or report.source_print_local_3mf_path != source_record.files["print_local_3mf"]
            or report.source_print_local_3mf_sha256 != _sha256(source_input)
            or report.gcode_path != record.gcode_path
            or report.gcode_sha256 != record.gcode_sha256
            or report.gcode_size_bytes != gcode_path.stat().st_size
            or report.slicer_result.input_model != Path(report.source_print_local_3mf_path)
            or report.slicer_result.output_gcode != Path(report.gcode_path)
        ):
            raise ConfigurationError(f"tile slice report identity mismatch: {record.tile_id}")
        reopened = parse_gcode_metrics(
            gcode_path.read_text(encoding="utf-8", errors="replace"),
            diagnostics="\n".join((report.slicer_result.stdout, report.slicer_result.stderr)),
        )
        if reopened != report.reopened_metrics or reopened != report.slicer_result.metrics:
            raise SlicerError(f"tile G-code metrics changed on reopen: {record.tile_id}")
        expected_gate = (
            evaluate_bambu_p2s_release_gate(
                report.slicer_result.model_dump(mode="json"),
                printer_profile_id=connector_plan.policy.printer_profile.profile_id,
            )
            if report.slicer_result.slicer.name == "BambuStudio"
            else None
        )
        expected_release_passed = bool(
            expected_gate is not None and expected_gate.get("release_gate_passed") is True
        )
        if (
            report.manufacturing_release_gate != expected_gate
            or report.official_p2s_release_gate_passed != expected_release_passed
            or report.release_role
            != ("official-p2s-release" if expected_gate is not None else "diagnostic")
        ):
            raise SlicerError(f"tile release gate changed on reopen: {record.tile_id}")
        required = bool(
            report.required_checks_passed
            and report.slicer_result.status is SliceStatus.SUCCEEDED
            and report.slicer_result.exit_code == 0
            and report.exit_code_zero
            and report.gcode_generated
            and report.metrics_reopen_match
            and report.layer_count_positive
            and not report.out_of_bed
            and not report.empty_layer_warning
            and not report.floating_region_warning
            and report.support_material is False
            and (expected_gate is None or expected_release_passed)
        )
        if not required:
            raise SlicerError(f"tile slice quality gate changed on reopen: {record.tile_id}")
        if (
            record.layer_count != reopened.layer_count
            or record.estimated_time_seconds != reopened.estimated_time_seconds
            or record.filament_used_mm != reopened.filament_used_mm
            or record.filament_used_cm3 != reopened.filament_used_cm3
            or record.filament_used_g != reopened.filament_used_g
            or not record.required_checks_passed
        ):
            raise ConfigurationError(f"tile slice summary mismatch: {record.tile_id}")
        total_size += gcode_path.stat().st_size
        reports.append(report)
    time_sum = _optional_sum([record.estimated_time_seconds for record in manifest.tiles])
    filament_mm_sum = _optional_sum([record.filament_used_mm for record in manifest.tiles])
    filament_cm3_sum = _optional_sum([record.filament_used_cm3 for record in manifest.tiles])
    filament_g_sum = _optional_sum([record.filament_used_g for record in manifest.tiles])
    if (
        total_size != manifest.total_gcode_size_bytes
        or manifest.total_estimated_time_seconds != (None if time_sum is None else int(time_sum))
        or manifest.total_filament_used_mm
        != (None if filament_mm_sum is None else float(filament_mm_sum))
        or manifest.total_filament_used_cm3
        != (None if filament_cm3_sum is None else float(filament_cm3_sum))
        or manifest.total_filament_used_g
        != (None if filament_g_sum is None else float(filament_g_sum))
        or manifest.maximum_layer_count != max(record.layer_count for record in manifest.tiles)
        or manifest.printer_profile_id != connector_plan.policy.printer_profile.profile_id
        or manifest.release_role
        != ("official-p2s-release" if manifest.slicer.name == "BambuStudio" else "diagnostic")
        or manifest.official_p2s_release_gate_passed
        != bool(
            manifest.slicer.name == "BambuStudio"
            and all(report.official_p2s_release_gate_passed for report in reports)
        )
        or manifest.all_parameter_checks_passed
        != bool(
            manifest.slicer.name == "BambuStudio"
            and all(
                report.manufacturing_release_gate is not None
                and report.manufacturing_release_gate.get("parameter_checks_passed") is True
                for report in reports
            )
        )
        or not manifest.all_exit_codes_zero
        or not manifest.no_out_of_bed
        or not manifest.no_empty_layers
        or not manifest.no_floating_regions
        or not manifest.no_support_material
        or not manifest.required_checks_passed
    ):
        raise ConfigurationError("tile slice aggregate summary mismatch")
    return {
        "status": "verified",
        "output_dir": str(root),
        "layout_id": manifest.layout_id,
        "tile_grid_shape": manifest.tile_grid_shape,
        "tile_count": manifest.tile_count,
        "slicer": manifest.slicer.model_dump(mode="json"),
        "profile": manifest.profile_name,
        "printer_profile_id": manifest.printer_profile_id,
        "release_role": manifest.release_role,
        "official_p2s_release_gate_passed": manifest.official_p2s_release_gate_passed,
        "all_parameter_checks_passed": manifest.all_parameter_checks_passed,
        "total_gcode_size_bytes": manifest.total_gcode_size_bytes,
        "total_estimated_time_seconds": manifest.total_estimated_time_seconds,
        "total_filament_used_mm": manifest.total_filament_used_mm,
        "total_filament_used_cm3": manifest.total_filament_used_cm3,
        "total_filament_used_g": manifest.total_filament_used_g,
        "maximum_layer_count": manifest.maximum_layer_count,
        "all_exit_codes_zero": True,
        "no_out_of_bed": True,
        "no_empty_layers": True,
        "no_floating_regions": True,
        "no_support_material": True,
        "required_checks_passed": True,
    }
