"""Unit tests for CheckinRunner, Storage, lock, and notification deduplication."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from geteggs.core.config import business_date, today_biz_date
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


class SlowProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__(provider_id="slow", display_name="SlowTool")

    async def checkin(
        self,
        config: ProviderConfig,
        biz_date: str,
        *,
        dry_run: bool = False,
        log: Optional[LogCallback] = None,
    ) -> CheckinResult:
        self.calls += 1
        await asyncio.sleep(10)
        return CheckinResult(
            provider_id=self.id,
            display_name=self.display_name,
            status=CheckinStatus.SUCCESS,
            message="done",
            biz_date=biz_date,
            snapshot=CreditSnapshot(delta=1),
            dry_run=dry_run,
        )


class FixedBusinessDayProvider(FakeProvider):
    def __init__(self, business_day: str) -> None:
        super().__init__(provider_id="qoder", display_name="Qoder")
        self._business_day = business_day

    def business_date(
        self,
        config: ProviderConfig,
        tz_name: str = "Asia/Shanghai",
        *,
        now: Optional[datetime] = None,
    ) -> str:
        return self._business_day


class SequenceBusinessDayProvider(FakeProvider):
    def __init__(self, business_days: list[str]) -> None:
        super().__init__()
        self._business_days = business_days

    def business_date(
        self,
        config: ProviderConfig,
        tz_name: str = "Asia/Shanghai",
        *,
        now: Optional[datetime] = None,
    ) -> str:
        return self._business_days[min(self.calls, len(self._business_days) - 1)]


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


def test_runner_missed_only_uses_provider_business_date(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.save_provider_config(
        ProviderConfig(provider_id="qoder", enabled=True, business_day_start="10:00")
    )
    storage.save_checkin_result(
        "seed-run",
        CheckinResult(
            provider_id="qoder",
            display_name="Qoder",
            status=CheckinStatus.SUCCESS,
            message="上一业务日已领取",
            biz_date="2026-11-18",
            snapshot=CreditSnapshot(delta=100),
        ),
    )
    runner = CheckinRunner(
        storage=storage,
        notifier=RecordingNotifier(),
        lock_path=tmp_path / "run.lock",
    )

    before_reset = FixedBusinessDayProvider("2026-11-18")
    skipped = asyncio.run(
        runner.run(
            trigger_type=TriggerType.MISSED_CATCHUP,
            missed_only=True,
            providers_override=[before_reset],
        )
    )
    assert before_reset.calls == 0
    assert skipped.results[0].status == CheckinStatus.ALREADY_DONE
    assert skipped.results[0].biz_date == "2026-11-18"

    after_reset = FixedBusinessDayProvider("2026-11-19")
    claimed = asyncio.run(
        runner.run(
            trigger_type=TriggerType.MISSED_CATCHUP,
            missed_only=True,
            providers_override=[after_reset],
        )
    )
    assert after_reset.calls == 1
    assert claimed.results[0].biz_date == "2026-11-19"


def test_business_date_resets_at_configured_start() -> None:
    tz = timezone(timedelta(hours=8))
    before = datetime(2026, 11, 19, 9, 59, tzinfo=tz)
    at_start = datetime(2026, 11, 19, 10, 0, tzinfo=tz)

    assert business_date("10:00", "Asia/Shanghai", before) == "2026-11-18"
    assert business_date("10:00", "Asia/Shanghai", at_start) == "2026-11-19"
    assert business_date("00:00", "Asia/Shanghai", before) == "2026-11-19"


def test_success_notifications_are_not_lost_when_business_date_changes(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "test.db")
    notifier = RecordingNotifier()
    runner = CheckinRunner(
        storage=storage,
        notifier=notifier,
        lock_path=tmp_path / "run.lock",
    )
    provider = SequenceBusinessDayProvider(["2026-11-18", "2026-11-19"])

    asyncio.run(runner.run(providers_override=[provider]))
    asyncio.run(runner.run(providers_override=[provider]))

    assert provider.calls == 2
    assert len(notifier.sent) == 2


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


def test_runner_times_out_provider_and_continues(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    notifier = RecordingNotifier()
    runner = CheckinRunner(
        storage=storage,
        notifier=notifier,
        lock_path=tmp_path / "run.lock",
        provider_timeout_seconds=0.05,
    )
    slow = SlowProvider()
    good = FakeProvider(provider_id="good", display_name="GoodTool")

    summary = asyncio.run(runner.run(providers_override=[slow, good]))

    assert len(summary.results) == 2
    assert summary.results[0].status == CheckinStatus.FAILED
    assert summary.results[0].raw_error_type == "ProviderTimeout"
    assert "超时" in summary.results[0].message
    assert summary.results[1].status == CheckinStatus.SUCCESS
    assert slow.calls == 1
    assert good.calls == 1

    # 超时后运行锁必须已释放。
    with acquire_run_lock(runner.lock_path):
        pass


def test_runner_failure_uses_provider_business_date(tmp_path: Path) -> None:
    class BrokenWithBusinessDay(RaisingProvider):
        def business_date(
            self,
            config: ProviderConfig,
            tz_name: str = "Asia/Shanghai",
            *,
            now: Optional[datetime] = None,
        ) -> str:
            return "2026-11-18"

    storage = Storage(tmp_path / "test.db")
    runner = CheckinRunner(
        storage=storage,
        notifier=RecordingNotifier(),
        lock_path=tmp_path / "run.lock",
    )

    summary = asyncio.run(runner.run(providers_override=[BrokenWithBusinessDay()]))

    assert summary.results[0].status == CheckinStatus.FAILED
    assert summary.results[0].biz_date == "2026-11-18"
    records = storage.get_history(limit=10)["records"]
    assert records[0]["biz_date"] == "2026-11-18"


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


def test_storage_today_summary_prefers_positive_streak_and_success_message(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "test.db")
    biz_date = "2026-11-19"
    storage.save_checkin_result(
        "run-1",
        CheckinResult(
            provider_id="workbuddy",
            display_name="WorkBuddy",
            status=CheckinStatus.SUCCESS,
            message="WorkBuddy 今日签到成功，+100 积分，已连续 33 天。",
            biz_date=biz_date,
            snapshot=CreditSnapshot(delta=100, streak_days=33),
        ),
    )
    storage.save_checkin_result(
        "run-2",
        CheckinResult(
            provider_id="workbuddy",
            display_name="WorkBuddy",
            status=CheckinStatus.ALREADY_DONE,
            message="今日已完成签到。",
            biz_date=biz_date,
            snapshot=CreditSnapshot(delta=0, streak_days=0),
        ),
    )

    summary = storage.get_today_summary("workbuddy", biz_date)
    assert summary is not None
    assert summary["status"] == "success"
    assert summary["delta"] == 100
    assert summary["streak_days"] == 33
    assert "已连续 33 天" in summary["message"]

    latest_snapshot = storage.get_latest_snapshot_record("workbuddy")
    assert latest_snapshot is not None
    assert latest_snapshot["streak_days"] == 33


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


def test_provider_config_business_day_start_persists(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "business_day.db")
    qoder_cfg = storage.get_provider_config("qoder")
    assert qoder_cfg.business_day_start == "10:00"
    assert storage.get_provider_config("workbuddy").business_day_start == "00:00"

    workbuddy_cfg = storage.get_provider_config("workbuddy")
    workbuddy_cfg.business_day_start = "11:30"
    storage.save_provider_config(workbuddy_cfg)
    assert storage.get_provider_config("workbuddy").business_day_start == "11:30"
    assert storage.get_provider_config("qoder").business_day_start == "10:00"


def test_storage_normalizes_fixed_qoder_business_day(tmp_path: Path) -> None:
    db_path = tmp_path / "fixed_business_day.db"
    storage = Storage(db_path)
    qoder_cfg = storage.get_provider_config("qoder")
    qoder_cfg.business_day_start = "00:00"
    storage.save_provider_config(qoder_cfg)
    assert storage.get_provider_config("qoder").business_day_start == "00:00"

    reopened = Storage(db_path)
    assert reopened.get_provider_config("qoder").business_day_start == "10:00"


def test_storage_migrates_business_day_start_for_qoder(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_business_day.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
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
            'qoder', 1, '00:00', 10, '{}', '2026-11-10T00:00:00+00:00'
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

    assert storage.get_provider_config("qoder").business_day_start == "10:00"
    assert storage.get_provider_config("workbuddy").business_day_start == "00:00"


def test_effective_jitter_uses_provider_override_when_set() -> None:
    settings = GlobalSettings(jitter_minutes=15)
    cfg = ProviderConfig(provider_id="fake")
    assert CheckinRunner._effective_jitter_minutes(cfg, settings) == 15

    cfg.jitter_minutes = 5
    cfg.extra["jitter_override"] = True
    assert CheckinRunner._effective_jitter_minutes(cfg, settings) == 5


def test_task_timeout_tracks_enabled_jitter_budget() -> None:
    settings = GlobalSettings(jitter_minutes=15)
    enabled = ProviderConfig(provider_id="alpha")
    second = ProviderConfig(provider_id="beta")
    disabled = ProviderConfig(provider_id="gamma", enabled=False)

    assert CheckinRunner.estimate_task_timeout_minutes(settings, [enabled]) == 25
    assert (
        CheckinRunner.estimate_task_timeout_minutes(settings, [enabled, second]) == 40
    )
    assert (
        CheckinRunner.estimate_task_timeout_minutes(settings, [enabled, disabled])
        == 25
    )

    override = ProviderConfig(
        provider_id="delta",
        jitter_minutes=120,
        extra={"jitter_override": True},
    )
    assert CheckinRunner.estimate_task_timeout_minutes(settings, [override]) == 130
    assert (
        CheckinRunner.estimate_task_timeout_minutes(
            settings, [override, override, override]
        )
        == 370
    )


def test_manual_run_persists_maintenance_state(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "manual-maintenance-state.db")
    runner = CheckinRunner(
        storage=storage,
        notifier=RecordingNotifier(),
        lock_path=tmp_path / "run.lock",
    )

    asyncio.run(
        runner.run(
            providers_override=[FakeProvider()],
            trigger_type=TriggerType.MANUAL_WEB,
        )
    )

    assert storage.get_maintenance_state().get("last_cleanup_at")


def test_manual_run_triggers_due_maintenance(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path / "manual-maintenance.db")
    runner = CheckinRunner(
        storage=storage,
        notifier=RecordingNotifier(),
        lock_path=tmp_path / "run.lock",
    )
    calls: list[tuple[bool, bool]] = []

    monkeypatch.setattr(storage, "maintenance_due", lambda settings: (True, False))

    def fake_maintenance(settings, *, vacuum=False, backup=True):
        calls.append((vacuum, backup))
        return {"deleted": {}, "backup": None}

    monkeypatch.setattr(storage, "run_maintenance", fake_maintenance)

    asyncio.run(
        runner.run(
            providers_override=[FakeProvider()],
            trigger_type=TriggerType.MANUAL_WEB,
        )
    )
    assert calls == [(False, True)]

    calls.clear()
    asyncio.run(
        runner.run(
            providers_override=[FakeProvider()],
            trigger_type=TriggerType.MANUAL_WEB,
            dry_run=True,
        )
    )
    assert calls == []


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


def test_models_validate_assignment_and_timezone() -> None:
    from pydantic import ValidationError

    settings = GlobalSettings()
    with pytest.raises(ValidationError):
        settings.schedule_time = "invalid"
    with pytest.raises(ValidationError):
        GlobalSettings(timezone="Mars/Olympus")
    assert GlobalSettings(timezone="UTC").timezone == "UTC"

    cfg = ProviderConfig(provider_id="workbuddy")
    with pytest.raises(ValidationError):
        cfg.earliest_time = "9:00"
    with pytest.raises(ValidationError):
        cfg.jitter_minutes = 999


def test_storage_run_id_index_history_biz_date_and_has_more(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "history_filter.db")
    with storage._connect() as conn:
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert "idx_records_run_id" in indexes

    for idx, day in enumerate(("2026-11-17", "2026-11-18", "2026-11-18")):
        storage.save_checkin_result(
            f"run-{idx}",
            CheckinResult(
                provider_id="workbuddy",
                display_name="WorkBuddy",
                status=CheckinStatus.SUCCESS,
                message=f"rec-{idx}",
                biz_date=day,
                snapshot=CreditSnapshot(delta=10),
            ),
        )

    page1 = storage.get_history(limit=2, month="2026-11", offset=0)
    assert len(page1["records"]) == 2
    assert page1["has_more"] is True

    page2 = storage.get_history(limit=2, month="2026-11", offset=2)
    assert len(page2["records"]) == 1
    assert page2["has_more"] is False

    exact_page = storage.get_history(limit=3, month="2026-11", offset=0)
    assert exact_page["has_more"] is False

    day_only = storage.get_history(
        limit=10, month="2026-11", biz_date="2026-11-17"
    )
    assert [r["biz_date"] for r in day_only["records"]] == ["2026-11-17"]
    assert {c["biz_date"] for c in day_only["calendar"]} == {
        "2026-11-17",
        "2026-11-18",
    }


def test_storage_count_consecutive_failures_only_counts_action_required(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "streak_fail.db")
    storage.save_checkin_result(
        "r1",
        CheckinResult(
            provider_id="workbuddy",
            display_name="WorkBuddy",
            status=CheckinStatus.FAILED,
            message="transient network error",
            biz_date="2026-11-18",
        ),
    )
    storage.save_checkin_result(
        "r2",
        CheckinResult(
            provider_id="workbuddy",
            display_name="WorkBuddy",
            status=CheckinStatus.AUTH_EXPIRED,
            message="auth expired",
            biz_date="2026-11-18",
        ),
    )
    assert storage.count_consecutive_failures("workbuddy") == 1

    storage.save_checkin_result(
        "r3",
        CheckinResult(
            provider_id="workbuddy",
            display_name="WorkBuddy",
            status=CheckinStatus.NEEDS_MANUAL,
            message="captcha",
            biz_date="2026-11-18",
        ),
    )
    assert storage.count_consecutive_failures("workbuddy") == 2


def test_config_security_mixed_unc_port_and_resolved_data_dir(
    tmp_path: Path, monkeypatch
) -> None:
    from geteggs.core.config import (
        is_unc_path,
        validate_local_file_path,
        validate_provider_base_url,
    )

    for unc in (r"\/attacker\share\evil.exe", r"/\attacker\share\evil.exe"):
        assert is_unc_path(unc) is True
        with pytest.raises(ValueError, match="UNC"):
            validate_local_file_path(
                unc, field_label="exe", require_exe_on_windows=True, allow_custom=False
            )
        with pytest.raises(ValueError, match="UNC"):
            validate_local_file_path(unc, field_label="exe", allow_custom=True)

    with pytest.raises(ValueError, match="端口"):
        validate_provider_base_url("qoder", "https://openapi.qoder.com.cn:abc")
    with pytest.raises(ValueError, match="查询参数"):
        validate_provider_base_url("qoder", "https://openapi.qoder.com.cn?x=1")

    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    resolved = validate_local_file_path(
        "session.json",
        field_label="browser_state_path",
        restrict_to_data_dir=True,
        require_json=True,
        allow_custom=False,
    )
    assert Path(resolved).is_absolute()
    assert Path(resolved) == (tmp_path / "session.json").resolve()


def test_storage_get_settings_falls_back_on_unavailable_persisted_timezone(
    tmp_path: Path,
) -> None:
    import json

    storage = Storage(tmp_path / "tz_compat.db")
    with storage._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings(key, value_json, updated_at) "
            "VALUES('global', ?, '2026-01-01T00:00:00Z')",
            (json.dumps({"timezone": "Invalid/NoSuchZone"}),),
        )
    loaded = storage.get_settings()
    assert loaded.timezone == "Asia/Shanghai"
