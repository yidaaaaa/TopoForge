"""One-command loopback server launcher and installation checks."""

from __future__ import annotations

import ipaddress
import threading
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn

from topoforge.web.api import create_app, verify_static_assets
from topoforge.web.models import WebAppConfig


def is_loopback_host(host: str) -> bool:
    """Return whether a bind address is explicitly local-only."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def verify_web_installation(
    config: WebAppConfig,
    *,
    static_dir: Path | None = None,
) -> dict[str, Any]:
    """Verify packaged assets and configured local filesystem boundaries."""
    resolved = config.resolved()
    assets = verify_static_assets(static_dir)
    return {
        "status": "ok",
        "loopback_only": True,
        "state_dir": str(resolved.state_dir),
        "workspace_root": str(resolved.workspace_root),
        "input_roots": [str(path) for path in resolved.input_roots],
        "max_concurrent_jobs": resolved.max_concurrent_jobs,
        "assets": assets,
        "required_checks_passed": True,
    }


def run_web_server(
    config: WebAppConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    static_dir: Path | None = None,
) -> None:
    """Run the local application on an explicit loopback address."""
    if not is_loopback_host(host):
        raise ValueError("topoforge web accepts only localhost or loopback IP addresses")
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    resolved = config.resolved()
    resolved.state_dir.mkdir(parents=True, exist_ok=True)
    resolved.workspace_root.mkdir(parents=True, exist_ok=True)
    application = create_app(resolved, static_dir=static_dir)
    url_host = "[::1]" if host.strip("[]") == "::1" else host
    url = f"http://{url_host}:{port}/"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        application,
        host=host.strip("[]"),
        port=port,
        log_level="info",
        access_log=True,
    )
