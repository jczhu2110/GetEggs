"""Core Pydantic domain models and enums."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from geteggs.core.config import validate_timezone_name

_HHMM_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def validate_hhmm(value: str) -> str:
    """Normalize and validate a 24-hour HH:MM time string."""
    cleaned = (value or "").strip()
    if not _HHMM_PATTERN.fullmatch(cleaned):
        raise ValueError("时间格式必须为 HH:MM（24 小时制，例如 09:00）。")
    return cleaned


class CheckinStatus(str, Enum):
    """Normalized outcome of a provider check-in execution."""

    SUCCESS = "success"
    ALREADY_DONE = "already_done"
    AUTH_EXPIRED = "auth_expired"
    NEEDS_MANUAL = "needs_manual"
    SCHEMA_CHANGED = "schema_changed"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"

    @property
    def is_done_for_today(self) -> bool:
        """Whether today's check-in goal is satisfied."""
        return self in {CheckinStatus.SUCCESS, CheckinStatus.ALREADY_DONE}

    @property
    def requires_user_action(self) -> bool:
        """Whether the failure requires manual intervention rather than retrying."""
        return self in {
            CheckinStatus.AUTH_EXPIRED,
            CheckinStatus.NEEDS_MANUAL,
            CheckinStatus.SCHEMA_CHANGED,
            CheckinStatus.UNAVAILABLE,
        }


class TriggerType(str, Enum):
    """How a check-in run was initiated."""

    SCHEDULED = "scheduled"
    MISSED_CATCHUP = "missed_catchup"
    MANUAL_CLI = "manual_cli"
    MANUAL_WEB = "manual_web"


class DoctorResult(BaseModel):
    """Read-only environment and credential health report for a provider."""

    provider_id: str
    display_name: str
    client_installed: bool
    client_exe_path: Optional[str] = None
    auth_file_found: bool
    auth_file_path: Optional[str] = None
    auth_readable: bool
    account_label: Optional[str] = None
    token_expires_at: Optional[str] = None
    healthy: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class CheckinResult(BaseModel):
    """Result of executing a single provider check-in."""

    provider_id: str
    display_name: str
    status: CheckinStatus
    message: str
    biz_date: str
    duration_ms: int = 0
    dry_run: bool = False
    raw_error_type: Optional[str] = None


class RunSummary(BaseModel):
    """Batch summary of one check-in execution run."""

    run_id: str
    trigger_type: TriggerType
    started_at: str
    finished_at: str
    results: list[CheckinResult] = Field(default_factory=list)


class ProviderConfig(BaseModel):
    """Per-provider persistent settings stored in SQLite."""

    model_config = ConfigDict(validate_assignment=True)

    provider_id: str
    enabled: bool = True
    earliest_time: str = "00:00"
    business_day_start: str = "00:00"
    jitter_minutes: int = 10
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("earliest_time")
    @classmethod
    def _validate_earliest_time(cls, value: str) -> str:
        return validate_hhmm(value)

    @field_validator("business_day_start")
    @classmethod
    def _validate_business_day_start(cls, value: str) -> str:
        return validate_hhmm(value)

    @field_validator("jitter_minutes")
    @classmethod
    def _validate_provider_jitter_minutes(cls, value: int) -> int:
        if not 0 <= value <= 120:
            raise ValueError("Provider 随机抖动窗口必须在 0-120 分钟之间。")
        return value


class GlobalSettings(BaseModel):
    """Application-level settings."""

    model_config = ConfigDict(validate_assignment=True)

    schedule_time: str = "10:15"
    jitter_minutes: int = 15
    timezone: str = "Asia/Shanghai"
    notify_toast_enabled: bool = True
    notify_on_already_done: bool = False
    auto_catchup_on_logon: bool = True
    auto_cleanup_enabled: bool = True
    run_events_retention_days: int = 30
    notification_retention_days: int = 90
    run_retention_days: int = 180
    ledger_retention_days: int = 365
    vacuum_interval_days: int = 30

    @field_validator("schedule_time")
    @classmethod
    def _validate_schedule_time(cls, value: str) -> str:
        return validate_hhmm(value)

    @field_validator("jitter_minutes")
    @classmethod
    def _validate_jitter_minutes(cls, value: int) -> int:
        if not 0 <= value <= 120:
            raise ValueError("随机抖动窗口必须在 0-120 分钟之间。")
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        return validate_timezone_name(value)

    @field_validator(
        "run_events_retention_days",
        "notification_retention_days",
        "run_retention_days",
        "ledger_retention_days",
        "vacuum_interval_days",
    )
    @classmethod
    def _validate_retention_days(cls, value: int) -> int:
        if not 1 <= value <= 3650:
            raise ValueError("数据保留天数必须在 1-3650 天之间。")
        return value


class LogEvent(BaseModel):
    """Structured event emitted during a run (consumed by CLI & Web SSE)."""

    timestamp: str = Field(default_factory=utc_now_iso)
    level: str = "info"
    provider_id: Optional[str] = None
    message: str
