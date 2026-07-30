"""Elevation provider protocol and provider registry."""

from topoforge.providers.protocol import CoverageInfo, ElevationProvider, ProviderDescriptor
from topoforge.providers.registry import list_provider_descriptors

__all__ = [
    "CoverageInfo",
    "ElevationProvider",
    "ProviderDescriptor",
    "list_provider_descriptors",
]
