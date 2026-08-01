"""Strict no-key global source acquisition used by resumable local workflows."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.exceptions import ConfigurationError, ProviderFetchError
from topoforge.models import AreaOfInterest, AreaOfInterestInput, DatasetMetadata, TerrainMode
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
from topoforge.util import sha256_file


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


def _manifest_path(destination: Path) -> Path:
    return destination.with_suffix(destination.suffix + ".source_acquisition.json")


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderFetchError(f"provider acquisition manifest is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ProviderFetchError("provider acquisition manifest root is not an object")
    return value


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
    paths: list[Path] = []
    roles: set[str] = set()
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
        path = Path(raw_path).expanduser().resolve()
        if path.parent != Path(str(manifest.get("raster_path"))).expanduser().resolve().parent:
            raise ProviderFetchError(f"provider quality mask escapes acquisition directory: {path}")
        if not path.is_file() or sha256_file(path) != expected_sha256:
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
    raster_path = destination.expanduser().resolve()
    manifest_path = _manifest_path(raster_path)
    if not raster_path.is_file() or raster_path.stat().st_size <= 0:
        raise ProviderFetchError(f"provider raster is missing or empty: {raster_path}")
    if not manifest_path.is_file() or manifest_path.stat().st_size <= 0:
        raise ProviderFetchError(f"provider acquisition manifest is missing: {manifest_path}")
    manifest = _load_manifest(manifest_path)
    raster_sha256 = sha256_file(raster_path)
    if manifest.get("output_raster_sha256") != raster_sha256:
        raise ProviderFetchError("provider output raster SHA-256 changed")
    if Path(str(manifest.get("raster_path"))).expanduser().resolve() != raster_path:
        raise ProviderFetchError("provider manifest raster_path does not match the stage output")
    if Path(str(manifest.get("acquisition_manifest_path"))).expanduser().resolve() != manifest_path:
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
        acquisition_manifest_sha256=sha256_file(manifest_path),
        dataset=dataset,
        normalized_aoi=normalized,
        provider_selection=trace,
        quality_mask_paths=quality_paths,
        cache_summary=cache_summary,
        required_checks_passed=True,
    )


def acquire_global_source(
    config: GlobalAcquisitionConfig,
    destination: Path,
    *,
    providers: Mapping[str, ElevationProvider] | None = None,
    descriptors: Sequence[ProviderDescriptor] | None = None,
) -> GlobalSourceEvidence:
    """Acquire one no-key global source and strictly verify its persisted evidence."""
    if (providers is None) != (descriptors is None):
        raise ConfigurationError(
            "providers and descriptors test overrides must be supplied together"
        )
    cache_summary: dict[str, Any] | None = None
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
        cache_summary = cache_store.summary().model_dump(mode="json")
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
    evidence = verify_global_source(
        config,
        selection.acquisition.raster_path,
        cache_summary=cache_summary,
    )
    if (
        selection.acquisition.acquisition_manifest_path.resolve()
        != evidence.acquisition_manifest_path
        or selection.trace != evidence.provider_selection
    ):
        raise ProviderFetchError("in-memory acquisition does not match persisted provider evidence")
    if providers is None:
        cache_store = ContentAddressedCache(config.cache_dir)
        return evidence.model_copy(
            update={"cache_summary": cache_store.summary().model_dump(mode="json")}
        )
    return evidence
