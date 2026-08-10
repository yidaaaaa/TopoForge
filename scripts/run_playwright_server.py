#!/usr/bin/env python3
"""Start the persistent Playwright Web fixture without shell-specific paths."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.util import sha256_file
from topoforge.web import WebAppConfig, run_web_server


def default_runtime_root() -> Path:
    """Return the explicit or platform temporary Playwright runtime root."""
    configured = os.environ.get("TOPOFORGE_PLAYWRIGHT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(tempfile.gettempdir()) / "topoforge-playwright-v0.11").resolve()


def prepare_source(root: Path) -> Path:
    """Create or verify the deterministic Playwright source raster."""
    input_root = root / "input"
    input_root.mkdir(parents=True, exist_ok=True)
    source = input_root / "topoforge-playwright-input.tif"
    temporary = input_root / ".topoforge-playwright-input.creating.tif"
    temporary.unlink(missing_ok=True)
    create_synthetic_geotiff(
        temporary,
        SyntheticTerrain.SADDLE,
        rows=12,
        columns=16,
        pixel_size_m=20,
    )
    if source.is_file():
        if sha256_file(source) != sha256_file(temporary):
            temporary.unlink(missing_ok=True)
            raise RuntimeError(
                f"retained Playwright source checksum changed: {source}; "
                "remove the test runtime root and rerun"
            )
        temporary.unlink()
    else:
        temporary.replace(source)
    return source


def main() -> int:
    """Prepare one cross-platform persistent fixture and run the loopback server."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args()

    root = (
        args.runtime_root.expanduser().resolve()
        if args.runtime_root is not None
        else default_runtime_root()
    )
    source = prepare_source(root)
    config = WebAppConfig(
        state_dir=root / "state",
        workspace_root=root / "workspaces",
        input_roots=(source.parent,),
        max_concurrent_jobs=1,
    )
    run_web_server(
        config,
        host=args.host,
        port=args.port,
        open_browser=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
