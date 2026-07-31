"""Elevation provider protocol, cache, transport, and registry."""

from topoforge.providers.cache import (
    CacheEntry,
    CacheIdentity,
    CacheLookup,
    CacheStatus,
    CacheSummary,
    ContentAddressedCache,
)
from topoforge.providers.copernicus_aws import (
    CopernicusAwsConfig,
    CopernicusAwsProvider,
    CopernicusPlan,
    CopernicusProduct,
    CopernicusQualityMaskRole,
    CopernicusTile,
    ProviderAcquisition,
    parse_s3_object_listing,
    parse_tile_list,
    required_tile_coordinates,
    tile_id,
)
from topoforge.providers.protocol import CoverageInfo, ElevationProvider, ProviderDescriptor
from topoforge.providers.registry import list_provider_descriptors
from topoforge.providers.selection import (
    ProviderEvaluation,
    ProviderFetchAttempt,
    ProviderFetchSelection,
    ProviderSelectionError,
    ProviderSelectionPolicy,
    ProviderSelectionTrace,
    evaluate_providers,
    fetch_with_provider_selection,
)
from topoforge.providers.transport import (
    CachingHttpClient,
    DownloadResult,
    HttpTransportConfig,
    NetworkAttempt,
)

__all__ = [
    "CacheEntry",
    "CacheIdentity",
    "CacheLookup",
    "CacheStatus",
    "CacheSummary",
    "CachingHttpClient",
    "ContentAddressedCache",
    "CopernicusAwsConfig",
    "CopernicusAwsProvider",
    "CopernicusPlan",
    "CopernicusProduct",
    "CopernicusQualityMaskRole",
    "CopernicusTile",
    "CoverageInfo",
    "DownloadResult",
    "ElevationProvider",
    "HttpTransportConfig",
    "NetworkAttempt",
    "ProviderAcquisition",
    "ProviderDescriptor",
    "ProviderEvaluation",
    "ProviderFetchAttempt",
    "ProviderFetchSelection",
    "ProviderSelectionError",
    "ProviderSelectionPolicy",
    "ProviderSelectionTrace",
    "evaluate_providers",
    "fetch_with_provider_selection",
    "list_provider_descriptors",
    "parse_s3_object_listing",
    "parse_tile_list",
    "required_tile_coordinates",
    "tile_id",
]
