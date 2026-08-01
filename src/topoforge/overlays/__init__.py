"""Local provenance-aware terrain overlay API."""

from topoforge.overlays.bundle import (
    OverlayBundleResult,
    generate_overlay_bundle,
    overlay_identity_payload,
    read_overlay_config,
    verify_overlay_bundle,
    write_overlay_config,
)
from topoforge.overlays.models import (
    OverlayConfig,
    OverlayFormat,
    OverlayKind,
    OverlayLayerRecord,
    OverlayManifest,
    OverlaySourceConfig,
    OverlaySourceRecord,
    OverlayStyle,
    OverlayValidation,
)

__all__ = [
    "OverlayBundleResult",
    "OverlayConfig",
    "OverlayFormat",
    "OverlayKind",
    "OverlayLayerRecord",
    "OverlayManifest",
    "OverlaySourceConfig",
    "OverlaySourceRecord",
    "OverlayStyle",
    "OverlayValidation",
    "generate_overlay_bundle",
    "overlay_identity_payload",
    "read_overlay_config",
    "verify_overlay_bundle",
    "write_overlay_config",
]
