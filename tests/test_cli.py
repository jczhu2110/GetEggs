from __future__ import annotations

import json
import socket
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from typer.testing import CliRunner

from geteggs import cli, launcher
from geteggs.core.config import get_ui_state_path
from geteggs.core.lock import RunLockBusyError
from geteggs.scheduler import windows

runner = CliRunner()


def test_maintenance_dry_run_lists_info(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    result = runner.invoke(cli.app, ["maintenance", "--dry-run"])
    assert result.exit_code == 0
    assert "db_path" in result.output
    assert "counts" in result.output


def test_maintenance_exits_when_run_lock_is_busy(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    called: list[bool] = []

    @contextmanager
    def busy_lock(_path):
        raise RunLockBusyError("busy")
        yield

    monkeypatch.setattr("geteggs.cli.acquire_run_lock", busy_lock)
    monkeypatch.setattr(
        "geteggs.cli.Storage.run_maintenance",
        lambda *args, **kwargs: called.append(True),
    )

    result = runner.invoke(cli.app, ["maintenance"])
    assert result.exit_code == 2
    assert "已有签到任务在运行" in result.output
    assert called == []


def test_pick_port_avoids_occupied_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        occupied = int(listener.getsockname()[1])
        chosen = cli._pick_port(occupied)
        assert chosen != occupied


def test_pick_port_returns_free_port_quickly() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free = int(probe.getsockname()[1])

    started = time.perf_counter()
    assert cli._pick_port(free) == free
    assert time.perf_counter() - started < 0.5


def test_port_open_probes_without_waiting_for_refusal() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        assert launcher.port_open(port) is True

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        closed = int(probe.getsockname()[1])

    started = time.perf_counter()
    assert launcher.port_open(closed) is False
    assert time.perf_counter() - started < 0.5


def test_task_missing_fast_uses_schtasks(monkeypatch) -> None:
    commands: list[list[str]] = []

    class Proc:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return Proc(1)

    monkeypatch.setattr(windows.subprocess, "run", fake_run)
    assert windows._task_missing_fast() is True
    assert commands[0][:2] == ["schtasks", "/query"]

    monkeypatch.setattr(windows.subprocess, "run", lambda cmd, **kwargs: Proc(0))
    assert windows._task_missing_fast() is False


def test_ui_duplicate_reports_existing_url(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    called: list[bool] = []

    @contextmanager
    def busy_lock(_path):
        raise RunLockBusyError("busy")
        yield

    monkeypatch.setattr("geteggs.cli.acquire_run_lock", busy_lock)
    monkeypatch.setattr(
        "geteggs.cli._read_ui_state",
        lambda: {"url": "http://127.0.0.1:18888/"},
    )
    monkeypatch.setattr(
        "geteggs.cli._run_ui", lambda *args: called.append(True)
    )

    result = runner.invoke(cli.app, ["ui", "--no-browser"])
    assert result.exit_code == 0
    assert "http://127.0.0.1:18888/" in result.output
    assert called == []


def test_read_ui_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    path = get_ui_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"url": "http://127.0.0.1:19999/", "port": 19999}',
        encoding="utf-8",
    )
    state = cli._read_ui_state(timeout=0.01)
    assert state["url"] == "http://127.0.0.1:19999/"
    assert state["port"] == 19999


def test_ui_endpoint_rejects_non_local_urls() -> None:
    assert launcher.ui_endpoint({"url": "https://evil.example/x", "port": 80}) is None
    assert (
        launcher.ui_endpoint(
            {"url": "http://127.0.0.1:18765/auth?token=x", "port": 18765}
        )
        == ("http://127.0.0.1:18765/auth?token=x", 18765)
    )


def test_ui_server_command_opens_when_ready() -> None:
    command = launcher.ui_server_command()
    assert "--open-when-ready" in command
    assert "--quiet" in command


def test_ui_accepts_open_when_ready(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    received: dict[str, object] = {}

    def fake_run(port, no_browser, no_auth, quiet=False, open_when_ready=False):
        received.update(
            port=port,
            quiet=quiet,
            open_when_ready=open_when_ready,
        )

    monkeypatch.setattr(cli, "_run_ui", fake_run)
    result = runner.invoke(cli.app, ["ui", "--quiet", "--open-when-ready"])

    assert result.exit_code == 0
    assert received["quiet"] is True
    assert received["open_when_ready"] is True


def test_open_browser_when_ready_uses_url(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher, "port_open", lambda port: True)
    monkeypatch.setattr(launcher, "open_url", lambda url: opened.append(url) or True)

    url = "http://127.0.0.1:18765/auth?token=ready"
    cli._open_browser_when_ready(url, 18765)

    assert opened == [url]


def test_open_command_opens_existing_page(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        url = f"http://127.0.0.1:{port}/auth?token=existing"
        path = get_ui_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": 1, "port": port, "url": url}), encoding="utf-8"
        )

        opened: list[str] = []
        monkeypatch.setattr(
            launcher, "open_url", lambda value: opened.append(value) or True
        )
        result = runner.invoke(cli.app, ["open"])

    assert result.exit_code == 0
    assert opened == [url]


def test_ui_server_command_no_browser_when_splash() -> None:
    command = launcher.ui_server_command(open_when_ready=False)
    assert "--no-browser" in command
    assert "--open-when-ready" not in command
    assert "--quiet" in command


def test_splash_server_serves_loading_and_ready(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    state = launcher.SplashState()
    server, url = launcher.start_splash_server(state)
    try:
        base = url.split("/loading", 1)[0]
        with httpx.Client(timeout=5) as client:
            loading = client.get(url)
            assert loading.status_code == 200
            assert "GetEggs 正在启动" in loading.text
            assert "正在准备本地服务" in loading.text

            wrong = client.get(base + "/ready?n=wrong")
            assert wrong.status_code == 403

            pending = client.get(base + f"/ready?n={state.nonce}")
            assert pending.json()["ready"] is False

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                port = int(listener.getsockname()[1])
                ui_url = f"http://127.0.0.1:{port}/auth?token=test"
                path = get_ui_state_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"port": port, "url": ui_url}), encoding="utf-8"
                )
                ready = client.get(base + f"/ready?n={state.nonce}")
                assert ready.json() == {"ready": True, "url": ui_url}
                assert state.redirect_seen.wait(1.0)
    finally:
        server.shutdown()
        server.server_close()


class _FakeSplashServer:
    def __init__(self) -> None:
        self.shutdown_called = False
        self.closed = False

    def shutdown(self) -> None:
        self.shutdown_called = True

    def server_close(self) -> None:
        self.closed = True


def _patch_splash(monkeypatch, url: str = "http://127.0.0.1:19999/loading?n=test"):
    fake = _FakeSplashServer()
    monkeypatch.setattr(
        launcher, "start_splash_server", lambda state: (fake, url)
    )
    monkeypatch.setattr(launcher, "SPLASH_REDIRECT_TIMEOUT", 0.0)
    monkeypatch.setattr(launcher, "SPLASH_ERROR_TIMEOUT", 0.0)
    return fake, url


def test_open_command_shows_splash_then_starts_server(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    fake, splash_url = _patch_splash(monkeypatch)
    ui_url = "http://127.0.0.1:18765/auth?token=ready"
    existing_values = [None, ui_url]
    opened: list[str] = []
    spawn_calls: list[bool] = []

    class FakeProc:
        def poll(self):
            return None

    def fake_existing():
        return existing_values.pop(0) if existing_values else ui_url

    monkeypatch.setattr(launcher, "existing_ui_url", fake_existing)
    monkeypatch.setattr(launcher, "open_url", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        launcher,
        "spawn_ui_server",
        lambda open_when_ready=True: spawn_calls.append(open_when_ready) or FakeProc(),
    )
    monkeypatch.setattr(launcher, "wait_for_state", lambda proc, timeout=0: True)

    result = runner.invoke(cli.app, ["open"])

    assert result.exit_code == 0
    assert opened == [splash_url]
    assert spawn_calls == [False]
    assert fake.shutdown_called is True
    assert fake.closed is True


def test_open_command_falls_back_when_splash_browser_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    fake, splash_url = _patch_splash(monkeypatch)
    opened: list[str] = []
    spawn_calls: list[bool] = []

    class FakeProc:
        def poll(self):
            return None

    monkeypatch.setattr(launcher, "existing_ui_url", lambda: None)
    monkeypatch.setattr(launcher, "open_url", lambda url: opened.append(url) or False)
    monkeypatch.setattr(
        launcher,
        "spawn_ui_server",
        lambda open_when_ready=True: spawn_calls.append(open_when_ready) or FakeProc(),
    )
    monkeypatch.setattr(launcher, "wait_for_state", lambda proc, timeout=0: True)

    result = runner.invoke(cli.app, ["open"])

    assert result.exit_code == 0
    assert opened == [splash_url]
    assert spawn_calls == [True]
    assert fake.shutdown_called is True
    assert fake.closed is True


def test_open_command_reports_spawn_failure_in_splash(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    fake, _splash_url = _patch_splash(monkeypatch)
    messages: list[str] = []

    monkeypatch.setattr(launcher, "existing_ui_url", lambda: None)
    monkeypatch.setattr(launcher, "open_url", lambda url: True)
    monkeypatch.setattr(launcher, "notify_failure", messages.append)

    def fail_spawn(open_when_ready=True):
        raise OSError("boom")

    monkeypatch.setattr(launcher, "spawn_ui_server", fail_spawn)

    result = runner.invoke(cli.app, ["open"])

    assert result.exit_code == 0
    assert fake.shutdown_called is True
    assert fake.closed is True
    assert messages == []


def test_open_command_reports_start_timeout_in_splash(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    fake, _splash_url = _patch_splash(monkeypatch)

    class FakeProc:
        def poll(self):
            return None

    monkeypatch.setattr(launcher, "existing_ui_url", lambda: None)
    monkeypatch.setattr(launcher, "open_url", lambda url: True)
    monkeypatch.setattr(launcher, "spawn_ui_server", lambda open_when_ready=True: FakeProc())
    monkeypatch.setattr(launcher, "wait_for_state", lambda proc, timeout=0: False)

    result = runner.invoke(cli.app, ["open"])

    assert result.exit_code == 0
    assert fake.shutdown_called is True
    assert fake.closed is True


def test_ui_quiet_does_not_print_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))

    @contextmanager
    def busy_lock(_path):
        raise RunLockBusyError("busy")
        yield

    monkeypatch.setattr("geteggs.cli.acquire_run_lock", busy_lock)
    monkeypatch.setattr(
        "geteggs.cli._read_ui_state",
        lambda: {"url": "http://127.0.0.1:18888/auth?token=secret"},
    )

    result = runner.invoke(cli.app, ["ui", "--no-browser", "--quiet"])

    assert result.exit_code == 0
    assert "已在运行" in result.output
    assert "token=secret" not in result.output
