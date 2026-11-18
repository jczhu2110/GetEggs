from __future__ import annotations

import socket
from contextlib import contextmanager
from pathlib import Path

from typer.testing import CliRunner

from geteggs import cli
from geteggs.core.config import get_ui_state_path
from geteggs.core.lock import RunLockBusyError

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
