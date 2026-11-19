"""Unit tests for CheckinRunner, Storage, lock, and notification deduplication."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from geteggs.core.config import today_biz_date
from geteggs.core.lock import RunLockBusyError, acquire_run_lock
from geteggs.core.models import (
    DEFAULT_CHECKIN_CREDITS,
    CheckinResult,
    CheckinStatus,
    CreditSnapshot,
    DoctorResult,
    GlobalSettings,
    LogEvent,
    ProviderConfig,
    TriggerType,
)
from geteggs.core.runner import CheckinRunner
from geteggs.core.storage import Storage
from geteggs.notifiers.base import BaseNotifier
from geteggs.providers.base import BaseProvider, LogCallback


class FakeProvider(BaseProvider):
    id = "fake"
    display_name = "FakeTool"
    description = "Fake test provider"

    def __init__(
        self,
        status: CheckinStatus = CheckinStatus.SUCCESS,
        delta: int = 80,
        provider_id: str = "fake",
        display_name: str = "FakeTool",
    ) -> None:
        self.id = provider_id
        self.display_name = display_name
        self._status = status
        self._delta = delta
        self.calls = 0

    def detect_client_exe(self, config: ProviderConfig) -> Optional[str]:
        return None

    async def doctor(self, config: ProviderConfig) -> DoctorResult:
        return DoctorResult(
            provider_id=self.id,
            display_name=self.display_name,
            client_installed=False,
            auth_file_found=True,
            auth_readable=True,
            healthy=True,
            message="ok",
        )

    async def checkin(
        self,
        config: ProviderConfig,
        biz_date: str,
        *,
        dry_run: bool = False,
        log: Optional[LogCallback] = None,
    ) -> CheckinResult:
        self.calls += 1
        return CheckinResult(
            provider_id=self.id,
            display_name=self.display_name,
            status=self._status,
            message="done" if self._status.is_done_for_today else "need login",
            biz_date=biz_date,
            snapshot=CreditSnapshot(delta=self._delta, streak_days=3),
            dry_run=dry_run,
        )


class RaisingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__(provider_id="raising", display_name="RaisingTool")

    async def checkin(
        self,
        config: ProviderConfig,
        biz_date: str,
        *,
        dry_run: bool = False,
        log: Optional[LogCallback] = None,
    ) -> CheckinResult:
        self.calls += 1
        raise RuntimeError("adapter exploded")


class RecordingNotifier(BaseNotifier):
    channel_id = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(
        self,
        title: str,
        message: str,
        *,
        level: str = "info",
        action_label: Optional[str] = None,
        action_url: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        self.sent.append((level, title, message))
        return True, None


def test_runner_missed_only_skips_already_completed_today(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    notifier = RecordingNotifier()
    runner = CheckinRunner(storage=storage, notifier=notifier, lock_path=tmp_path / "run.lock")
    provider = FakeProvider(status=CheckinStatus.SUCCESS, delta=80)

    first = asyncio.run(
        runner.run(
            trigger_type=TriggerType.SCHEDULED,
            missed_only=True,
            providers_override=[provider],
        )
    )
    assert provider.calls == 1
    assert first.results[0].status == CheckinStatus.SUCCESS
    assert len(notifier.sent) == 1

    second = asyncio.run(
        runner.run(
            trigger_type=TriggerType.MISSED_CATCHUP,
            missed_only=True,
            providers_override=[provider],
        )
    )
    assert provider.calls == 1
    assert second.results[0].status == CheckinStatus.ALREADY_DONE
    assert len(notifier.sent) == 1
    assert storage.get_maintenance_state().get("last_cleanup_at")


def test_runner_deduplicates_action_alert_same_day(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    notifier = RecordingNotifier()
    runner = CheckinRunner(storage=storage, notifier=notifier, lock_path=tmp_path / "run.lock")
    provider = FakeProvider(status=CheckinStatus.AUTH_EXPIRED, delta=0)

    asyncio.run(runner.run(providers_override=[provider]))
    asyncio.run(runner.run(providers_override=[provider]))

    assert provider.calls == 2
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] == "warning"


def test_file_lock_blocks_concurrent_acquire(tmp_path: Path) -> None:
    lock_file = tmp_path / "runner.lock"
    with acquire_run_lock(lock_file):
        with pytest.raises(RunLockBusyError):
            with acquire_run_lock(lock_file):
                pass


def test_runner_isolates_provider_exception_and_continues(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    notifier = RecordingNotifier()
    runner = CheckinRunner(storage=storage, notifier=notifier, lock_path=tmp_path / "run.lock")
    raising = RaisingProvider()
    good = FakeProvider(provider_id="good", display_name="GoodTool")

    summary = asyncio.run(runner.run(providers_override=[raising, good]))

    assert len(summary.results) == 2
    assert summary.results[0].status == CheckinStatus.FAILED
    assert "adapter exploded" in summary.results[0].message
    assert summary.results[1].status == CheckinStatus.SUCCESS
    assert raising.calls == 1
    assert good.calls == 1
    assert storage.get_runs()[0]["status"] == "completed"

    events = storage.get_run_events(summary.run_id)
    assert any("开始执行 GoodTool 签到" in event["message"] for event in events)
    assert any(event["level"] == "error" for event in events)


def test_runner_notifies_both_success_and_failure(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    notifier = RecordingNotifier()
    runner = CheckinRunner(storage=storage, notifier=notifier, lock_path=tmp_path / "run.lock")
    success = FakeProvider(provider_id="alpha", display_name="Alpha")
    failed = FakeProvider(
        status=CheckinStatus.FAILED,
        delta=0,
        provider_id="beta",
        display_name="Beta",
    )

    asyncio.run(runner.run(providers_override=[success, failed]))

    assert len(notifier.sent) == 2
    assert {item[0] for item in notifier.sent} == {"info", "error"}

    # Re-running the same fixed outcomes must not spam duplicate notifications.
    asyncio.run(runner.run(providers_override=[success, failed]))
    assert len(notifier.sent) == 2


def test_storage_today_summary_keeps_success_after_later_failure(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    biz_date = "2026-11-16"
    storage.save_checkin_result(
        "run-1",
        CheckinResult(
            provider_id="fake",
            display_name="FakeTool",
            status=CheckinStatus.SUCCESS,
            message="签到成功",
            biz_date=biz_date,
            snapshot=CreditSnapshot(
                delta=100,
                streak_days=7,
                expiring_soon=20,
                expiring_at="2026-12-01T00:00:00+00:00",
            ),
        ),
    )
    storage.save_checkin_result(
        "run-1",
        CheckinResult(
            provider_id="fake",
            display_name="FakeTool",
            status=CheckinStatus.FAILED,
            message="network timeout",
            biz_date=biz_date,
        ),
    )

    summary = storage.get_today_summary("fake", biz_date)
    assert summary is not None
    assert summary["status"] == "success"
    assert summary["ok"] is True
    assert summary["delta"] == 100
    assert "balance" not in summary
    assert summary["streak_days"] == 7
    assert summary["expiring_soon"] == 20
    assert summary["last_status"] == "failed"
    assert summary["record_count"] == 2

    latest_snapshot = storage.get_latest_snapshot_record("fake")
    assert latest_snapshot is not None
    assert "balance" not in latest_snapshot
    assert latest_snapshot["streak_days"] == 7

    page = storage.get_history(limit=1, offset=1)
    assert len(page["records"]) == 1
    assert page["records"][0]["status"] == "success"
    assert page["offset"] == 1


def test_storage_drops_legacy_balance_column_and_preserves_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy_balance.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE checkin_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            biz_date TEXT NOT NULL,
            status TEXT NOT NULL,
            delta INTEGER NOT NULL DEFAULT 0,
            balance INTEGER,
            streak_days INTEGER,
            expiring_soon INTEGER,
            expiring_at TEXT,
            message TEXT NOT NULL,
            raw_error_type TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            dry_run INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        INSERT INTO checkin_records (
            run_id, provider_id, display_name, biz_date, status,
            delta, balance, streak_days, message, created_at
        ) VALUES (
            'run-legacy', 'workbuddy', 'WorkBuddy', '2026-11-10', 'success',
            100, 1300, 7, '旧记录', '2026-11-10T00:00:00+00:00'
        );
        CREATE TABLE provider_configs (
            provider_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            earliest_time TEXT NOT NULL DEFAULT '00:00',
            jitter_minutes INTEGER NOT NULL DEFAULT 10,
            extra_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        INSERT INTO provider_configs (
            provider_id, enabled, earliest_time, jitter_minutes, extra_json, updated_at
        ) VALUES (
            'workbuddy', 1, '00:00', 10, '{}', '2026-11-10T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    storage = Storage(db_path)

    with sqlite3.connect(db_path) as check:
        columns = {
            row[1] for row in check.execute("PRAGMA table_info(checkin_records)")
        }
        preserved = check.execute(
            "SELECT run_id, delta, streak_days, message FROM checkin_records"
        ).fetchone()
    assert "balance" not in columns
    assert preserved == ("run-legacy", 100, 7, "旧记录")
    record = storage.get_history(limit=1)["records"][0]
    assert "balance" not in record
    assert (
        storage.get_provider_config("workbuddy").checkin_credits
        == DEFAULT_CHECKIN_CREDITS
    )


def test_provider_config_checkin_credits_persists(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "credits.db")
    cfg = storage.get_provider_config("workbuddy")
    assert cfg.checkin_credits == DEFAULT_CHECKIN_CREDITS

    cfg.checkin_credits = 250
    storage.save_provider_config(cfg)
    assert storage.get_provider_config("workbuddy").checkin_credits == 250


def test_effective_jitter_uses_provider_override_when_set() -> None:
    settings = GlobalSettings(jitter_minutes=15)
    cfg = ProviderConfig(provider_id="fake")
    assert CheckinRunner._effective_jitter_minutes(cfg, settings) == 15

    cfg.jitter_minutes = 5
    cfg.extra["jitter_override"] = True
    assert CheckinRunner._effective_jitter_minutes(cfg, settings) == 5


def test_storage_run_events_roundtrip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "events.db")
    storage.record_run_start("run-events", TriggerType.MANUAL_CLI)
    storage.save_log_event(
        "run-events",
        LogEvent(level="info", provider_id="fake", message="hello"),
    )
    storage.save_log_event(
        "run-events",
        LogEvent(level="warning", provider_id=None, message="careful"),
    )

    events = storage.get_run_events("run-events")
    assert [event["message"] for event in events] == ["hello", "careful"]
    assert events[0]["provider_id"] == "fake"
    assert events[1]["level"] == "warning"


def test_storage_cleanup_removes_old_data_and_marks_stale_runs(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "cleanup.db")
    settings = GlobalSettings(
        run_events_retention_days=1,
        notification_retention_days=1,
        run_retention_days=30,
        ledger_retention_days=1,
        vacuum_interval_days=30,
    )
    now = datetime.now(timezone.utc)
    old_iso = (now - timedelta(days=3)).isoformat()
    old_run_iso = (now - timedelta(days=40)).isoformat()
    stale_iso = (now - timedelta(days=1)).isoformat()
    old_date = (now.date() - timedelta(days=3)).strftime("%Y-%m-%d")
    new_date = today_biz_date(settings.timezone)

    storage.save_log_event(
        "old-run", LogEvent(timestamp=old_iso, message="old event")
    )
    storage.save_log_event("new-run", LogEvent(message="new event"))
    with storage._connect() as conn:
        conn.execute(
            """
            INSERT INTO notifications (
                channel, level, dedup_key, biz_date, title, message, success, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("test", "info", None, old_date, "old", "old", 1, old_iso),
        )
        conn.execute(
            """
            INSERT INTO checkin_runs (run_id, trigger_type, started_at, finished_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("old-run", "manual_cli", old_run_iso, old_run_iso, "completed"),
        )
        conn.execute(
            """
            INSERT INTO checkin_runs (run_id, trigger_type, started_at, finished_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("stale-run", "manual_cli", stale_iso, None, "running"),
        )
    storage.record_run_start("new-run", TriggerType.MANUAL_CLI)
    storage.record_run_finish("new-run", "completed")
    storage.save_checkin_result(
        "old-run",
        CheckinResult(
            provider_id="fake",
            display_name="Fake",
            status=CheckinStatus.SUCCESS,
            message="old",
            biz_date=old_date,
        ),
    )
    storage.save_checkin_result(
        "new-run",
        CheckinResult(
            provider_id="fake",
            display_name="Fake",
            status=CheckinStatus.SUCCESS,
            message="new",
            biz_date=new_date,
        ),
    )
    storage.save_notification("test", "info", "new", "new", True)

    deleted = storage.cleanup_old_data(settings)
    assert deleted["run_events"] == 1
    assert deleted["notifications"] == 1
    assert deleted["checkin_runs"] == 1
    assert deleted["checkin_records"] == 1
    assert deleted["stale_runs"] == 1

    assert not storage.get_run_events("old-run")
    assert len(storage.get_run_events("new-run")) == 1
    notifications = storage.get_notifications()
    assert len(notifications) == 1
    assert notifications[0]["title"] == "new"
    runs = {run["run_id"]: run for run in storage.get_runs()}
    assert "old-run" not in runs
    assert runs["new-run"]["status"] == "completed"
    assert runs["stale-run"]["status"] == "interrupted"
    records = storage.get_history(limit=10)["records"]
    assert [record["biz_date"] for record in records] == [new_date]


def test_storage_run_maintenance_backup_state_and_vacuum(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "maintenance.db")
    settings = GlobalSettings()
    storage.save_log_event("run", LogEvent(message="hello"))

    cleanup_due, vacuum_due = storage.maintenance_due(settings)
    assert cleanup_due is True
    assert vacuum_due is True

    result = storage.run_maintenance(settings, vacuum=False, backup=True)
    assert result["backup"]
    backup_path = Path(result["backup"])
    assert backup_path.is_file()
    assert backup_path.stat().st_size > 0
    assert storage.get_maintenance_state().get("last_cleanup_at")
    assert "last_vacuum_at" not in storage.get_maintenance_state()

    cleanup_due, vacuum_due = storage.maintenance_due(settings)
    assert cleanup_due is False
    assert vacuum_due is False

    info = storage.get_maintenance_info()
    assert info["backup_count"] >= 1
    assert info["counts"]["run_events"] == 1

    vacuum_result = storage.run_maintenance(settings, vacuum=True, backup=False)
    assert vacuum_result["vacuumed"] is True
    assert vacuum_result["vacuum_error"] is None
    assert storage.get_maintenance_state().get("last_vacuum_at")


def test_storage_get_runs_includes_summary_counts(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "runs_summary.db")
    storage.record_run_start("run-real", TriggerType.MANUAL_WEB)
    storage.save_checkin_result(
        "run-real",
        CheckinResult(
            provider_id="alpha",
            display_name="Alpha",
            status=CheckinStatus.SUCCESS,
            message="ok",
            biz_date="2026-11-18",
            snapshot=CreditSnapshot(delta=30, streak_days=2),
        ),
    )
    storage.save_checkin_result(
        "run-real",
        CheckinResult(
            provider_id="beta",
            display_name="Beta",
            status=CheckinStatus.FAILED,
            message="err",
            biz_date="2026-11-18",
        ),
    )
    storage.record_run_finish("run-real", "completed")

    storage.record_run_start("run-dry", TriggerType.MANUAL_WEB)
    storage.save_checkin_result(
        "run-dry",
        CheckinResult(
            provider_id="alpha",
            display_name="Alpha",
            status=CheckinStatus.ALREADY_DONE,
            message="dry",
            biz_date="2026-11-18",
            snapshot=CreditSnapshot(delta=99, streak_days=2),
            dry_run=True,
        ),
    )
    storage.record_run_finish("run-dry", "completed")

    runs = {run["run_id"]: run for run in storage.get_runs()}
    assert runs["run-real"]["record_count"] == 2
    assert runs["run-real"]["ok_count"] == 1
    assert runs["run-real"]["total_delta"] == 30
    assert runs["run-real"]["dry_run"] == 0

    assert runs["run-dry"]["record_count"] == 1
    assert runs["run-dry"]["ok_count"] == 1
    assert runs["run-dry"]["total_delta"] == 0
    assert runs["run-dry"]["dry_run"] == 1
