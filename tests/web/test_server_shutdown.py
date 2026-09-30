"""Real CLI signal handling outside the thread-based Web startup tests."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from topoforge.web.models import WebAppConfig


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGINT delivery to the CLI process")
def test_web_cli_sigint_exits_cleanly(tmp_path: Path, web_config: WebAppConfig) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "topoforge.cli.app",
        "web",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--state-dir",
        str(web_config.state_dir),
        "--workspace-root",
        str(web_config.workspace_root),
        "--input-root",
        str(web_config.input_roots[0]),
        "--no-open",
    ]
    process = subprocess.Popen(
        command,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    health_url = f"http://127.0.0.1:{port}/api/v1/health"
    try:
        deadline = time.monotonic() + 60
        while True:
            assert process.poll() is None, "Web CLI exited before becoming healthy"
            try:
                with opener.open(health_url, timeout=1) as response:
                    health = json.load(response)
                if health.get("status") == "ok":
                    break
            except (OSError, urllib.error.URLError):
                pass
            assert time.monotonic() < deadline, "Web CLI did not become healthy"
            time.sleep(0.05)
        # Unlike a server running in a test thread, this exercises Uvicorn's
        # main-thread signal capture and Click/Typer's real interrupt handling.
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=30)
        (tmp_path / "web-stdout.log").write_text(stdout, encoding="utf-8")
        (tmp_path / "web-stderr.log").write_text(stderr, encoding="utf-8")
        output = stdout + stderr
        assert process.returncode == 0, output
        assert "Aborted!" not in output
        assert "Traceback" not in output
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
