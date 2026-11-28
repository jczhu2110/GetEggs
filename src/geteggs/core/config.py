"""Runtime path resolution and timezone helpers."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CUSTOM_CONFIG_ENV = "GETEGGS_ALLOW_CUSTOM_CONFIG"
MAX_CONFIG_STR_LENGTH = 1024

PROVIDER_ALLOWED_HOSTS: dict[str, tuple[str, ...]] = {
    "workbuddy": ("codebuddy.cn", "workbuddy.cn", "copilot.tencent.com"),
    "qoder": ("qoder.com", "qoder.com.cn"),
    "trae": ("trae.cn", "trae.com.cn"),
}


def allow_custom_config() -> bool:
    """Whether relaxed validation for custom base URLs and local paths is enabled."""
    raw = (os.environ.get(CUSTOM_CONFIG_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def is_allowed_provider_host(provider_id: str, hostname: str) -> bool:
    """Check whether a hostname matches a provider's official domain allowlist."""
    allowed = PROVIDER_ALLOWED_HOSTS.get(provider_id)
    if not allowed:
        return True
    host = hostname.strip().lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed)


def _has_control_chars(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def is_unc_path(path_text: str) -> bool:
    """Return True when a path string targets a UNC/SMB network share."""
    cleaned = path_text.strip()
    return cleaned.replace("/", "\\").startswith("\\\\")


def validate_provider_base_url(
    provider_id: str,
    value: str,
    *,
    allow_custom: Optional[bool] = None,
) -> str:
    """Validate and normalize a provider API base URL."""
    cleaned = (value or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if len(cleaned) > MAX_CONFIG_STR_LENGTH:
        raise ValueError(f"API Base URL 长度不能超过 {MAX_CONFIG_STR_LENGTH} 个字符。")
    if _has_control_chars(cleaned) or any(ch.isspace() for ch in cleaned):
        raise ValueError("API Base URL 不能包含空白或控制字符。")

    relaxed = allow_custom_config() if allow_custom is None else allow_custom
    parsed = urlparse(cleaned)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("API Base URL 缺少有效主机名，请使用完整 URL（例如 https://...）。")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("API Base URL 不能包含内嵌凭据。")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("API Base URL 端口号无效。") from exc
    if parsed.query or parsed.fragment:
        raise ValueError("API Base URL 不能包含查询参数或锚点。")
    if relaxed:
        if scheme not in {"http", "https"}:
            raise ValueError("API Base URL 协议必须为 http 或 https。")
        return cleaned

    if scheme != "https":
        raise ValueError(
            f"API Base URL 必须使用 https://（如需允许自定义调试地址，请设置环境变量 {CUSTOM_CONFIG_ENV}=1）。"
        )
    if not is_allowed_provider_host(provider_id, hostname):
        allowed = "、".join(PROVIDER_ALLOWED_HOSTS.get(provider_id, ()))
        raise ValueError(
            f"API Base URL 域名不在 {provider_id} 官方白名单（{allowed}）内"
            f"（如需允许自定义地址，请设置环境变量 {CUSTOM_CONFIG_ENV}=1）。"
        )
    return cleaned


def validate_local_file_path(
    value: str,
    *,
    field_label: str,
    require_exe_on_windows: bool = False,
    restrict_to_data_dir: bool = False,
    require_json: bool = False,
    allow_custom: Optional[bool] = None,
) -> str:
    """Validate a local filesystem path supplied via configuration."""
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > MAX_CONFIG_STR_LENGTH:
        raise ValueError(f"{field_label} 长度不能超过 {MAX_CONFIG_STR_LENGTH} 个字符。")
    if _has_control_chars(cleaned):
        raise ValueError(f"{field_label} 包含非法控制字符。")
    if is_unc_path(cleaned):
        raise ValueError(f"{field_label} 不允许使用网络共享（UNC）路径。")

    relaxed = allow_custom_config() if allow_custom is None else allow_custom
    if relaxed:
        return cleaned

    expanded = Path(cleaned).expanduser()
    if require_exe_on_windows and os.name == "nt":
        if not expanded.is_absolute() or expanded.suffix.lower() != ".exe":
            raise ValueError(
                f"{field_label} 必须是本机绝对 .exe 路径"
                f"（如需允许自定义路径，请设置环境变量 {CUSTOM_CONFIG_ENV}=1）。"
            )
    if restrict_to_data_dir:
        resolved = expanded.resolve(strict=False)
        data_dir = get_data_dir().resolve(strict=False)
        if not resolved.is_relative_to(data_dir):
            raise ValueError(
                f"{field_label} 必须位于本地数据目录（{data_dir}）内"
                f"（如需允许目录外路径，请设置环境变量 {CUSTOM_CONFIG_ENV}=1）。"
            )
        if require_json and resolved.suffix.lower() != ".json":
            raise ValueError(
                f"{field_label} 必须是 .json 文件路径"
                f"（如需允许其他扩展名，请设置环境变量 {CUSTOM_CONFIG_ENV}=1）。"
            )
        if not expanded.is_absolute() or ".." in expanded.parts:
            return str(resolved)
    elif require_json and expanded.suffix.lower() != ".json":
        raise ValueError(f"{field_label} 必须是 .json 文件路径。")
    return cleaned


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


UI_LOG_MAX_BYTES = 512 * 1024
UI_LOG_KEEP_BYTES = 256 * 1024


def get_ui_state_path() -> Path:
    """Return path to the running Web UI state file."""
    return get_data_dir() / "ui-state.json"


def get_ui_log_path() -> Path:
    """Return path to the hidden-mode UI/launcher log file."""
    return get_data_dir() / "geteggs-ui.log"


def trim_log_file(
    path: Path,
    *,
    max_bytes: int = UI_LOG_MAX_BYTES,
    keep_bytes: int = UI_LOG_KEEP_BYTES,
) -> bool:
    """Keep a text log file bounded by retaining the newest tail on a line boundary."""
    try:
        size = path.stat().st_size
        if size <= max_bytes:
            return False
        keep = max(1, min(keep_bytes, max_bytes))
        with path.open("rb") as handle:
            handle.seek(-keep, os.SEEK_END)
            tail = handle.read()
        newline_idx = tail.find(b"\n")
        if 0 <= newline_idx < len(tail) - 1:
            tail = tail[newline_idx + 1 :]
        else:
            tail = tail.decode("utf-8", errors="ignore").encode("utf-8")
        path.write_bytes(tail)
        return True
    except OSError:
        return False


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


def validate_timezone_name(tz_name: str) -> str:
    """Validate that a timezone name can be resolved on this system."""
    cleaned = (tz_name or "").strip()
    if not cleaned:
        raise ValueError("时区不能为空。")
    if cleaned in {"Asia/Shanghai", "UTC"}:
        return cleaned
    try:
        ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ValueError(
            f"无效或当前系统不支持的时区：{cleaned}（默认支持 Asia/Shanghai 或 UTC；如需更多 IANA 时区请安装 tzdata）。"
        ) from exc
    return cleaned


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
