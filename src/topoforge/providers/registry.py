"""Stable provider registry; unimplemented entries are explicit rather than hidden."""

from topoforge.models import DatasetType
from topoforge.providers.protocol import ProviderDescriptor


def list_provider_descriptors() -> list[ProviderDescriptor]:
    """List current and planned providers without making network requests."""
    return [
        ProviderDescriptor(
            provider_id="local",
            name="Local GeoTIFF/DEM",
            implemented=True,
            requires_api_key=False,
            dataset_types=list(DatasetType),
            notes="Phase 1 engine; user-supplied license and vertical datum stay explicit.",
        ),
        ProviderDescriptor(
            provider_id="copernicus-aws",
            name="Copernicus DEM GLO-30/GLO-90 AWS COG mirror",
            implemented=False,
            requires_api_key=False,
            dataset_types=[DatasetType.DSM],
            notes=(
                "Planned Phase 3 no-key route; configurable 2021 COG endpoints and "
                "dataset-specific GLO-30/GLO-90 attribution."
            ),
        ),
        ProviderDescriptor(
            provider_id="copernicus-cdse",
            name="Copernicus Data Space Ecosystem DEM",
            implemented=False,
            requires_api_key=True,
            dataset_types=[DatasetType.DSM],
            notes="Optional authenticated route; separate from the default AWS mirror.",
        ),
        ProviderDescriptor(
            provider_id="aws-terrain-tiles",
            name="AWS Open Data Terrain Tiles",
            implemented=False,
            requires_api_key=False,
            dataset_types=[DatasetType.MIXED, DatasetType.UNKNOWN],
            notes=(
                "Candidate no-key fallback; tile provenance must identify the underlying dataset."
            ),
        ),
        ProviderDescriptor(
            provider_id="usgs-3dep",
            name="USGS 3DEP",
            implemented=False,
            requires_api_key=False,
            dataset_types=[DatasetType.DTM],
            notes="Planned regional high-resolution provider for the United States.",
        ),
    ]
