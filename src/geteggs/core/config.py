"""Runtime path resolution and timezone helpers."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_data_dir() -> Path:
    """Resolve the persistent local data directory for GetEggs."""
    override = os.environ.get("GETEGGS_HOME")
    if override:
        path = Path(override).expanduser().resolve()
    elif os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            path = Path(local_app_data) / "GetEggs"
        else:
            path = Path.home() / ".geteggs"
    else:
        path = Path.home() / ".geteggs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_path() -> Path:
    """Return path to the SQLite ledger database."""
    return get_data_dir() / "geteggs.db"


def get_lock_path() -> Path:
    """Return path to the single-instance execution lock file."""
    return get_data_dir() / "runner.lock"


def get_ui_lock_path() -> Path:
    """Return path to the single-instance Web UI lock file."""
    return get_data_dir() / "ui.lock"


def get_ui_state_path() -> Path:
    """Return path to the running Web UI state file."""
    return get_data_dir() / "ui-state.json"


def get_qoder_pat_path() -> Path:
    """Return path to the DPAPI-protected Qoder CN PAT store."""
    return get_data_dir() / "qoder_pat.json"


def get_trae_auth_path() -> Path:
    """Return path to the DPAPI-protected Trae CN rotated-token cache."""
    return get_data_dir() / "trae_auth.json"


def get_timezone(tz_name: str = "Asia/Shanghai") -> timezone | ZoneInfo:
    """Resolve timezone safely on Windows even when system tzdata is absent."""
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        if tz_name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        return timezone.utc


def today_biz_date(tz_name: str = "Asia/Shanghai") -> str:
    """Return current YYYY-MM-DD date in the configured business timezone."""
    return datetime.now(get_timezone(tz_name)).strftime("%Y-%m-%d")


def business_date(
    start_hhmm: str = "00:00",
    tz_name: str = "Asia/Shanghai",
    now: datetime | None = None,
) -> str:
    """Return the business date for a daily window that starts at `start_hhmm`.

    Times before the start belong to the previous business day, which matches
    providers whose daily activity resets in the morning (for example Qoder CN
    resets at 10:00).
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(get_timezone(tz_name))
    try:
        hour, minute = (int(part) for part in (start_hhmm or "00:00").split(":", 1))
    except ValueError:
        hour, minute = 0, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        hour, minute = 0, 0
    if (moment.hour, moment.minute) < (hour, minute):
        moment -= timedelta(days=1)
    return moment.strftime("%Y-%m-%d")
