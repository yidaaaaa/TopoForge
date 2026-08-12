"""Strict no-key global source acquisition used by resumable local workflows."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.exceptions import ConfigurationError, ProviderFetchError
from topoforge.models import AreaOfInterest, AreaOfInterestInput, DatasetMetadata, TerrainMode
from topoforge.platforms import stat_result_is_link_like
from topoforge.providers import (
    CachingHttpClient,
    ContentAddressedCache,
    CopernicusAwsProvider,
    ElevationProvider,
    HttpTransportConfig,
    ProviderAcquisition,
    ProviderDescriptor,
    ProviderSelectionPolicy,
    ProviderSelectionTrace,
    fetch_with_provider_selection,
    list_provider_descriptors,
)
from topoforge.raster import normalize_area_of_interest
from topoforge.util import sha256_bytes, sha256_file

_MANIFEST_MAX_BYTES = 8 * 1024 * 1024
_MAX_PROVIDER_OUTPUT_FILES = 32
_MAX_PROVIDER_OUTPUT_FILE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_PROVIDER_OUTPUT_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


class GlobalAcquisitionConfig(BaseModel):
    """Normalized-AOI provider, cache, and bounded transport settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aoi: AreaOfInterestInput
    requested_provider_id: str = "auto"
    terrain_mode: TerrainMode = TerrainMode.BEST_AVAILABLE
    allow_semantic_fallback: bool = False
    preferred_provider_ids: tuple[str, ...] = ()
    cache_dir: Path = Path("cache/providers")
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=4, ge=1, le=10)
    min_request_interval_seconds: float = Field(default=0.2, ge=0)

    @model_validator(mode="after")
    def validate_policy(self) -> GlobalAcquisitionConfig:
        """Reuse the provider policy validator for identifiers and preferences."""
        self.selection_policy()
        return self

    def normalized_aoi(self) -> AreaOfInterest:
        """Return the production normalized WGS84/metric AOI contract."""
        return normalize_area_of_interest(self.aoi)

    def selection_policy(self) -> ProviderSelectionPolicy:
        """Return the exact deterministic provider selection policy."""
        return ProviderSelectionPolicy(
            requested_provider_id=self.requested_provider_id,
            requested_terrain_mode=self.terrain_mode,
            allow_semantic_fallback=self.allow_semantic_fallback,
            preferred_provider_ids=list(self.preferred_provider_ids),
        )

    def identity_payload(self) -> dict[str, Any]:
        """Return content-affecting acquisition settings, excluding retry/cache location."""
        return {
            "aoi": self.normalized_aoi().model_dump(mode="json"),
            "policy": self.selection_policy().model_dump(mode="json"),
            "provider_contract": "copernicus-aws-glo30-glo90-v1",
        }


class GlobalSourceEvidence(BaseModel):
    """Strictly reopened provider raster, provenance, masks, and selection trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    raster_path: Path
    acquisition_manifest_path: Path
    raster_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acquisition_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset: DatasetMetadata
    normalized_aoi: AreaOfInterest
    provider_selection: ProviderSelectionTrace
    quality_mask_paths: tuple[Path, ...] = ()
    cache_summary: dict[str, Any] | None = None
    required_checks_passed: bool


@dataclass(frozen=True, slots=True)
class GlobalSourceFetch:
    """One completed provider fetch awaiting strict verification or snapshotting."""

    acquisition: ProviderAcquisition
    provider_selection: ProviderSelectionTrace
    cache_summary: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ProviderOutputSnapshot:
    """Private immutable-by-convention copy and the temporary provider drop it replaces."""

    raster_path: Path
    acquisition_manifest_path: Path
    source_directory: Path
    source_directory_identity: tuple[int, int]
    source_entries: tuple[tuple[Path, tuple[int, int]], ...]


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _manifest_path(destination: Path) -> Path:
    return destination.with_suffix(destination.suffix + ".source_acquisition.json")


def _load_manifest_bytes(payload: bytes, *, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderFetchError(f"provider acquisition manifest is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ProviderFetchError("provider acquisition manifest root is not an object")
    return value


def _load_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    from topoforge.web.security import read_stable_regular_bytes

    try:
        payload = read_stable_regular_bytes(
            path,
            context="provider acquisition manifest",
            max_bytes=_MANIFEST_MAX_BYTES,
        )
    except (OSError, ValueError) as exc:
        raise ProviderFetchError(f"provider acquisition manifest is unreadable: {path}") from exc
    return _load_manifest_bytes(payload, path=path), payload


def exact_regular_file_inventory(
    directory: Path,
    expected_paths: Sequence[Path],
    *,
    context: str,
) -> dict[Path, tuple[int, int]]:
    """No-follow enumerate one flat directory and require an exact regular-file set."""
    root = _absolute(directory)
    expected: dict[str, Path] = {}
    for raw_path in expected_paths:
        path = _absolute(raw_path)
        if path.parent != root or path.name in expected:
            raise ValueError(f"{context} contains duplicate or escaping declared paths")
        expected[path.name] = path
    if len(expected) > _MAX_PROVIDER_OUTPUT_FILES:
        raise ValueError(f"{context} declares too many files; split or inspect the provider output")

    try:
        root_stat = root.lstat()
        if stat_result_is_link_like(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError(f"{context} root must be one real directory: {root}")
        observed: set[str] = set()
        with os.scandir(root) as entries:
            for entry in entries:
                if len(observed) >= _MAX_PROVIDER_OUTPUT_FILES:
                    raise ValueError(
                        f"{context} contains too many entries; preserve it for inspection"
                    )
                observed.add(entry.name)
    except OSError as exc:
        raise ValueError(f"{context} could not be enumerated without following links") from exc

    if observed != set(expected):
        missing = sorted(set(expected) - observed)
        unexpected = sorted(observed - set(expected))
        raise ValueError(
            f"{context} file inventory changed; missing={missing}, unexpected={unexpected}"
        )

    identities: dict[Path, tuple[int, int]] = {}
    total_bytes = 0
    for name, path in expected.items():
        try:
            entry_stat = path.lstat()
        except OSError as exc:
            raise ValueError(f"{context} file is missing or unreadable: {name}") from exc
        if (
            stat_result_is_link_like(entry_stat)
            or not stat.S_ISREG(entry_stat.st_mode)
            or entry_stat.st_nlink != 1
        ):
            raise ValueError(f"{context} file must be one non-linked regular file: {name}")
        if entry_stat.st_size > _MAX_PROVIDER_OUTPUT_FILE_BYTES:
            raise ValueError(
                f"{context} file exceeds the {_MAX_PROVIDER_OUTPUT_FILE_BYTES}-byte limit: {name}"
            )
        total_bytes += entry_stat.st_size
        if total_bytes > _MAX_PROVIDER_OUTPUT_TOTAL_BYTES:
            raise ValueError(
                f"{context} exceeds the {_MAX_PROVIDER_OUTPUT_TOTAL_BYTES}-byte total limit"
            )
        identities[path] = (entry_stat.st_dev, entry_stat.st_ino)
    return identities


def _manifest_quality_paths(manifest: dict[str, Any], *, root: Path) -> tuple[Path, ...]:
    raw_records = manifest.get("quality_masks", [])
    if not isinstance(raw_records, list):
        raise ProviderFetchError("provider quality_masks is not a list")
    paths: list[Path] = []
    roles: set[str] = set()
    names: set[str] = set()
    for record in raw_records:
        if not isinstance(record, dict):
            raise ProviderFetchError("provider quality mask record is not an object")
        if record.get("availability") != "present":
            continue
        role = record.get("role")
        output = record.get("output")
        if not isinstance(role, str) or not role or role in roles:
            raise ProviderFetchError("provider quality mask roles must be unique strings")
        if not isinstance(output, dict) or not isinstance(output.get("path"), str):
            raise ProviderFetchError("present provider quality mask has no output path")
        roles.add(role)
        path = _absolute(Path(output["path"]))
        if path.parent != root or path.name in names:
            raise ProviderFetchError(
                f"provider quality mask escapes or duplicates the acquisition directory: {path}"
            )
        names.add(path.name)
        paths.append(path)
    return tuple(paths)


def snapshot_provider_output(
    acquisition: ProviderAcquisition,
    destination: Path,
    *,
    private_root: Path,
    private_root_identity: tuple[int, int],
) -> ProviderOutputSnapshot:
    """Copy one completed flat provider drop into an exclusive program-owned snapshot."""
    from topoforge.web.security import (
        create_owned_directory,
        open_exclusive_owned_regular_binary,
        open_owned_regular_binary,
        owned_directory_identity,
        read_owned_regular_bytes,
        write_exclusive_owned_regular_bytes,
    )

    source_raster = _absolute(acquisition.raster_path)
    source_manifest = _absolute(acquisition.acquisition_manifest_path)
    source_directory = source_raster.parent
    if source_manifest.parent != source_directory or source_manifest != _manifest_path(
        source_raster
    ):
        raise ProviderFetchError(
            "provider acquisition paths do not form one flat output directory; correct the "
            "provider implementation and retry"
        )
    try:
        source_identity = owned_directory_identity(
            source_directory,
            root=private_root,
            root_identity=private_root_identity,
            context="provider output directory",
        )
        manifest_bytes = read_owned_regular_bytes(
            source_manifest,
            root=source_directory,
            root_identity=source_identity,
            context="provider output manifest",
            max_bytes=_MANIFEST_MAX_BYTES,
        )
        manifest = _load_manifest_bytes(manifest_bytes, path=source_manifest)
        if (
            _absolute(Path(str(manifest.get("raster_path")))) != source_raster
            or _absolute(Path(str(manifest.get("acquisition_manifest_path")))) != source_manifest
        ):
            raise ProviderFetchError(
                "provider manifest paths do not match the completed provider drop; correct the "
                "provider implementation and retry"
            )
        source_quality = _manifest_quality_paths(manifest, root=source_directory)
        expected_source = (source_raster, source_manifest, *source_quality)
        source_entries = exact_regular_file_inventory(
            source_directory,
            expected_source,
            context="provider output",
        )

        snapshot_directory = _absolute(destination)
        create_owned_directory(
            snapshot_directory,
            root=private_root,
            root_identity=private_root_identity,
            context="provider snapshot directory",
        )
        snapshot_identity = owned_directory_identity(
            snapshot_directory,
            root=private_root,
            root_identity=private_root_identity,
            context="provider snapshot directory",
        )
        snapshot_raster = snapshot_directory / source_raster.name
        snapshot_manifest = snapshot_directory / source_manifest.name
        snapshot_quality = tuple(snapshot_directory / path.name for path in source_quality)

        for source, target in zip(
            (source_raster, *source_quality),
            (snapshot_raster, *snapshot_quality),
            strict=True,
        ):
            with (
                open_owned_regular_binary(
                    source,
                    root=source_directory,
                    root_identity=source_identity,
                    expected_identity=source_entries[source],
                    context=f"provider snapshot source {source.name}",
                ) as reader,
                open_exclusive_owned_regular_binary(
                    target,
                    root=snapshot_directory,
                    root_identity=snapshot_identity,
                    context=f"provider snapshot target {target.name}",
                ) as writer,
            ):
                copied = 0
                while block := reader.read(_COPY_CHUNK_BYTES):
                    copied += len(block)
                    if copied > _MAX_PROVIDER_OUTPUT_FILE_BYTES:
                        raise ProviderFetchError(
                            f"provider output grew beyond the snapshot limit: {source}"
                        )
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())

        manifest["raster_path"] = str(snapshot_raster)
        manifest["acquisition_manifest_path"] = str(snapshot_manifest)
        snapshot_quality_by_name = {path.name: path for path in snapshot_quality}
        for record in manifest.get("quality_masks", []):
            if not isinstance(record, dict) or record.get("availability") != "present":
                continue
            output = record.get("output")
            if not isinstance(output, dict):
                raise ProviderFetchError("present provider quality mask has no output record")
            old_path = _absolute(Path(str(output.get("path"))))
            target = snapshot_quality_by_name.get(old_path.name)
            if target is None:
                raise ProviderFetchError("provider quality mask inventory changed during snapshot")
            output["path"] = str(target)
        rebound_manifest = (
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        write_exclusive_owned_regular_bytes(
            snapshot_manifest,
            rebound_manifest,
            root=snapshot_directory,
            root_identity=snapshot_identity,
            context="provider snapshot manifest",
        )
        exact_regular_file_inventory(
            snapshot_directory,
            (snapshot_raster, snapshot_manifest, *snapshot_quality),
            context="provider snapshot",
        )
    except ProviderFetchError:
        raise
    except (OSError, ValueError) as exc:
        raise ProviderFetchError(
            "provider output could not be copied into a safe private snapshot; preserve the "
            "private stage for inspection and retry"
        ) from exc

    return ProviderOutputSnapshot(
        raster_path=snapshot_raster,
        acquisition_manifest_path=snapshot_manifest,
        source_directory=source_directory,
        source_directory_identity=source_identity,
        source_entries=tuple(source_entries.items()),
    )


def cleanup_provider_output(
    snapshot: ProviderOutputSnapshot,
    *,
    private_root: Path,
    private_root_identity: tuple[int, int],
) -> None:
    """Remove only the exact temporary provider drop after its snapshot verifies."""
    from topoforge.web.security import remove_owned_path

    try:
        for path, identity in reversed(snapshot.source_entries):
            remove_owned_path(
                path,
                root=snapshot.source_directory,
                root_identity=snapshot.source_directory_identity,
                expected_identity=identity,
                directory=False,
                context=f"provider drop cleanup {path.name}",
            )
        remove_owned_path(
            snapshot.source_directory,
            root=private_root,
            root_identity=private_root_identity,
            expected_identity=snapshot.source_directory_identity,
            directory=True,
            context="provider drop cleanup",
        )
    except (OSError, ValueError) as exc:
        raise ProviderFetchError(
            "provider snapshot is verified but the unpublished provider drop could not be "
            "cleaned; preserve the private stage for inspection before retrying"
        ) from exc


def _require_regular_file(path: Path, *, context: str) -> None:
    try:
        result = path.lstat()
    except OSError as exc:
        raise ProviderFetchError(f"{context} is missing or unreadable: {path}") from exc
    if (
        stat_result_is_link_like(result)
        or not stat.S_ISREG(result.st_mode)
        or result.st_nlink != 1
        or result.st_size <= 0
    ):
        raise ProviderFetchError(f"{context} must be a non-empty, non-linked regular file: {path}")
    if result.st_size > _MAX_PROVIDER_OUTPUT_FILE_BYTES:
        raise ProviderFetchError(
            f"{context} exceeds the {_MAX_PROVIDER_OUTPUT_FILE_BYTES}-byte safety limit: {path}"
        )


def _quality_mask_paths(
    manifest: dict[str, Any],
    *,
    raster_shape: tuple[int, int],
    raster_crs: Any,
    raster_transform: Any,
) -> tuple[Path, ...]:
    raw_records = manifest.get("quality_masks", [])
    if not isinstance(raw_records, list):
        raise ProviderFetchError("provider quality_masks is not a list")
    raster_value = manifest.get("raster_path")
    if not isinstance(raster_value, str):
        raise ProviderFetchError("provider manifest raster_path is invalid")
    raster_parent = _absolute(Path(raster_value)).parent
    paths: list[Path] = []
    roles: set[str] = set()
    names: set[str] = set()
    for record in raw_records:
        if not isinstance(record, dict):
            raise ProviderFetchError("provider quality mask record is not an object")
        if record.get("availability") != "present":
            continue
        role = record.get("role")
        if not isinstance(role, str) or not role or role in roles:
            raise ProviderFetchError("provider quality mask roles must be unique strings")
        roles.add(role)
        output = record.get("output")
        if not isinstance(output, dict):
            raise ProviderFetchError("present provider quality mask has no output record")
        raw_path = output.get("path")
        expected_sha256 = output.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(expected_sha256, str):
            raise ProviderFetchError("provider quality mask path/SHA-256 is invalid")
        path = _absolute(Path(raw_path))
        if path.parent != raster_parent or path.name in names:
            raise ProviderFetchError(f"provider quality mask escapes acquisition directory: {path}")
        names.add(path.name)
        _require_regular_file(path, context=f"provider quality mask {role}")
        observed_sha256 = sha256_file(path)
        if observed_sha256 != expected_sha256:
            raise ProviderFetchError(f"provider quality mask checksum mismatch: {path}")
        with rasterio.open(path) as dataset:
            if (
                dataset.count != 1
                or dataset.shape != raster_shape
                or dataset.crs != raster_crs
                or not dataset.transform.almost_equals(raster_transform)
            ):
                raise ProviderFetchError(f"provider quality mask alignment changed: {path}")
        paths.append(path)
    return tuple(paths)


def verify_global_source(
    config: GlobalAcquisitionConfig,
    destination: Path,
    *,
    cache_summary: dict[str, Any] | None = None,
) -> GlobalSourceEvidence:
    """Strictly reopen one acquired metric raster and its complete provider evidence."""
    from topoforge.web.security import real_directory_tree_identity

    raster_path = _absolute(destination)
    manifest_path = _manifest_path(raster_path)
    try:
        real_directory_tree_identity(
            raster_path.parent,
            context="provider acquisition directory",
        )
    except (OSError, ValueError) as exc:
        raise ProviderFetchError(
            f"provider acquisition directory is unsafe: {raster_path.parent}"
        ) from exc
    _require_regular_file(raster_path, context="provider raster")
    _require_regular_file(manifest_path, context="provider acquisition manifest")
    manifest, manifest_bytes = _load_manifest(manifest_path)
    raster_sha256 = sha256_file(raster_path)
    if manifest.get("output_raster_sha256") != raster_sha256:
        raise ProviderFetchError("provider output raster SHA-256 changed")
    if (
        not isinstance(manifest.get("raster_path"), str)
        or _absolute(Path(manifest["raster_path"])) != raster_path
    ):
        raise ProviderFetchError("provider manifest raster_path does not match the stage output")
    if (
        not isinstance(manifest.get("acquisition_manifest_path"), str)
        or _absolute(Path(manifest["acquisition_manifest_path"])) != manifest_path
    ):
        raise ProviderFetchError("provider manifest path does not match the stage output")

    normalized = config.normalized_aoi()
    if manifest.get("aoi") != normalized.model_dump(mode="json"):
        raise ProviderFetchError("provider manifest AOI does not match the normalized request")
    dataset = DatasetMetadata.model_validate(manifest.get("dataset"))
    trace = ProviderSelectionTrace.model_validate(manifest.get("provider_selection"))
    if trace.policy != config.selection_policy():
        raise ProviderFetchError("provider selection policy does not match the workflow request")
    provider_id = manifest.get("provider_id")
    if (
        not isinstance(provider_id, str)
        or trace.outcome != "selected"
        or trace.selected_provider != provider_id
        or (config.requested_provider_id != "auto" and provider_id != config.requested_provider_id)
        or dataset.provider != provider_id
        or trace.selected_dataset != dataset.dataset_name
    ):
        raise ProviderFetchError("provider selection trace does not bind the acquired dataset")

    with rasterio.open(raster_path) as raster:
        values = raster.read(1, masked=True)
        finite = np.isfinite(values.data)
        valid = finite & ~np.ma.getmaskarray(values)
        if (
            raster.count != 1
            or raster.crs is None
            or raster.crs.is_geographic
            or values.size < 16
            or not np.any(valid)
        ):
            raise ProviderFetchError(
                "provider raster must be a non-empty single-band metric elevation grid"
            )
        recorded_nodata = manifest.get("output_source_nodata_pixels")
        if not isinstance(recorded_nodata, int) or recorded_nodata != int(np.count_nonzero(~valid)):
            raise ProviderFetchError("provider output NoData count changed")
        quality_paths = _quality_mask_paths(
            manifest,
            raster_shape=raster.shape,
            raster_crs=raster.crs,
            raster_transform=raster.transform,
        )
    return GlobalSourceEvidence(
        raster_path=raster_path,
        acquisition_manifest_path=manifest_path,
        raster_sha256=raster_sha256,
        acquisition_manifest_sha256=sha256_bytes(manifest_bytes),
        dataset=dataset,
        normalized_aoi=normalized,
        provider_selection=trace,
        quality_mask_paths=quality_paths,
        cache_summary=cache_summary,
        required_checks_passed=True,
    )


def fetch_global_source(
    config: GlobalAcquisitionConfig,
    destination: Path,
    *,
    providers: Mapping[str, ElevationProvider] | None = None,
    descriptors: Sequence[ProviderDescriptor] | None = None,
) -> GlobalSourceFetch:
    """Fetch one provider result without validating files before a caller-owned snapshot."""
    if (providers is None) != (descriptors is None):
        raise ConfigurationError(
            "providers and descriptors test overrides must be supplied together"
        )
    cache_store: ContentAddressedCache | None = None
    if providers is None:
        cache_store = ContentAddressedCache(config.cache_dir)
        client = CachingHttpClient(
            cache_store,
            HttpTransportConfig(
                timeout_seconds=config.timeout_seconds,
                max_attempts=config.max_attempts,
                min_request_interval_seconds=config.min_request_interval_seconds,
            ),
        )
        provider_map: Mapping[str, ElevationProvider] = {
            "copernicus-aws": CopernicusAwsProvider(client)
        }
        descriptor_values: Sequence[ProviderDescriptor] = [
            item for item in list_provider_descriptors() if item.provider_id != "local"
        ]
    else:
        provider_map = providers
        descriptor_values = descriptors or ()
    selection = fetch_with_provider_selection(
        aoi=config.normalized_aoi(),
        destination=destination,
        providers=dict(provider_map),
        descriptors=list(descriptor_values),
        policy=config.selection_policy(),
    )
    if not isinstance(selection.acquisition, ProviderAcquisition):
        raise ProviderFetchError("selected provider returned an unsupported acquisition result")
    return GlobalSourceFetch(
        acquisition=selection.acquisition,
        provider_selection=selection.trace,
        cache_summary=(
            None if cache_store is None else cache_store.summary().model_dump(mode="json")
        ),
    )


def acquire_global_source_snapshot(
    config: GlobalAcquisitionConfig,
    snapshot_directory: Path,
    *,
    private_root: Path,
    private_root_identity: tuple[int, int],
    providers: Mapping[str, ElevationProvider] | None = None,
    descriptors: Sequence[ProviderDescriptor] | None = None,
) -> GlobalSourceEvidence:
    """Fetch into a provider drop, snapshot its exact closure, and verify only the snapshot."""
    from topoforge.web.security import create_owned_directory

    provider_output = _absolute(private_root) / "provider-output"
    try:
        create_owned_directory(
            provider_output,
            root=private_root,
            root_identity=private_root_identity,
            context="provider output directory",
        )
    except (OSError, ValueError) as exc:
        raise ProviderFetchError(
            "provider output directory could not be created exclusively; preserve the private "
            "stage for inspection and retry"
        ) from exc

    fetched = fetch_global_source(
        config,
        provider_output / "global-aoi.tif",
        providers=providers,
        descriptors=descriptors,
    )
    snapshot = snapshot_provider_output(
        fetched.acquisition,
        snapshot_directory,
        private_root=private_root,
        private_root_identity=private_root_identity,
    )
    evidence = verify_global_source(
        config,
        snapshot.raster_path,
        cache_summary=fetched.cache_summary,
    )
    if (
        snapshot.acquisition_manifest_path != evidence.acquisition_manifest_path
        or fetched.provider_selection != evidence.provider_selection
    ):
        raise ProviderFetchError(
            "provider selection does not match the verified private snapshot; preserve the "
            "private stage for inspection and retry"
        )
    cleanup_provider_output(
        snapshot,
        private_root=private_root,
        private_root_identity=private_root_identity,
    )
    return evidence


def acquire_global_source(
    config: GlobalAcquisitionConfig,
    destination: Path,
    *,
    providers: Mapping[str, ElevationProvider] | None = None,
    descriptors: Sequence[ProviderDescriptor] | None = None,
) -> GlobalSourceEvidence:
    """Acquire one no-key global source and strictly verify its persisted evidence."""
    fetched = fetch_global_source(
        config,
        destination,
        providers=providers,
        descriptors=descriptors,
    )
    evidence = verify_global_source(
        config,
        fetched.acquisition.raster_path,
        cache_summary=fetched.cache_summary,
    )
    if (
        _absolute(fetched.acquisition.acquisition_manifest_path)
        != evidence.acquisition_manifest_path
        or fetched.provider_selection != evidence.provider_selection
    ):
        raise ProviderFetchError("in-memory acquisition does not match persisted provider evidence")
    return evidence
