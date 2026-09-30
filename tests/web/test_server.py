from __future__ import annotations

import contextlib
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from topoforge.exceptions import ConfigurationError
from topoforge.web import server as server_module
from topoforge.web.jobs import LocalJobManager
from topoforge.web.models import WebAppConfig


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _health(url: str) -> dict[str, object]:
    # Ignore host proxy configuration when testing the real loopback listener.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + "api/v1/health", timeout=5) as response:
        return json.load(response)


@contextlib.contextmanager
def _running_server(
    monkeypatch: pytest.MonkeyPatch,
    config: WebAppConfig,
    static_dir: Path,
    port: int,
    *,
    open_browser: bool = True,
) -> Iterator[tuple[threading.Thread, list[BaseException]]]:
    servers: list[uvicorn.Server] = []
    errors: list[BaseException] = []
    original_init = uvicorn.Server.__init__

    def capture_server(self: uvicorn.Server, config: uvicorn.Config) -> None:
        original_init(self, config)
        servers.append(self)

    monkeypatch.setattr(uvicorn.Server, "__init__", capture_server)

    def run() -> None:
        try:
            server_module.run_web_server(
                config, port=port, open_browser=open_browser, static_dir=static_dir
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield thread, errors
    finally:
        for server in servers:
            server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "local Web server did not stop"


def test_browser_waits_for_recovery_and_a_working_http_listener(
    monkeypatch: pytest.MonkeyPatch, web_config: WebAppConfig, web_static_dir: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()
    opened = threading.Event()
    observations: list[object] = []
    original_start = LocalJobManager.start
    port = _unused_port()
    url = f"http://127.0.0.1:{port}/"

    def recover(self: LocalJobManager) -> None:
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release simulated recovery")
        original_start(self)

    def open_browser(actual_url: str) -> bool:
        try:
            observations.append((actual_url, _health(actual_url)))
        except Exception as exc:
            observations.append(exc)
        finally:
            opened.set()
        return True

    monkeypatch.setattr(LocalJobManager, "start", recover)
    monkeypatch.setattr(server_module.webbrowser, "open", open_browser)
    with _running_server(monkeypatch, web_config, web_static_dir, port) as (_, errors):
        try:
            assert entered.wait(10)
            # The previous fixed 0.8s timer fired while recovery was still blocked.
            assert not opened.wait(1.1)
        finally:
            release.set()
        assert opened.wait(10)
        assert len(observations) == 1
        assert observations[0] == (url, _health(url))
        assert not errors


@pytest.mark.parametrize("failure", ["occupied-port", "recovery-error"])
def test_failed_startup_never_opens_a_browser(
    monkeypatch: pytest.MonkeyPatch,
    web_config: WebAppConfig,
    web_static_dir: Path,
    failure: str,
) -> None:
    opened = threading.Event()
    monkeypatch.setattr(server_module.webbrowser, "open", lambda _url: opened.set())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        if failure == "occupied-port":
            listener.listen()
        else:
            listener.close()

            def fail_recovery(self: LocalJobManager) -> None:
                raise ConfigurationError("simulated startup recovery failure")

            monkeypatch.setattr(LocalJobManager, "start", fail_recovery)
        with _running_server(monkeypatch, web_config, web_static_dir, port) as (thread, errors):
            thread.join(timeout=10)
            assert not thread.is_alive()
            assert len(errors) == 1
            assert isinstance(errors[0], SystemExit)
            assert errors[0].code != 0
            assert not opened.wait(1.1), "browser opened despite failed startup"


def test_no_open_still_serves_the_application(
    monkeypatch: pytest.MonkeyPatch, web_config: WebAppConfig, web_static_dir: Path
) -> None:
    opened = threading.Event()
    monkeypatch.setattr(server_module.webbrowser, "open", lambda _url: opened.set())
    port = _unused_port()
    with _running_server(monkeypatch, web_config, web_static_dir, port, open_browser=False) as (
        _,
        errors,
    ):
        deadline = time.monotonic() + 10
        while True:
            try:
                health = _health(f"http://127.0.0.1:{port}/")
                break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        assert health["status"] == "ok"
        assert not opened.wait(1.1)
        assert not errors


@pytest.mark.parametrize("failure", ["not-found", "exception"])
def test_browser_failure_reports_the_manual_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    url = "http://127.0.0.1:8765/"

    def unavailable(_url: str) -> bool:
        if failure == "exception":
            raise OSError("browser is unavailable")
        return False

    monkeypatch.setattr(server_module.webbrowser, "open", unavailable)
    with caplog.at_level(logging.WARNING, logger=server_module.__name__):
        server_module._open_ready_browser(url)
    assert f"open {url} manually" in caplog.text
