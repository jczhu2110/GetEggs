from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
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
        def __init__(
            self, returncode: int, stdout: str = "", stderr: str = ""
        ) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return Proc(1, stdout="ERROR: The system cannot find the file specified.")

    monkeypatch.setattr(windows.subprocess, "run", fake_run)
    assert windows._task_missing_fast() is True
    assert commands[0][:2] == ["schtasks", "/query"]

    monkeypatch.setattr(windows.subprocess, "run", lambda cmd, **kwargs: Proc(0))
    assert windows._task_missing_fast() is False


def test_task_missing_fast_returns_none_for_ambiguous_errors(monkeypatch) -> None:
    class Proc:
        returncode = 1
        stdout = "Access is denied."
        stderr = ""

    monkeypatch.setattr(windows.subprocess, "run", lambda cmd, **kwargs: Proc())
    assert windows._task_missing_fast() is None


def test_task_missing_fast_decodes_chinese_console_output(monkeypatch) -> None:
    class Proc:
        returncode = 1
        stdout = b""
        stderr = "错误: 系统找不到指定的文件。".encode("gb18030")

    monkeypatch.setattr(windows.subprocess, "run", lambda cmd, **kwargs: Proc())
    assert windows._task_missing_fast() is True


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_schedule_status_reports_query_failure(monkeypatch) -> None:
    monkeypatch.setattr(windows, "_task_missing_fast", lambda: None)
    monkeypatch.setattr(
        windows, "_run_ps", lambda script: (0, '{"error":"Access is denied."}', "")
    )

    info = windows.get_schedule_status()

    assert info["supported"] is True
    assert info["installed"] is False
    assert info["query_failed"] is True
    assert "Access is denied." in info["message"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_schedule_status_distinguishes_missing_and_abnormal_result(monkeypatch) -> None:
    monkeypatch.setattr(windows, "_task_missing_fast", lambda: True)
    missing = windows.get_schedule_status()
    assert missing["installed"] is False
    assert not missing.get("query_failed")

    payload = '{"installed": true, "state": "Ready", "last_task_result": 1}'
    monkeypatch.setattr(windows, "_task_missing_fast", lambda: False)
    monkeypatch.setattr(windows, "_run_ps", lambda script: (0, payload, ""))
    abnormal = windows.get_schedule_status()
    assert abnormal["installed"] is True
    assert not abnormal.get("query_failed")
    assert "异常代码 1" in abnormal["message"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_install_schedule_uses_computed_timeout(monkeypatch) -> None:
    captured: dict[str, str] = {}

    monkeypatch.setattr(windows, "_probe_package_import", lambda *args: (True, ""))

    def fake_ps(script: str) -> tuple[int, str, str]:
        captured["script"] = script
        return 0, "OK", ""

    monkeypatch.setattr(windows, "_run_ps", fake_ps)

    ok, message = windows.install_schedule(
        "10:15", timeout_minutes=40, elevate=False
    )

    assert ok is True
    assert "-Minutes 40" in captured["script"]
    assert "40 分钟" in message
    assert "登录漏签补跑" in message

    ok_no_catchup, message_no_catchup = windows.install_schedule(
        "10:15", catchup_on_logon=False, timeout_minutes=40, elevate=False
    )
    assert ok_no_catchup is True
    assert "登录漏签补跑" not in message_no_catchup


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_schedule_status_reports_query_timeout(monkeypatch) -> None:
    monkeypatch.setattr(windows, "_task_missing_fast", lambda: None)

    def fake_ps(script: str, timeout: float = 15) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired("powershell", timeout)

    monkeypatch.setattr(windows, "_run_ps", fake_ps)

    info = windows.get_schedule_status()

    assert info["supported"] is True
    assert info["installed"] is False
    assert info["query_failed"] is True
    assert "查询计划任务失败" in info["message"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_install_schedule_reports_query_timeout(monkeypatch) -> None:
    monkeypatch.setattr(windows, "_probe_package_import", lambda *args: (True, ""))

    def fake_ps(script: str, timeout: float = 15) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired("powershell", timeout)

    monkeypatch.setattr(windows, "_run_ps", fake_ps)

    ok, message = windows.install_schedule("10:15", elevate=False)

    assert ok is False
    assert "注册计划任务失败" in message


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_install_schedule_timeout_checks_existing_task(monkeypatch) -> None:
    monkeypatch.setattr(windows, "_is_elevated", lambda: False)
    monkeypatch.setattr(windows, "_probe_package_import", lambda *args: (True, ""))
    monkeypatch.setattr(
        windows,
        "get_schedule_status",
        lambda: {"installed": True, "state": "Ready"},
    )

    def fake_ps(script: str, timeout: float = 15) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired("powershell", timeout)

    monkeypatch.setattr(windows, "_run_ps", fake_ps)

    ok, message = windows.install_schedule("10:15")

    assert ok is True
    assert "已安装成功" in message


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_install_schedule_requests_elevation_when_not_admin(monkeypatch) -> None:
    import base64

    monkeypatch.setattr(windows, "_is_elevated", lambda: False)
    monkeypatch.setattr(windows, "_probe_package_import", lambda *args: (True, ""))
    captured: dict[str, object] = {}

    def _extract_b64_var(script: str, var_name: str) -> str:
        match = re.search(
            rf"\${var_name}\s*=\s*\[System\.Text\.Encoding\]::UTF8\.GetString\(\[System\.Convert\]::FromBase64String\('([^']+)'\)\)",
            script,
        )
        assert match, script
        return base64.b64decode(match.group(1)).decode("utf-8")

    def fake_ps(script: str, timeout: float = 15) -> tuple[int, str, str]:
        captured["script"] = script
        result_file = Path(_extract_b64_var(script, "resultFile"))
        params_file = Path(_extract_b64_var(script, "paramsFile"))
        captured["result_file"] = result_file
        captured["params_file"] = params_file
        captured["params"] = json.loads(params_file.read_text(encoding="utf-8"))
        result_file.write_text(
            json.dumps({"ok": True, "message": "提权安装成功"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return 0, "", ""

    monkeypatch.setattr(windows, "_run_ps", fake_ps)

    ok, message = windows.install_schedule(
        "08:30", catchup_on_logon=False, timeout_minutes=45
    )

    assert ok is True
    assert message == "提权安装成功"
    script_text = str(captured["script"])
    assert "-Verb RunAs" in script_text
    assert "--no-elevate" in script_text
    assert "--params-file" in script_text
    assert captured["params"]["schedule_time"] == "08:30"
    assert captured["params"]["catchup_on_logon"] is False
    assert captured["params"]["timeout_minutes"] == 45
    assert not Path(captured["result_file"]).exists()
    assert not Path(captured["params_file"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_install_schedule_encodes_unicode_quote_paths_safely(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(windows, "_is_elevated", lambda: True)
    monkeypatch.setattr(windows, "_probe_package_import", lambda *args: (True, ""))
    captured: dict[str, str] = {}

    def fake_ps(script: str, timeout: float = 15) -> tuple[int, str, str]:
        captured["script"] = script
        return 0, "OK", ""

    monkeypatch.setattr(windows, "_run_ps", fake_ps)
    tricky_dir = tmp_path / "O\u2019Connor; Write-Output PWN"
    ok, _ = windows.install_schedule("10:15", working_dir=tricky_dir, elevate=False)

    assert ok is True
    assert "\u2019" not in captured["script"]
    assert "Write-Output PWN" not in captured["script"]
    assert "-AtLogOn -User $userId" in captured["script"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_uninstall_schedule_stops_on_error_and_elevates_on_access_denied(
    monkeypatch,
) -> None:
    import base64

    monkeypatch.setattr(windows, "_is_elevated", lambda: False)
    scripts: list[str] = []

    def fake_ps(script: str, timeout: float = 15) -> tuple[int, str, str]:
        scripts.append(script)
        if len(scripts) == 1:
            return 1, "", "Access is denied."
        match = re.search(
            r"\$resultFile\s*=\s*\[System\.Text\.Encoding\]::UTF8\.GetString\(\[System\.Convert\]::FromBase64String\('([^']+)'\)\)",
            script,
        )
        assert match, script
        res_path = Path(base64.b64decode(match.group(1)).decode("utf-8"))
        res_path.write_text(
            json.dumps({"ok": True, "message": "提权卸载成功"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return 0, "", ""

    monkeypatch.setattr(windows, "_run_ps", fake_ps)

    ok, message = windows.uninstall_schedule()

    assert ok is True
    assert message == "提权卸载成功"
    assert len(scripts) == 2
    assert "$ErrorActionPreference = 'Stop'" in scripts[0]
    assert "-ErrorAction Stop" in scripts[0]
    assert "-Verb RunAs" in scripts[1]


def test_schedule_install_reads_params_file(tmp_path: Path, monkeypatch) -> None:
    params_path = tmp_path / "params.json"
    result_path = tmp_path / "result.json"
    params_path.write_text(
        json.dumps(
            {
                "schedule_time": "07:45",
                "catchup_on_logon": False,
                "working_dir": str(tmp_path),
                "timeout_minutes": 55,
                "target_user": "PC\\Alice",
            }
        ),
        encoding="utf-8",
    )
    received: dict[str, object] = {}

    def fake_install(schedule_time, **kwargs):
        received["schedule_time"] = schedule_time
        received.update(kwargs)
        return True, "installed from params"

    monkeypatch.setattr("geteggs.cli.install_schedule", fake_install)
    result = runner.invoke(
        cli.app,
        [
            "schedule",
            "install",
            "--no-elevate",
            "--params-file",
            str(params_path),
            "--result-file",
            str(result_path),
        ],
    )

    assert result.exit_code == 0
    assert received["schedule_time"] == "07:45"
    assert received["catchup_on_logon"] is False
    assert received["timeout_minutes"] == 55
    assert received["target_user"] == "PC\\Alice"
    assert json.loads(result_path.read_text(encoding="utf-8"))["ok"] is True


def test_windows_toast_powershell_passes_text_via_env_vars(monkeypatch) -> None:
    from geteggs.notifiers import windows_toast

    captured: dict[str, object] = {}

    class Proc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return Proc()

    monkeypatch.setattr(windows_toast.subprocess, "run", fake_run)
    payload_msg = "error\u2019; Start-Process calc #\x00\x07"
    ok, err = windows_toast._send_via_powershell("标题\u2019", payload_msg)

    assert ok is True
    assert err is None
    script = captured["cmd"][-1]
    assert "\u2019" not in script
    assert "Start-Process" not in script
    assert captured["env"]["GETEGGS_TOAST_TITLE"] == "标题\u2019"
    assert captured["env"]["GETEGGS_TOAST_MSG"] == "error\u2019; Start-Process calc #"


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_install_schedule_reports_uac_cancellation(monkeypatch) -> None:
    monkeypatch.setattr(windows, "_is_elevated", lambda: False)
    monkeypatch.setattr(windows, "_probe_package_import", lambda *args: (True, ""))
    monkeypatch.setattr(
        windows,
        "_run_ps",
        lambda script, timeout=15: (1, "", "操作已被用户取消。"),
    )

    ok, message = windows.install_schedule("10:15")

    assert ok is False
    assert "取消" in message


def test_schedule_install_writes_result_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(
        "geteggs.cli.install_schedule", lambda *args, **kwargs: (False, "boom")
    )

    result = runner.invoke(
        cli.app,
        ["schedule", "install", "--no-elevate", "--result-file", str(result_path)],
    )

    assert result.exit_code == 1
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload == {"ok": False, "message": "boom"}


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
            json.dumps({"pid": os.getpid(), "port": port, "url": url}),
            encoding="utf-8",
        )

        opened: list[str] = []
        monkeypatch.setattr(
            launcher, "open_url", lambda value: opened.append(value) or True
        )
        result = runner.invoke(cli.app, ["open"])

    assert result.exit_code == 0
    assert opened == [url]


def test_existing_ui_url_rejects_dead_pid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        path = get_ui_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "pid": -1,
                    "port": port,
                    "url": f"http://127.0.0.1:{port}/auth?token=stale",
                }
            ),
            encoding="utf-8",
        )

        assert launcher._pid_alive(-1) is False
        assert launcher.existing_ui_url() is None


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
        with httpx.Client(timeout=5, trust_env=False) as client:
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


def test_cli_unknown_provider_exits_cleanly() -> None:
    doc = runner.invoke(cli.app, ["doctor", "--provider", "nonexistent"])
    assert doc.exit_code == 1
    assert "未知的插件" in doc.output

    auth = runner.invoke(cli.app, ["auth", "nonexistent"])
    assert auth.exit_code == 1
    assert "未知的插件" in auth.output


def test_cli_run_escapes_rich_markup_in_event_messages(
    tmp_path: Path, monkeypatch
) -> None:
    from geteggs.core.models import CheckinResult, CheckinStatus, LogEvent, RunSummary, TriggerType

    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))

    async def fake_run(self, **kwargs):
        sink = kwargs.get("event_sink")
        if sink:
            sink(LogEvent(level="error", provider_id="workbuddy", message="raw [/red][bold] payload"))
        return RunSummary(
            run_id="r1",
            trigger_type=TriggerType.MANUAL_CLI,
            started_at="2026-11-24T00:00:00+00:00",
            finished_at="2026-11-24T00:00:01+00:00",
            results=[
                CheckinResult(
                    provider_id="workbuddy",
                    display_name="WorkBuddy",
                    status=CheckinStatus.SUCCESS,
                    message="ok",
                    biz_date="2026-11-24",
                )
            ],
        )

    monkeypatch.setattr(cli.CheckinRunner, "run", fake_run)
    res = runner.invoke(cli.app, ["run", "--dry-run"])
    assert res.exit_code == 0
    assert "[/red][bold]" in res.output


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_get_schedule_status_query_failed_and_267008_healthy(monkeypatch) -> None:
    monkeypatch.setattr(windows, "_task_missing_fast", lambda: None)

    # Non-JSON / empty stdout on rc=0 must report query_failed rather than "not installed".
    monkeypatch.setattr(windows, "_run_ps", lambda script, timeout=15: (0, "not json", ""))
    bad = windows.get_schedule_status()
    assert bad["installed"] is False
    assert bad["query_failed"] is True

    # SCHED_S_TASK_READY (267008) must be treated as healthy.
    payload = json.dumps(
        {
            "installed": True,
            "state": "Ready",
            "next_run_time": "2026-11-25T09:00:00+08:00",
            "last_run_time": None,
            "last_task_result": 267008,
        }
    )
    monkeypatch.setattr(windows, "_run_ps", lambda script, timeout=15: (0, payload, ""))
    ready = windows.get_schedule_status()
    assert ready["installed"] is True
    assert not ready.get("query_failed")
    assert "已安装并就绪" in ready["message"]


def test_schedule_install_invalid_params_file_and_invalid_time_fail_cleanly(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    bad_params = tmp_path / "missing_params.json"
    result_file = tmp_path / "result.json"

    res = runner.invoke(
        cli.app,
        [
            "schedule",
            "install",
            "--no-elevate",
            "--params-file",
            str(bad_params),
            "--result-file",
            str(result_file),
        ],
    )
    assert res.exit_code == 1
    assert result_file.is_file()
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["ok"] is False

    bad_time = runner.invoke(cli.app, ["schedule", "install", "--time", "99:99"])
    assert bad_time.exit_code == 1
    assert "定时时间格式无效" in bad_time.output


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler only")
def test_uninstall_schedule_script_propagates_access_denied(monkeypatch) -> None:
    captured: list[str] = []

    def fake_ps(script: str, timeout: float = 15) -> tuple[int, str, str]:
        captured.append(script)
        return 0, "OK", ""

    monkeypatch.setattr(windows, "_run_ps", fake_ps)
    ok, _ = windows.uninstall_schedule(elevate=False)
    assert ok is True
    assert captured
    assert "-ErrorAction SilentlyContinue" not in captured[0]
    assert "Get-ScheduledTask -TaskName $taskName -ErrorAction Stop" in captured[0]
    assert "NotFound|ObjectNotFound" in captured[0]


def test_cli_status_renders_without_credit_or_streak_columns(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    res = runner.invoke(cli.app, ["status"])
    assert res.exit_code == 0
    assert "WorkBuddy" in res.output
    assert "连签天数" not in res.output
    assert "今日新增" not in res.output
