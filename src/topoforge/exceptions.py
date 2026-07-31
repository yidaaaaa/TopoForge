"""Actionable exception hierarchy for TopoForge."""


class TopoForgeError(RuntimeError):
    """Base error for a failed TopoForge operation."""


class ConfigurationError(TopoForgeError):
    """Raised when build settings are internally inconsistent."""


class RasterProcessingError(TopoForgeError):
    """Raised when a raster cannot be interpreted without inventing data."""


class MeshValidationError(TopoForgeError):
    """Raised when generated geometry fails a required invariant."""


class ProviderFetchError(TopoForgeError):
    """Raised when a provider asset cannot be fetched and verified within bounds."""


class SlicerError(TopoForgeError):
    """Raised when an external slicer invocation fails."""
