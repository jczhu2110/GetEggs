"""SQLite WAL-mode ledger for check-in records, provider configs, and settings."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from geteggs.core.config import get_db_path, get_timezone, today_biz_date
from geteggs.core.models import (
    CheckinResult,
    CheckinStatus,
    GlobalSettings,
    LogEvent,
    ProviderConfig,
    TriggerType,
    utc_now_iso,
)

DEFAULT_DISABLED_PROVIDERS = {"qoder", "trae"}
# Keep in sync with provider fixed_business_day_start values.
FIXED_BUSINESS_DAY_START = {"qoder": "10:00", "trae": "00:00"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_configs (
    provider_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    earliest_time TEXT NOT NULL DEFAULT '00:00',
    business_day_start TEXT NOT NULL DEFAULT '00:00',
    jitter_minutes INTEGER NOT NULL DEFAULT 10,
    extra_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkin_runs (
    run_id TEXT PRIMARY KEY,
    trigger_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkin_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    biz_date TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    raw_error_type TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_provider_date
    ON checkin_records(provider_id, biz_date, id DESC);

CREATE INDEX IF NOT EXISTS idx_records_biz_date
    ON checkin_records(biz_date);

CREATE INDEX IF NOT EXISTS idx_records_run_id
    ON checkin_records(run_id);

CREATE INDEX IF NOT EXISTS idx_checkin_runs_started_at
    ON checkin_runs(started_at);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    level TEXT NOT NULL,
    dedup_key TEXT,
    biz_date TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    success INTEGER NOT NULL,
    error_message TEXT,
    sent_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_dedup
    ON notifications(dedup_key, biz_date);

CREATE INDEX IF NOT EXISTS idx_notifications_sent_at
    ON notifications(sent_at);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    provider_id TEXT,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_events_run
    ON run_events(run_id, id);

CREATE INDEX IF NOT EXISTS idx_run_events_ts
    ON run_events(ts);
"""


class Storage:
    """Thread-safe short-lived SQLite helper using WAL mode."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON;")
            with conn:
                yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(SCHEMA_SQL)
            self._drop_legacy_columns(
                conn,
                "checkin_records",
                ("balance", "streak_days", "delta", "expiring_soon", "expiring_at"),
            )
            self._drop_legacy_columns(conn, "provider_configs", ("checkin_credits",))
            self._add_missing_business_day_column(conn)
            self._normalize_fixed_business_day(conn)

    @staticmethod
    def _normalize_fixed_business_day(conn: sqlite3.Connection) -> None:
        """Keep provider-locked business-day starts correct in older databases."""
        for provider_id, start in FIXED_BUSINESS_DAY_START.items():
            conn.execute(
                "UPDATE provider_configs SET business_day_start = ? "
                "WHERE provider_id = ? AND business_day_start <> ?",
                (start, provider_id, start),
            )

    @staticmethod
    def _add_missing_business_day_column(conn: sqlite3.Connection) -> None:
        """Add the configurable business-day start time to older databases."""
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(provider_configs)")
        }
        if "business_day_start" in columns:
            return
        added = False
        try:
            conn.execute(
                "ALTER TABLE provider_configs "
                "ADD COLUMN business_day_start TEXT NOT NULL DEFAULT '00:00'"
            )
            added = True
        except sqlite3.OperationalError as exc:
            # Another process may have completed the migration between the check and ALTER.
            if "duplicate column" not in str(exc).lower():
                raise
        if added:
            # Qoder CN 的每日活动以 10:00 重置，首次迁移时写入正确默认值。
            conn.execute(
                "UPDATE provider_configs SET business_day_start = '10:00' "
                "WHERE provider_id = 'qoder'"
            )

    @staticmethod
    def _drop_legacy_columns(
        conn: sqlite3.Connection,
        table: str,
        retired_columns: tuple[str, ...],
    ) -> None:
        """Remove retired credit/streak columns from older databases while preserving rows."""
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in retired_columns:
            if column not in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            except sqlite3.OperationalError as exc:
                # Another process may have completed the migration between the check and ALTER.
                if "no such column" not in str(exc).lower():
                    raise

    # ---- Global settings ----

    def get_settings(self) -> GlobalSettings:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM app_settings WHERE key = 'global'"
            ).fetchone()
        if not row:
            return GlobalSettings()
        raw_json = row["value_json"]
        try:
            return GlobalSettings.model_validate_json(raw_json)
        except ValueError:
            data = json.loads(raw_json)
            if isinstance(data, dict) and "timezone" in data:
                data["timezone"] = "Asia/Shanghai"
                return GlobalSettings.model_validate(data)
            raise

    def save_settings(self, settings: GlobalSettings) -> GlobalSettings:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value_json, updated_at)
                VALUES ('global', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (settings.model_dump_json(), now),
            )
        return settings

    # ---- Provider configs ----

    def get_provider_config(self, provider_id: str) -> ProviderConfig:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM provider_configs WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        if not row:
            return ProviderConfig(
                provider_id=provider_id,
                enabled=provider_id not in DEFAULT_DISABLED_PROVIDERS,
                business_day_start=FIXED_BUSINESS_DAY_START.get(provider_id, "00:00"),
            )
        return ProviderConfig(
            provider_id=row["provider_id"],
            enabled=bool(row["enabled"]),
            earliest_time=row["earliest_time"],
            business_day_start=row["business_day_start"],
            jitter_minutes=row["jitter_minutes"],
            extra=json.loads(row["extra_json"] or "{}"),
        )

    def save_provider_config(self, cfg: ProviderConfig) -> ProviderConfig:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_configs (
                    provider_id, enabled, earliest_time, business_day_start,
                    jitter_minutes, extra_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    earliest_time = excluded.earliest_time,
                    business_day_start = excluded.business_day_start,
                    jitter_minutes = excluded.jitter_minutes,
                    extra_json = excluded.extra_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cfg.provider_id,
                    int(cfg.enabled),
                    cfg.earliest_time,
                    cfg.business_day_start,
                    cfg.jitter_minutes,
                    json.dumps(cfg.extra, ensure_ascii=False),
                    now,
                ),
            )
        return cfg

    # ---- Run & check-in ledger ----

    def record_run_start(self, run_id: str, trigger_type: TriggerType) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO checkin_runs (run_id, trigger_type, started_at, status)
                VALUES (?, ?, ?, 'running')
                """,
                (run_id, trigger_type.value, utc_now_iso()),
            )

    def record_run_finish(self, run_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE checkin_runs
                SET finished_at = ?, status = ?
                WHERE run_id = ?
                """,
                (utc_now_iso(), status, run_id),
            )

    def save_checkin_result(self, run_id: str, result: CheckinResult) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO checkin_records (
                    run_id, provider_id, display_name, biz_date, status,
                    message, raw_error_type, duration_ms, dry_run, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.provider_id,
                    result.display_name,
                    result.biz_date,
                    result.status.value,
                    result.message,
                    result.raw_error_type,
                    result.duration_ms,
                    int(result.dry_run),
                    utc_now_iso(),
                ),
            )

    def get_today_completed_record(
        self, provider_id: str, biz_date: str
    ) -> Optional[dict[str, Any]]:
        """Return the latest completed non-dry-run record for a business day."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM checkin_records
                WHERE provider_id = ?
                  AND biz_date = ?
                  AND dry_run = 0
                  AND status IN ('success', 'already_done')
                ORDER BY id DESC
                LIMIT 1
                """,
                (provider_id, biz_date),
            ).fetchone()
        return dict(row) if row else None

    def count_consecutive_failures(self, provider_id: str) -> int:
        """Count recent consecutive action-required failures for circuit-breaking."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status FROM checkin_records
                WHERE provider_id = ? AND dry_run = 0
                ORDER BY id DESC
                LIMIT 10
                """,
                (provider_id,),
            ).fetchall()
        count = 0
        for row in rows:
            status = CheckinStatus(row["status"])
            if status.requires_user_action:
                count += 1
            else:
                break
        return count

    def get_latest_record(self, provider_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM checkin_records
                WHERE provider_id = ? AND dry_run = 0
                ORDER BY id DESC
                LIMIT 1
                """,
                (provider_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_today_summary(
        self, provider_id: str, biz_date: str
    ) -> Optional[dict[str, Any]]:
        """Aggregate all non-dry-run attempts for one provider on a business day.

        A later failed retry must not erase an earlier successful check-in.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, message, created_at
                FROM checkin_records
                WHERE provider_id = ? AND biz_date = ? AND dry_run = 0
                ORDER BY id ASC
                """,
                (provider_id, biz_date),
            ).fetchall()

        if not rows:
            return None

        completed = [r["status"] for r in rows if r["status"] in ("success", "already_done")]
        latest = rows[-1]
        latest_success = next((r for r in reversed(rows) if r["status"] == "success"), None)
        message = (latest_success or latest)["message"]
        if completed and latest["status"] not in ("success", "already_done"):
            message = f"今日已完成签到；最近一次重跑异常：{latest['message']}"

        return {
            "provider_id": provider_id,
            "biz_date": biz_date,
            "status": "success" if "success" in completed else (completed[0] if completed else latest["status"]),
            "ok": bool(completed),
            "message": message,
            "last_status": latest["status"],
            "record_count": len(rows),
            "updated_at": latest["created_at"],
        }

    def get_history(
        self,
        limit: int = 100,
        provider_id: Optional[str] = None,
        month: Optional[str] = None,
        offset: int = 0,
        biz_date: Optional[str] = None,
    ) -> dict[str, Any]:
        base_clauses = ["dry_run = 0"]
        base_params: list[Any] = []
        if provider_id:
            base_clauses.append("provider_id = ?")
            base_params.append(provider_id)
        if month:
            base_clauses.append("biz_date LIKE ?")
            base_params.append(f"{month}%")

        record_clauses = list(base_clauses)
        record_params = list(base_params)
        if biz_date:
            record_clauses.append("biz_date = ?")
            record_params.append(biz_date)

        base_where = " AND ".join(base_clauses)
        record_where = " AND ".join(record_clauses)
        safe_offset = max(0, offset)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM checkin_records
                WHERE {record_where}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (*record_params, limit + 1, safe_offset),
            ).fetchall()

            calendar_rows = conn.execute(
                f"""
                SELECT biz_date,
                       MAX(CASE WHEN status IN ('success', 'already_done') THEN 1 ELSE 0 END) AS ok
                FROM checkin_records
                WHERE {base_where}
                GROUP BY biz_date
                ORDER BY biz_date ASC
                """,
                tuple(base_params),
            ).fetchall()

        has_more = len(rows) > limit
        page_rows = rows[:limit]
        return {
            "records": [dict(r) for r in page_rows],
            "calendar": [dict(r) for r in calendar_rows],
            "offset": safe_offset,
            "limit": limit,
            "has_more": has_more,
        }

    # ---- Run history ----

    def save_log_event(self, run_id: str, event: LogEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_events (run_id, ts, level, provider_id, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event.timestamp,
                    event.level,
                    event.provider_id,
                    event.message,
                ),
            )

    def get_run_events(
        self, run_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, level, provider_id, message
                FROM run_events
                WHERE run_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_runs(self, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.run_id, r.trigger_type, r.started_at, r.finished_at, r.status,
                       COUNT(c.id) AS record_count,
                       COALESCE(SUM(
                           CASE WHEN c.status IN ('success', 'already_done') THEN 1 ELSE 0 END
                       ), 0) AS ok_count,
                       COALESCE(MAX(c.dry_run), 0) AS dry_run
                FROM (
                    SELECT run_id, trigger_type, started_at, finished_at, status
                    FROM checkin_runs
                    ORDER BY started_at DESC
                    LIMIT ? OFFSET ?
                ) AS r
                LEFT JOIN checkin_records AS c ON c.run_id = r.run_id
                GROUP BY r.run_id
                ORDER BY r.started_at DESC
                """,
                (limit, max(0, offset)),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_run_detail(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            run = conn.execute(
                """
                SELECT run_id, trigger_type, started_at, finished_at, status
                FROM checkin_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if not run:
                return None
            records = conn.execute(
                """
                SELECT provider_id, display_name, biz_date, status,
                       message, raw_error_type, duration_ms, dry_run, created_at
                FROM checkin_records
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
            events = conn.execute(
                """
                SELECT ts, level, provider_id, message
                FROM run_events
                WHERE run_id = ?
                ORDER BY id ASC
                LIMIT 500
                """,
                (run_id,),
            ).fetchall()
        return {
            "run": dict(run),
            "records": [dict(r) for r in records],
            "events": [dict(event) for event in events],
        }

    # ---- Notification history ----

    def get_notifications(
        self, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, channel, level, dedup_key, biz_date, title, message,
                       success, error_message, sent_at
                FROM notifications
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, max(0, offset)),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- Maintenance ----

    def get_maintenance_state(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM app_settings WHERE key = 'maintenance'"
            ).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row["value_json"])
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def save_maintenance_state(self, state: dict[str, Any]) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value_json, updated_at)
                VALUES ('maintenance', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (json.dumps(state, ensure_ascii=False), now),
            )

    def get_maintenance_info(self) -> dict[str, Any]:
        with self._connect() as conn:
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "checkin_records",
                    "checkin_runs",
                    "run_events",
                    "notifications",
                )
            }
        state = self.get_maintenance_state()
        backup_dir = self.db_path.parent / "backups"
        backup_count = (
            len(list(backup_dir.glob("geteggs-*.db"))) if backup_dir.exists() else 0
        )
        size_paths = [
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ]
        db_size = 0
        for path in size_paths:
            try:
                db_size += path.stat().st_size
            except OSError:
                continue
        return {
            "db_path": str(self.db_path),
            "db_size": db_size,
            "counts": counts,
            "backup_count": backup_count,
            "last_cleanup_at": state.get("last_cleanup_at"),
            "last_vacuum_at": state.get("last_vacuum_at"),
        }

    @staticmethod
    def _cleanup_cutoffs(settings: GlobalSettings) -> dict[str, str]:
        now_utc = datetime.now(timezone.utc)
        tz = get_timezone(settings.timezone)
        records_cutoff = (
            datetime.now(tz).date() - timedelta(days=settings.ledger_retention_days)
        ).strftime("%Y-%m-%d")
        stale_minutes = max(30, settings.jitter_minutes + 20)
        return {
            "now": now_utc.isoformat(),
            "stale": (now_utc - timedelta(minutes=stale_minutes)).isoformat(),
            "events": (
                now_utc - timedelta(days=settings.run_events_retention_days)
            ).isoformat(),
            "notifications": (
                now_utc - timedelta(days=settings.notification_retention_days)
            ).isoformat(),
            "runs": (
                now_utc - timedelta(days=settings.run_retention_days)
            ).isoformat(),
            "records": records_cutoff,
        }

    def mark_stale_runs(
        self,
        settings: GlobalSettings,
        *,
        exclude_run_id: Optional[str] = None,
    ) -> int:
        """Mark long-abandoned 'running' batches as 'interrupted'."""
        cutoffs = self._cleanup_cutoffs(settings)
        with self._connect() as conn:
            if exclude_run_id:
                return conn.execute(
                    """
                    UPDATE checkin_runs
                    SET status = 'interrupted', finished_at = ?
                    WHERE status = 'running' AND started_at < ? AND run_id <> ?
                    """,
                    (cutoffs["now"], cutoffs["stale"], exclude_run_id),
                ).rowcount
            return conn.execute(
                """
                UPDATE checkin_runs
                SET status = 'interrupted', finished_at = ?
                WHERE status = 'running' AND started_at < ?
                """,
                (cutoffs["now"], cutoffs["stale"]),
            ).rowcount

    def cleanup_old_data(self, settings: GlobalSettings) -> dict[str, int]:
        cutoffs = self._cleanup_cutoffs(settings)
        with self._connect() as conn:
            stale_runs = conn.execute(
                """
                UPDATE checkin_runs
                SET status = 'interrupted', finished_at = ?
                WHERE status = 'running' AND started_at < ?
                """,
                (cutoffs["now"], cutoffs["stale"]),
            ).rowcount
            run_events = conn.execute(
                "DELETE FROM run_events WHERE ts < ?", (cutoffs["events"],)
            ).rowcount
            notifications = conn.execute(
                "DELETE FROM notifications WHERE sent_at < ?",
                (cutoffs["notifications"],),
            ).rowcount
            checkin_runs = conn.execute(
                """
                DELETE FROM checkin_runs
                WHERE started_at < ?
                  AND status IN ('completed', 'error', 'interrupted')
                """,
                (cutoffs["runs"],),
            ).rowcount
            checkin_records = conn.execute(
                "DELETE FROM checkin_records WHERE biz_date < ?",
                (cutoffs["records"],),
            ).rowcount
        return {
            "stale_runs": stale_runs,
            "run_events": run_events,
            "notifications": notifications,
            "checkin_runs": checkin_runs,
            "checkin_records": checkin_records,
        }

    def maintenance_due(self, settings: GlobalSettings) -> tuple[bool, bool]:
        """Return (cleanup_due, vacuum_due) based on persisted timestamps."""
        if not settings.auto_cleanup_enabled:
            return False, False
        state = self.get_maintenance_state()
        now = datetime.now(timezone.utc)

        def age_days(value: Any) -> Optional[float]:
            if not isinstance(value, str) or not value:
                return None
            try:
                moment = datetime.fromisoformat(value)
            except ValueError:
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return (now - moment).total_seconds() / 86400

        cleanup_age = age_days(state.get("last_cleanup_at"))
        cleanup_due = cleanup_age is None or cleanup_age >= 1.0
        vacuum_age = age_days(state.get("last_vacuum_at"))
        vacuum_due = cleanup_due and (
            vacuum_age is None or vacuum_age >= settings.vacuum_interval_days
        )
        return cleanup_due, vacuum_due

    def backup(self, keep: int = 5) -> Optional[Path]:
        backup_dir = self.db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest_path = backup_dir / f"geteggs-{stamp}.db"
        with self._connect() as source:
            dest = sqlite3.connect(dest_path)
            try:
                source.backup(dest)
            finally:
                dest.close()
        keep = max(1, keep)
        backups = sorted(backup_dir.glob("geteggs-*.db"), key=lambda path: path.name)
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
        return dest_path

    def vacuum(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.isolation_level = None
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.execute("VACUUM;")
            conn.execute("PRAGMA optimize;")
        finally:
            conn.close()

    def run_maintenance(
        self,
        settings: GlobalSettings,
        *,
        vacuum: bool = False,
        backup: bool = True,
    ) -> dict[str, Any]:
        backup_path = self.backup() if backup else None
        deleted = self.cleanup_old_data(settings)
        vacuumed = False
        vacuum_error: Optional[str] = None
        if vacuum:
            try:
                self.vacuum()
                vacuumed = True
            except Exception as exc:  # noqa: BLE001
                vacuum_error = str(exc)
        state = self.get_maintenance_state()
        state["last_cleanup_at"] = utc_now_iso()
        if vacuumed:
            state["last_vacuum_at"] = state["last_cleanup_at"]
        self.save_maintenance_state(state)
        return {
            "deleted": deleted,
            "backup": str(backup_path) if backup_path else None,
            "vacuumed": vacuumed,
            "vacuum_error": vacuum_error,
            "info": self.get_maintenance_info(),
        }

    # ---- Notifications dedup & audit ----

    def was_notified_today(self, dedup_key: str, biz_date: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM notifications
                WHERE dedup_key = ? AND biz_date = ? AND success = 1
                LIMIT 1
                """,
                (dedup_key, biz_date),
            ).fetchone()
        return row is not None

    def save_notification(
        self,
        channel: str,
        level: str,
        title: str,
        message: str,
        success: bool,
        biz_date: Optional[str] = None,
        dedup_key: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notifications (
                    channel, level, dedup_key, biz_date, title, message,
                    success, error_message, sent_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel,
                    level,
                    dedup_key,
                    biz_date or today_biz_date(),
                    title,
                    message,
                    int(success),
                    error_message,
                    utc_now_iso(),
                ),
            )
