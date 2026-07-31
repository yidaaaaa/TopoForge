"""Explainable provider-selection contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from topoforge.models import AreaOfInterest, DatasetMetadata, DatasetType


class CoverageInfo(BaseModel):
    """Provider coverage and operational constraints for one AOI."""

    model_config = ConfigDict(extra="forbid")

    covered: bool
    complete: bool
    dataset_type: DatasetType
    horizontal_resolution_m: float | None = Field(default=None, gt=0)
    requires_api_key: bool = False
    estimated_download_bytes: int | None = Field(default=None, ge=0)
    failure_probability: str = "unknown"
    reason: list[str] = Field(default_factory=list)


class ProviderDescriptor(BaseModel):
    """User-facing provider capability without triggering network access."""

    provider_id: str
    name: str
    implemented: bool
    requires_api_key: bool
    dataset_types: list[DatasetType]
    notes: str


@runtime_checkable
class ElevationProvider(Protocol):
    """Common interface implemented by local and future network providers."""

    provider_id: str

    def probe(self, aoi: AreaOfInterest) -> CoverageInfo:
        """Return explainable coverage for a normalized AOI."""
        ...

    def metadata(self) -> DatasetMetadata:
        """Return default dataset semantics and license metadata."""
        ...

    def fetch(self, aoi: AreaOfInterest, destination: Path) -> object:
        """Fetch a normalized AOI into a cache-controlled local raster."""
        ...
