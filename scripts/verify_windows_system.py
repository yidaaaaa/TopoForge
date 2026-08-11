#!/usr/bin/env python3
"""Verify native Web jobs, process recovery, backup, and artifact reopen."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Literal, cast

import topoforge
from topoforge.exporters.three_mf import ThreeMFInspection, inspect_3mf
from topoforge.models import BuildConfig, ResourceBudgetMode, SamplingMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.util import sha256_file
from topoforge.web.jobs import LocalJobManager
from topoforge.web.models import JobCreateRequest, JobRecord, JobState, WebAppConfig
from topoforge.web.processes import (
    process_group_id,
    process_identity,
    process_is_alive,
    terminate_process_tree,
    worker_process_options,
)
from topoforge.workflow import WorkflowLaunchConfig

_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))
import windows_acceptance as _windows_evidence  # noqa: E402

WINDOWS_TARGETS = _windows_evidence.WINDOWS_TARGETS
load_candidate_binding = _windows_evidence.load_candidate_binding
runtime_platform_record = _windows_evidence.runtime_platform_record
evidence_sha256_file = _windows_evidence.sha256_file
windows_host_record = _windows_evidence.windows_host_record
windows_target_record = _windows_evidence.windows_target_record
write_canonical_json = _windows_evidence.write_canonical_json

SCHEMA_VERSION = "topoforge-windows-system-verification-v2"
_TERMINAL_STATES = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
_HTTP_TERMINAL_STATES = {"completed", "failed", "cancelled"}
_HTTP_MAX_JSON_BYTES = 4 * 1024 * 1024
_HTTP_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_BROWSER_CALLBACK_PATH = "/__topoforge_browser_loaded__"
_BROWSER_CALLBACK_TIMEOUT_SECONDS = 20.0
_WINDOWS_CONTAINMENT_PROBE_CODE = r"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from topoforge.web.processes import (
    enable_current_process_containment,
    process_group_id,
    process_identity,
)

mode = sys.argv[1]
output_path = Path(sys.argv[2])
if mode not in {"leader-exit", "cancel"}:
    raise ValueError(f"unsupported containment probe mode: {mode}")

enable_current_process_containment()
child = subprocess.Popen(
    [sys.executable, "-I", "-X", "utf8", "-c", "import time; time.sleep(300)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
leader_identity = process_identity(os.getpid())
child_identity = process_identity(child.pid)
leader_group = process_group_id(os.getpid())
if leader_identity is None or child_identity is None or leader_group is None:
    raise RuntimeError("containment probe could not record stable process identities")
payload = {
    "containment_enabled": True,
    "leader_pid": os.getpid(),
    "leader_process_identity": leader_identity,
    "leader_process_group_id": leader_group,
    "child_pid": child.pid,
    "child_process_identity": child_identity,
}
temporary_path = None
try:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
        delete=False,
    ) as stream:
        temporary_path = Path(stream.name)
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_path, output_path)
    temporary_path = None
finally:
    if temporary_path is not None:
        temporary_path.unlink(missing_ok=True)
if mode == "leader-exit":
    raise SystemExit(0)
time.sleep(300)
"""
_WINDOWS_SERVER_CONTAINMENT_WRAPPER_CODE = r"""
import subprocess
import sys

from topoforge.web.processes import enable_current_process_containment

if len(sys.argv) < 2:
    raise RuntimeError("contained Web server command is missing")
enable_current_process_containment()
child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)
raise SystemExit(child.wait())
"""


def _job_request(config: WebAppConfig, *, name: str) -> JobCreateRequest:
    input_root = config.input_roots[0]
    source = input_root / f"{name} terrain.tif"
    create_synthetic_geotiff(
        source,
        SyntheticTerrain.SADDLE,
        rows=12,
        columns=16,
        pixel_size_m=20.0,
    )
    workspace = config.workspace_root / f"{name} workspace"
    return JobCreateRequest(
        launch=WorkflowLaunchConfig(
            workspace_dir=workspace,
            build=BuildConfig(
                dem_path=source,
                output_dir=workspace,
                model_width_mm=40.0,
                max_height_mm=20.0,
                sampling_mode=SamplingMode.SOURCE_PRESERVING,
                max_grid_cells=10_000,
                max_estimated_triangles=50_000,
                resource_budget_mode=ResourceBudgetMode.STRICT,
            ),
            maximum_tile_width_mm=180.0,
            maximum_tile_depth_mm=180.0,
            slicing_enabled=False,
        )
    )


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _http_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    maximum_bytes: int = _HTTP_MAX_JSON_BYTES,
    timeout_seconds: float = 10.0,
) -> tuple[int, bytes, dict[str, str]]:
    body = None
    headers = {"Accept": "application/json, text/html"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > maximum_bytes:
                raise RuntimeError(f"HTTP response exceeds its byte bound: {url}")
            data = response.read(maximum_bytes + 1)
            if len(data) > maximum_bytes:
                raise RuntimeError(f"HTTP response exceeds its byte bound: {url}")
            return response.status, data, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        detail = exc.read(64 * 1024).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    status, raw, _ = _http_request(
        url,
        method=method,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"HTTP endpoint did not return JSON: {url}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"HTTP endpoint returned a non-object: {url}")
    return status, value


def _server_command(
    *,
    web_launcher: Path | None,
    config: WebAppConfig,
    port: int,
    hosted_server: bool,
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    arguments = [
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--state-dir",
        str(config.state_dir),
        "--workspace-root",
        str(config.workspace_root),
        "--input-root",
        str(config.input_roots[0]),
        "--max-concurrent-jobs",
        "1",
        "--no-open",
    ]
    if platform.system() == "Windows":
        process_options: dict[str, Any] = {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        }
        if web_launcher is None:
            if not hosted_server:
                raise RuntimeError(
                    "clean Windows system acceptance requires --web-launcher pointing to the "
                    "candidate TopoForge-Web.cmd"
                )
            contained_command = [
                sys.executable,
                "-m",
                "topoforge.cli.app",
                "web",
                *arguments,
            ]
            launcher_record = {
                "kind": "hosted-python-module",
                "path": sys.executable,
                "sha256": evidence_sha256_file(Path(sys.executable)),
                "launcher_no_open": True,
                "target_evidence": False,
            }
        else:
            launcher = web_launcher.expanduser().resolve()
            if not launcher.is_file() or launcher.name.casefold() != "topoforge-web.cmd":
                raise RuntimeError(f"candidate TopoForge-Web.cmd is missing: {launcher}")
            command_line = subprocess.list2cmdline([str(launcher), *arguments])
            contained_command = [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/s",
                "/c",
                command_line,
            ]
            launcher_record = {
                "kind": "candidate-batch-launcher",
                "path": str(launcher),
                "sha256": evidence_sha256_file(launcher),
                "launcher_no_open": True,
            }
        command = [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-c",
            _WINDOWS_SERVER_CONTAINMENT_WRAPPER_CODE,
            *contained_command,
        ]
        launcher_record.update(
            {
                "containment": "kill-on-close-job-wrapper",
                "contained_process_tree": True,
            }
        )
    else:
        command = [sys.executable, "-m", "topoforge.cli.app", "web", *arguments]
        process_options = {"start_new_session": True}
        launcher_record = {
            "kind": "python-module-contract-only",
            "path": None,
            "sha256": None,
            "launcher_no_open": True,
        }
    return command, process_options, launcher_record


def _stop_server(
    process: subprocess.Popen[bytes],
    *,
    expected_identity: str | None,
    process_group: int | None,
) -> dict[str, Any]:
    if process.poll() is not None:
        return {"method": "already-exited", "exit_code": process.returncode}
    if expected_identity is None or process_group is None:
        process.kill()
        process.wait(timeout=15.0)
        return {"method": "exact-child-fallback", "exit_code": process.returncode}
    try:
        terminate_process_tree(
            process.pid,
            expected_identity=expected_identity,
            process_group=process_group,
        )
        process.wait(timeout=15.0)
    except Exception:
        if process.poll() is None:
            process.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=15.0)
        raise
    return {"method": "identity-bound-process-tree", "exit_code": process.returncode}


def _wait_for_server(base_url: str, process: subprocess.Popen[bytes]) -> dict[str, Any]:
    deadline = time.monotonic() + 45.0
    last_error = "server did not answer"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"TopoForge Web server exited before health check: {process.returncode}"
            )
        try:
            status, health = _http_json(f"{base_url}/api/v1/health", timeout_seconds=2.0)
            if status == 200 and health.get("status") == "ok":
                return health
            last_error = f"unexpected health response: {status} {health}"
        except (OSError, RuntimeError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise TimeoutError(f"TopoForge Web health endpoint was not ready: {last_error}")


def _wait_for_port_closed(port: int) -> None:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
        except OSError:
            return
        time.sleep(0.2)
    raise RuntimeError(f"Web loopback port {port} still accepts connections after shutdown")


def _download_artifact(url: str, destination: Path, expected_sha256: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    digest = hashlib.sha256()
    received = 0
    with (
        urllib.request.urlopen(request, timeout=30.0) as response,
        destination.open("xb") as output,
    ):
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > _HTTP_MAX_ARTIFACT_BYTES:
            raise RuntimeError("HTTP model_3mf exceeds its download byte bound")
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            received += len(chunk)
            if received > _HTTP_MAX_ARTIFACT_BYTES:
                raise RuntimeError("HTTP model_3mf exceeds its download byte bound")
            digest.update(chunk)
            output.write(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError("downloaded HTTP model_3mf SHA-256 differs from the JobRecord")
    inspection = inspect_3mf(destination)
    if inspection.strict_warning_count != 0:
        raise RuntimeError("downloaded HTTP model_3mf has strict-reader warnings")
    return {
        "path": str(destination),
        "sha256": actual_sha256,
        "bytes": received,
        "three_mf": _three_mf_report(inspection),
        "required_checks_passed": True,
    }


def _launch_default_browser(url: str) -> None:
    if not bool(webbrowser.open(url, new=0, autoraise=True)):
        raise RuntimeError(
            "default browser launch returned false; configure a default browser and rerun"
        )


def _confirm_default_browser_load(
    target_url: str,
    *,
    timeout_seconds: float = _BROWSER_CALLBACK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Dispatch the default browser and require its one-time loopback callback."""
    if timeout_seconds <= 0.0:
        raise ValueError("browser callback timeout must be positive")
    target = urllib.parse.urlsplit(target_url)
    try:
        target_port = target.port
    except ValueError as exc:
        raise ValueError("browser target URL has an invalid port") from exc
    if (
        target.scheme != "http"
        or target.hostname != "127.0.0.1"
        or target_port is None
        or target.username is not None
        or target.password is not None
        or target.query
        or target.fragment
    ):
        raise ValueError(
            "browser target URL must be an explicit http://127.0.0.1:<port> URL "
            "without credentials, query, or fragment"
        )

    nonce = secrets.token_urlsafe(32)
    nonce_sha256 = hashlib.sha256(nonce.encode("ascii")).hexdigest()
    confirmed = threading.Event()
    callback_lock = threading.Lock()
    callback_record: dict[str, Any] = {}
    callback_claimed = False

    class BrowserCallbackHandler(http.server.BaseHTTPRequestHandler):
        server_version = "TopoForgeBrowserCallback/1"
        sys_version = ""

        def do_GET(self) -> None:
            nonlocal callback_claimed
            request = urllib.parse.urlsplit(self.path)
            try:
                query = urllib.parse.parse_qs(
                    request.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=2,
                )
            except ValueError:
                query = {}
            exact_callback = request.path == _BROWSER_CALLBACK_PATH and query == {"nonce": [nonce]}
            with callback_lock:
                if exact_callback and not callback_claimed:
                    callback_claimed = True
                    accepted = True
                    status = 302
                elif exact_callback:
                    accepted = False
                    status = 410
                else:
                    accepted = False
                    status = 404
            if not accepted:
                self.send_error(status)
                return

            self.send_response(status)
            self.send_header("Location", target_url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            callback_record.update(
                {
                    "request_method": self.command,
                    "request_path": request.path,
                    "remote_address": self.client_address[0],
                    "redirect_target": target_url,
                }
            )
            confirmed.set()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        BrowserCallbackHandler,
    )
    server.daemon_threads = True
    callback_port = int(server.server_address[1])
    callback_origin = f"http://127.0.0.1:{callback_port}"
    callback_url = (
        callback_origin + _BROWSER_CALLBACK_PATH + "?" + urllib.parse.urlencode({"nonce": nonce})
    )
    server_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name="topoforge-browser-confirmation",
        daemon=True,
    )
    server_thread.start()
    started_at = time.monotonic()
    try:
        _launch_default_browser(callback_url)
        if not confirmed.wait(timeout_seconds):
            raise TimeoutError(
                "default browser dispatch was accepted but no valid nonce callback was "
                f"received within {timeout_seconds:.1f} seconds; verify that the default "
                "browser can load loopback HTTP URLs and rerun"
            )
        elapsed_seconds = time.monotonic() - started_at
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)
    if server_thread.is_alive():
        raise RuntimeError("browser confirmation callback server did not stop")
    if not callback_record:
        raise RuntimeError("browser confirmation event had no callback record")

    return {
        "mode": "require",
        "attempted": True,
        "opened": True,
        "url": target_url,
        "dispatch": {
            "attempted": True,
            "accepted": True,
            "required_checks_passed": True,
        },
        "confirmed_load": {
            "required": True,
            "confirmed": True,
            "one_time_nonce": True,
            "nonce_sha256": nonce_sha256,
            "callback_origin": callback_origin,
            "callback_timeout_seconds": timeout_seconds,
            "elapsed_seconds": elapsed_seconds,
            **callback_record,
            "required_checks_passed": True,
        },
        "launcher_no_open_is_not_browser_evidence": True,
        "required_checks_passed": True,
    }


def _real_http_web_acceptance(
    config: WebAppConfig,
    *,
    web_launcher: Path | None,
    browser_mode: Literal["skip", "require"],
    hosted_server: bool,
) -> dict[str, Any]:
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    command, process_options, launcher_record = _server_command(
        web_launcher=web_launcher,
        config=config,
        port=port,
        hosted_server=hosted_server,
    )
    log_path = config.state_dir.parent / "real-http-web-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    shutdown: dict[str, Any] | None = None
    with log_path.open("xb") as log:
        process = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(
                command,
                cwd=(web_launcher.resolve().parent if web_launcher is not None else Path.cwd()),
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1"},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **process_options,
            ),
        )
        server_identity: str | None = None
        server_group: int | None = None
        try:
            server_identity = process_identity(process.pid)
            server_group = process_group_id(process.pid)
            if server_identity is None or server_group is None:
                raise RuntimeError("Web server has no stable process identity or containment group")
            health = _wait_for_server(base_url, process)
            root_status, root, root_headers = _http_request(base_url + "/")
            if root_status != 200 or b"<!doctype html" not in root.lower():
                raise RuntimeError("TopoForge Web root did not serve the packaged application")

            if browser_mode == "require":
                browser = _confirm_default_browser_load(base_url + "/")
            else:
                browser = {
                    "mode": "skip",
                    "attempted": False,
                    "opened": None,
                    "url": base_url + "/",
                    "dispatch": {
                        "attempted": False,
                        "accepted": None,
                        "required_checks_passed": True,
                    },
                    "confirmed_load": {
                        "required": False,
                        "confirmed": None,
                        "reason": "hosted/headless automation; browser confirmation not required",
                        "required_checks_passed": True,
                    },
                    "reason": "hosted/headless automation; not clean-VM browser evidence",
                    "launcher_no_open_is_not_browser_evidence": True,
                    "required_checks_passed": True,
                }

            request = _job_request(config, name="real-http")
            create_status, submitted = _http_json(
                f"{base_url}/api/v1/jobs",
                method="POST",
                payload=request.model_dump(mode="json"),
                timeout_seconds=30.0,
            )
            if create_status != 201 or not isinstance(submitted.get("job_id"), str):
                raise RuntimeError("HTTP synthetic job submission did not return a JobRecord")
            job_id = str(submitted["job_id"])
            deadline = time.monotonic() + 240.0
            terminal = submitted
            while time.monotonic() < deadline:
                _, terminal = _http_json(f"{base_url}/api/v1/jobs/{job_id}")
                state = terminal.get("state")
                if state in _HTTP_TERMINAL_STATES:
                    break
                time.sleep(0.2)
            else:
                raise TimeoutError("HTTP synthetic Web job did not reach a terminal state")
            if terminal.get("state") != "completed":
                raise RuntimeError(
                    f"HTTP synthetic Web job finished as {terminal.get('state')}: "
                    f"{terminal.get('error')}"
                )
            artifacts = terminal.get("artifacts")
            if not isinstance(artifacts, list):
                raise RuntimeError("completed HTTP JobRecord has no artifacts list")
            model = next(
                (
                    item
                    for item in artifacts
                    if isinstance(item, dict) and item.get("artifact_id") == "model_3mf"
                ),
                None,
            )
            if not isinstance(model, dict):
                raise RuntimeError("completed HTTP JobRecord omitted model_3mf")
            expected_sha256 = model.get("sha256")
            download_url = model.get("download_url")
            if not isinstance(expected_sha256, str) or not isinstance(download_url, str):
                raise RuntimeError("HTTP model_3mf has no checksum or download URL")
            downloaded = _download_artifact(
                base_url + download_url,
                config.state_dir.parent / "downloaded-http-model.3mf",
                expected_sha256,
            )
        finally:
            shutdown = _stop_server(
                process,
                expected_identity=server_identity,
                process_group=server_group,
            )
    _wait_for_port_closed(port)
    return {
        "base_url": base_url,
        "launcher": launcher_record,
        "command": command,
        "health": health,
        "root": {
            "status": root_status,
            "content_type": root_headers.get("Content-Type"),
            "bytes": len(root),
            "packaged_application_served": True,
        },
        "browser": browser,
        "job": {
            "job_id": job_id,
            "state": terminal["state"],
            "expected_stages": terminal.get("expected_stages"),
            "ready_stages": terminal.get("ready_stages"),
            "model_3mf_sha256": expected_sha256,
            "required_checks_passed": True,
        },
        "download": downloaded,
        "shutdown": {
            **(shutdown or {}),
            "port": port,
            "port_closed": True,
            "log": str(log_path),
            "log_sha256": evidence_sha256_file(log_path),
            "required_checks_passed": True,
        },
        "required_checks_passed": True,
    }


def _completed_process_detail(process: subprocess.Popen[bytes]) -> str:
    stdout, stderr = process.communicate(timeout=1.0)
    parts = []
    for label, value in (("stdout", stdout), ("stderr", stderr)):
        if value:
            decoded = value.decode("utf-8", errors="replace").strip()
            if decoded:
                parts.append(f"{label}={decoded[-8192:]}")
    return "; ".join(parts) or "no subprocess output"


def _wait_for_containment_probe_record(
    path: Path,
    process: subprocess.Popen[bytes],
) -> dict[str, Any]:
    deadline = time.monotonic() + 20.0
    last_error = "probe record was not created"
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                last_error = f"probe record could not be reopened: {exc}"
            else:
                if not isinstance(value, dict):
                    last_error = "probe record root is not an object"
                else:
                    integer_fields = (
                        "leader_pid",
                        "leader_process_group_id",
                        "child_pid",
                    )
                    integers_valid = all(
                        isinstance(value.get(field), int)
                        and not isinstance(value.get(field), bool)
                        and int(value[field]) > 0
                        for field in integer_fields
                    )
                    identities_valid = all(
                        isinstance(value.get(field), str) and bool(value[field])
                        for field in (
                            "leader_process_identity",
                            "child_process_identity",
                        )
                    )
                    if (
                        value.get("containment_enabled") is True
                        and integers_valid
                        and identities_valid
                        and value["leader_pid"] == value["leader_process_group_id"]
                        and value["leader_pid"] != value["child_pid"]
                    ):
                        return value
                    last_error = "probe record process identities are invalid"
        if process.poll() is not None:
            detail = _completed_process_detail(process)
            raise RuntimeError(
                f"Windows containment probe exited before a valid record: {detail}; {last_error}"
            )
        time.sleep(0.05)
    raise TimeoutError(f"Windows containment probe did not become ready: {last_error}")


def _recorded_process_is_alive(pid: int, expected_identity: str) -> bool:
    observed_identity = process_identity(pid)
    if observed_identity is None:
        if process_is_alive(pid):
            raise RuntimeError(
                f"process {pid} is alive but its OS-issued identity cannot be verified"
            )
        return False
    return observed_identity == expected_identity


def _wait_for_recorded_process_exit(
    pid: int,
    expected_identity: str,
    *,
    timeout_seconds: float = 15.0,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while _recorded_process_is_alive(pid, expected_identity):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _run_windows_containment_mode(
    probe_root: Path, mode: Literal["leader-exit", "cancel"]
) -> dict[str, Any]:
    probe_root.mkdir(parents=True, exist_ok=False)
    record_path = probe_root / "process-record.json"
    process: subprocess.Popen[bytes] | None = None
    launched_identity: str | None = None
    launched_group: int | None = None
    record: dict[str, Any] | None = None
    try:
        launched_process = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    "-c",
                    _WINDOWS_CONTAINMENT_PROBE_CODE,
                    mode,
                    str(record_path),
                ],
                cwd=probe_root,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **worker_process_options(),
            ),
        )
        process = launched_process
        launched_identity = process_identity(launched_process.pid)
        launched_group = process_group_id(launched_process.pid)
        if launched_identity is None or launched_group is None:
            raise RuntimeError("containment probe leader has no stable process identity")
        record = _wait_for_containment_probe_record(record_path, launched_process)
        if (
            record["leader_pid"] != launched_process.pid
            or record["leader_process_identity"] != launched_identity
            or record["leader_process_group_id"] != launched_group
        ):
            raise RuntimeError("containment probe leader identity changed before verification")

        child_pid = int(record["child_pid"])
        child_identity = str(record["child_process_identity"])
        if mode == "leader-exit":
            try:
                exit_code = launched_process.wait(timeout=15.0)
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("containment probe leader did not exit normally") from exc
            if exit_code != 0:
                raise RuntimeError(
                    "containment probe leader failed before normal exit: "
                    f"{_completed_process_detail(launched_process)}"
                )
            if not _wait_for_recorded_process_exit(child_pid, child_identity):
                raise RuntimeError(
                    "child remained alive after its Job Object leader exited normally"
                )
            return {
                **record,
                "mode": mode,
                "leader_exit_code": exit_code,
                "leader_alive_after_exit": False,
                "child_alive_after_exit": False,
                "kill_on_job_close_verified": True,
                "required_checks_passed": True,
            }

        if launched_process.poll() is not None:
            raise RuntimeError(
                "containment probe leader exited before cancellation: "
                f"{_completed_process_detail(launched_process)}"
            )
        terminate_process_tree(
            launched_process.pid,
            expected_identity=launched_identity,
            process_group=launched_group,
        )
        try:
            exit_code = launched_process.wait(timeout=15.0)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("cancelled containment probe leader remained alive") from exc
        if not _wait_for_recorded_process_exit(child_pid, child_identity):
            raise RuntimeError("containment probe child remained alive after cancellation")
        return {
            **record,
            "mode": mode,
            "leader_exit_code": exit_code,
            "leader_alive_after_cancel": False,
            "child_alive_after_cancel": False,
            "production_termination_adapter_exercised": True,
            "required_checks_passed": True,
        }
    finally:
        if process is not None and process.poll() is None:
            if launched_identity is not None:
                with contextlib.suppress(Exception):
                    terminate_process_tree(
                        process.pid,
                        expected_identity=launched_identity,
                        process_group=launched_group,
                    )
            else:
                with contextlib.suppress(Exception):
                    process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=15.0)
        if record is not None:
            child_pid = int(record["child_pid"])
            child_identity = str(record["child_process_identity"])
            with contextlib.suppress(Exception):
                if _recorded_process_is_alive(child_pid, child_identity):
                    terminate_process_tree(
                        child_pid,
                        expected_identity=child_identity,
                        process_group=child_pid,
                    )


def _windows_process_containment_acceptance(
    probe_root: Path,
    candidate_binding: dict[str, Any] | None,
) -> dict[str, Any]:
    system_verifier_sha256 = evidence_sha256_file(Path(__file__).resolve())
    candidate_verifier_sha256 = (
        None if candidate_binding is None else candidate_binding.get("verifier_sha256")
    )
    if candidate_binding is not None and candidate_verifier_sha256 != system_verifier_sha256:
        raise RuntimeError("containment acceptance verifier differs from candidate binding")
    source_binding = {
        "candidate_bound": candidate_binding is not None,
        "candidate_binding_sha256": (
            None if candidate_binding is None else candidate_binding.get("binding_sha256")
        ),
        "source_commit": (
            None if candidate_binding is None else candidate_binding.get("source_commit")
        ),
        "system_verifier_sha256": system_verifier_sha256,
        "system_verifier_matches_candidate": (
            candidate_binding is not None and candidate_verifier_sha256 == system_verifier_sha256
        ),
        "required_checks_passed": True,
    }
    common = {
        "platform": platform.system(),
        "containment_entrypoint": ("topoforge.web.processes.enable_current_process_containment"),
        "probe_code_sha256": hashlib.sha256(
            _WINDOWS_CONTAINMENT_PROBE_CODE.encode("utf-8")
        ).hexdigest(),
        "source_binding": source_binding,
    }
    if platform.system() != "Windows":
        return {
            **common,
            "executed": False,
            "job_object_kill_on_close_verified": False,
            "production_cancellation_verified": False,
            "claim_boundary": (
                "non-Windows contract execution is not native Windows Job Object evidence"
            ),
            "required_checks_passed": True,
        }

    leader_exit = _run_windows_containment_mode(probe_root / "leader exit", "leader-exit")
    cancellation = _run_windows_containment_mode(probe_root / "cancellation", "cancel")
    return {
        **common,
        "executed": True,
        "leader_exit": leader_exit,
        "cancellation": cancellation,
        "job_object_kill_on_close_verified": (
            leader_exit.get("kill_on_job_close_verified") is True
            and leader_exit.get("child_alive_after_exit") is False
        ),
        "production_cancellation_verified": (
            cancellation.get("production_termination_adapter_exercised") is True
            and cancellation.get("child_alive_after_cancel") is False
        ),
        "required_checks_passed": True,
    }


def _wait_for_terminal(
    manager: LocalJobManager,
    job_id: str,
    *,
    timeout_seconds: float,
) -> JobRecord:
    deadline = time.monotonic() + timeout_seconds
    last = manager.get(job_id)
    while time.monotonic() < deadline:
        last = manager.get(job_id)
        if last.state in _TERMINAL_STATES:
            return last
        time.sleep(manager.config.poll_interval_seconds)
    raise TimeoutError(
        f"Web job {job_id} remained {last.state.value!r} after {timeout_seconds} seconds"
    )


def _three_mf_report(inspection: ThreeMFInspection) -> dict[str, Any]:
    return {
        "unit": inspection.unit,
        "object_count": inspection.object_count,
        "build_item_count": inspection.build_item_count,
        "vertex_count": inspection.vertex_count,
        "triangle_count": inspection.triangle_count,
        "dimensions_mm": list(inspection.dimensions_mm),
        "strict_warning_count": inspection.strict_warning_count,
        "lib3mf_version": list(inspection.lib3mf_version),
    }


def _stop_active_job(manager: LocalJobManager, job_id: str | None) -> None:
    if job_id is None:
        return
    with contextlib.suppress(Exception):
        record = manager.get(job_id)
        if record.state in {JobState.QUEUED, JobState.RUNNING, JobState.CANCELLING}:
            manager.cancel(job_id)
            _wait_for_terminal(manager, job_id, timeout_seconds=20.0)


def _complete_recover_backup_restore(config: WebAppConfig) -> dict[str, Any]:
    submitted_id: str | None = None
    first = LocalJobManager(config)
    first.start()
    try:
        submitted = first.submit(_job_request(config, name="completed"))
        submitted_id = submitted.job_id
        completed = _wait_for_terminal(first, submitted.job_id, timeout_seconds=180.0)
        if completed.state is not JobState.COMPLETED:
            detail = completed.error.message if completed.error is not None else "no error detail"
            raise RuntimeError(f"native Web job finished as {completed.state.value}: {detail}")
        if completed.summary is None or completed.summary.required_checks_passed is not True:
            raise RuntimeError("native Web job has no verified workflow summary")
        if completed.ready_stages != completed.expected_stages:
            raise RuntimeError("native Web job did not complete every expected stage")
    finally:
        _stop_active_job(first, submitted_id)
        first.close()

    recovered_manager = LocalJobManager(config)
    recovered_manager.start()
    try:
        recovered = recovered_manager.get(completed.job_id)
        if recovered.state is not JobState.COMPLETED or recovered.summary is None:
            raise RuntimeError("completed Web job did not recover after manager restart")

        model_path, model_artifact = recovered_manager.artifact_path(
            recovered.job_id,
            "model_3mf",
        )
        if model_artifact.sha256 is None:
            raise RuntimeError("completed Web job 3MF has no recorded SHA-256")
        if sha256_file(model_path) != model_artifact.sha256:
            raise RuntimeError("completed Web job 3MF SHA-256 changed")
        inspection = inspect_3mf(model_path)

        backup = recovered_manager.create_backup(recovered.job_id)
        backup_path, reopened_backup = recovered_manager.backup_archive_path(backup.backup_id)
        if (
            not backup.required_checks_passed
            or reopened_backup != backup
            or sha256_file(backup_path) != backup.archive_sha256
        ):
            raise RuntimeError("completed Web job backup did not strictly reopen")

        restored = recovered_manager.restore_backup(
            backup.backup_id,
            workspace_name="restored-system-workspace",
        )
        if restored.state is not JobState.COMPLETED or restored.summary is None:
            raise RuntimeError("restored Web job is not completed")
        if restored.summary.workflow_id != recovered.summary.workflow_id:
            raise RuntimeError("restored Web workflow identity changed")
        restored_path, restored_artifact = recovered_manager.artifact_path(
            restored.job_id,
            "model_3mf",
        )
        if restored_artifact.sha256 != model_artifact.sha256:
            raise RuntimeError("restored Web 3MF checksum differs from the original")
        restored_inspection = inspect_3mf(restored_path)
        if restored_inspection.dimensions_mm != inspection.dimensions_mm:
            raise RuntimeError("restored Web 3MF dimensions differ from the original")

        events = recovered_manager.read_events(recovered.job_id)
        restored_events = recovered_manager.read_events(restored.job_id)
        if not events or events[-1].message_key != "job.completed":
            raise RuntimeError("completed Web job event log did not recover")
        if not restored_events or restored_events[-1].message_key != "job.restored":
            raise RuntimeError("restored Web job event log is incomplete")

        return {
            "completed_job": {
                "job_id": recovered.job_id,
                "workflow_id": recovered.summary.workflow_id,
                "workspace": str(recovered.workspace_dir),
                "exit_code": recovered.exit_code,
                "expected_stages": [stage.value for stage in recovered.expected_stages],
                "ready_stages": [stage.value for stage in recovered.ready_stages],
                "artifact_sha256": model_artifact.sha256,
                "three_mf": _three_mf_report(inspection),
                "event_count": len(events),
                "required_checks_passed": True,
            },
            "restart_recovery": {
                "state": recovered.state.value,
                "summary_reopened": True,
                "artifact_reopened": True,
                "required_checks_passed": True,
            },
            "backup_restore": {
                "backup_id": backup.backup_id,
                "archive_sha256": backup.archive_sha256,
                "archive_size_bytes": backup.archive_size_bytes,
                "file_count": backup.file_count,
                "restored_job_id": restored.job_id,
                "restored_workspace": str(restored.workspace_dir),
                "restored_artifact_sha256": restored_artifact.sha256,
                "restored_three_mf": _three_mf_report(restored_inspection),
                "required_checks_passed": True,
            },
        }
    finally:
        recovered_manager.close()


class _SlowJobManager(LocalJobManager):
    def _start_job(self, record: JobRecord) -> None:
        process = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    "-c",
                    "import time; time.sleep(300)",
                ],
                cwd=self.config.workspace_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **worker_process_options(),
            ),
        )
        self._processes[record.job_id] = process
        identity = process_identity(process.pid)
        group = process_group_id(process.pid)
        if identity is None or group is None:
            process.kill()
            process.wait(timeout=15.0)
            raise RuntimeError("slow acceptance worker has no verifiable process identity")
        self._write_record(
            record.model_copy(
                update={
                    "state": JobState.RUNNING,
                    "pid": process.pid,
                    "process_identity": identity,
                    "process_group_id": group,
                    "current_stage": record.expected_stages[0],
                }
            ),
            message_key="job.started",
        )


def _restart_and_cancel_worker(config: WebAppConfig) -> dict[str, Any]:
    first = _SlowJobManager(config)
    recovered: LocalJobManager | None = None
    process: subprocess.Popen[bytes] | None = None
    submitted_id: str | None = None
    worker_identity: str | None = None
    worker_group: int | None = None
    first.start()
    try:
        submitted = first.submit(_job_request(config, name="cancelled"))
        submitted_id = submitted.job_id
        process = first._processes[submitted.job_id]
        running = first.get(submitted.job_id)
        if running.state is not JobState.RUNNING or running.pid != process.pid:
            raise RuntimeError("isolated cancellation worker did not enter running state")
        worker_identity = running.process_identity
        worker_group = running.process_group_id
        pid = process.pid

        first.close()
        recovered = LocalJobManager(config)
        recovered.start()
        recovered_running = recovered.get(submitted.job_id)
        if recovered_running.state is not JobState.RUNNING or recovered_running.pid != pid:
            raise RuntimeError("running worker did not recover after manager restart")

        cancelling = recovered.cancel(submitted.job_id)
        if cancelling.state is not JobState.CANCELLING:
            raise RuntimeError("recovered worker did not enter cancelling state")
        cancelled = _wait_for_terminal(recovered, submitted.job_id, timeout_seconds=30.0)
        if cancelled.state is not JobState.CANCELLED:
            raise RuntimeError(f"recovered worker finished as {cancelled.state.value}")
        process.wait(timeout=15.0)
        if process_is_alive(pid):
            raise RuntimeError("cancelled worker process remains alive")

        events = recovered.read_events(submitted.job_id)
        keys = [event.message_key for event in events]
        required_keys = {"job.queued", "job.started", "job.cancelling", "job.cancelled"}
        if not required_keys <= set(keys):
            raise RuntimeError(f"worker lifecycle event log is incomplete: {keys}")
        return {
            "job_id": submitted.job_id,
            "pid": pid,
            "worker_options": worker_process_options(),
            "recovered_state": recovered_running.state.value,
            "cancelling_state": cancelling.state.value,
            "terminal_state": cancelled.state.value,
            "process_alive_after_cancel": False,
            "event_keys": keys,
            "required_checks_passed": True,
        }
    finally:
        if recovered is not None:
            recovered.close()
        else:
            _stop_active_job(first, submitted_id)
        first.close()
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                if worker_identity is not None and worker_group is not None:
                    terminate_process_tree(
                        process.pid,
                        expected_identity=worker_identity,
                        process_group=worker_group,
                    )
                else:
                    process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=15.0)


def verify_windows_system(
    work_root: Path,
    *,
    require_windows: bool = False,
    expected_target: str | None = None,
    web_launcher: Path | None = None,
    browser_mode: Literal["skip", "require"] = "skip",
    candidate_binding: Path | None = None,
    hosted_server: bool = False,
) -> dict[str, Any]:
    """Run a real HTTP Web job plus native recovery and artifact acceptance."""
    if browser_mode not in {"skip", "require"}:
        raise ValueError("browser_mode must be 'skip' or 'require'")
    if require_windows and expected_target is None and not hosted_server:
        raise RuntimeError(
            "--require-windows requires --expected-target, or explicit --hosted-server "
            "for non-release Server evidence"
        )
    if hosted_server and expected_target is not None:
        raise RuntimeError("--hosted-server cannot be combined with --expected-target")
    if expected_target is not None and browser_mode != "require":
        raise RuntimeError("clean --expected-target acceptance requires --browser-mode require")
    target = (
        windows_host_record(require_windows=require_windows)
        if expected_target is None
        else windows_target_record(expected_target, require_windows=True)
    )
    if expected_target is not None and candidate_binding is None:
        raise RuntimeError(
            "clean --expected-target acceptance requires --candidate-binding from the "
            "portable archive verifier"
        )
    if (
        (require_windows or expected_target is not None)
        and web_launcher is None
        and not hosted_server
    ):
        raise RuntimeError(
            "clean target acceptance requires --web-launcher pointing to candidate "
            "TopoForge-Web.cmd"
        )
    binding = (
        None
        if candidate_binding is None
        else load_candidate_binding(
            candidate_binding,
            verifier_role="system",
            verifier_path=Path(__file__),
            expected_target=expected_target or "hosted-server",
        )
    )

    root = work_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    path_probe = root / "system path with spaces" / "地形"
    input_root = path_probe / "inputs"
    input_root.mkdir(parents=True)
    config = WebAppConfig(
        state_dir=path_probe / "state",
        workspace_root=path_probe / "workspaces",
        input_roots=(input_root,),
        max_concurrent_jobs=1,
        poll_interval_seconds=0.05,
    )

    real_http = _real_http_web_acceptance(
        config,
        web_launcher=web_launcher,
        browser_mode=browser_mode,
        hosted_server=hosted_server,
    )
    completed = _complete_recover_backup_restore(config)
    process_lifecycle = _restart_and_cancel_worker(config)
    windows_process_containment = _windows_process_containment_acceptance(
        root / "windows process containment",
        binding,
    )
    platform_record = runtime_platform_record(
        require_windows=require_windows or expected_target is not None
    )
    platform_record["topoforge"] = topoforge.__version__
    platform_record["target"] = target
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform_record,
        "expected_target": target.get("target_id"),
        "windows_target": target,
        "hosted_server": hosted_server,
        "candidate_binding": binding,
        "path_contract": {
            "root": str(path_probe),
            "contains_spaces": " " in str(path_probe),
            "contains_non_ascii": any(ord(character) > 127 for character in str(path_probe)),
            "required_checks_passed": True,
        },
        "real_http_web": real_http,
        **completed,
        "process_lifecycle": process_lifecycle,
        "windows_process_containment": windows_process_containment,
        "claim_boundary": (
            "native target evidence only when windows_target.target_verified is true; "
            "browser mode skip is hosted/headless evidence and never proves default-browser launch"
        ),
        "required_checks_passed": True,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    write_canonical_json(path, report)


def main() -> int:
    """Run native system acceptance and retain success or failure evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--require-windows", action="store_true")
    parser.add_argument("--expected-target", choices=WINDOWS_TARGETS)
    parser.add_argument("--web-launcher", type=Path)
    parser.add_argument("--browser-mode", choices=("skip", "require"), default="skip")
    parser.add_argument("--candidate-binding", type=Path)
    parser.add_argument("--hosted-server", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report_path = args.report.expanduser().resolve()
    try:
        if args.work_root is not None:
            report = verify_windows_system(
                args.work_root,
                require_windows=args.require_windows,
                expected_target=args.expected_target,
                web_launcher=args.web_launcher,
                browser_mode=args.browser_mode,
                candidate_binding=args.candidate_binding,
                hosted_server=args.hosted_server,
            )
        else:
            with tempfile.TemporaryDirectory(prefix="topoforge-windows-system-") as temporary:
                report = verify_windows_system(
                    Path(temporary) / "verification",
                    require_windows=args.require_windows,
                    expected_target=args.expected_target,
                    web_launcher=args.web_launcher,
                    browser_mode=args.browser_mode,
                    candidate_binding=args.candidate_binding,
                    hosted_server=args.hosted_server,
                )
    except Exception as exc:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "python_executable": sys.executable,
                "topoforge": topoforge.__version__,
                "native_windows_required": args.require_windows or args.expected_target is not None,
                "expected_target": args.expected_target,
            },
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
            "required_checks_passed": False,
        }
        _write_report(report_path, failure)
        raise
    _write_report(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
