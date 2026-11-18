"""WorkBuddy (Tencent CodeBuddy) provider adapter."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from geteggs.core.models import (
    CheckinResult,
    CheckinStatus,
    CreditSnapshot,
    DoctorResult,
    ProviderConfig,
)
from geteggs.providers.base import BaseProvider, LogCallback, extract_first, mask_secret
from geteggs.providers.workbuddy_browser import (
    browser_checkin_session,
    get_browser_state_path,
    interactive_browser_login,
    parse_storage_state,
)

DEFAULT_BASE_URLS = (
    "https://www.codebuddy.cn",
    "https://copilot.tencent.com",
)


@dataclass
class WorkBuddyCredentials:
    """In-memory credentials parsed from local WorkBuddy desktop info or browser state."""

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    uid: Optional[str] = None
    domain: Optional[str] = None
    enterprise_id: Optional[str] = None
    expires_at: Optional[str] = None
    is_expired: bool = False
    source_path: Optional[str] = None
    cookies: Optional[dict[str, str]] = None


class CredentialFormatError(ValueError):
    """Raised when local auth file exists but credentials cannot be parsed cleanly."""

    def __init__(
        self,
        message: str,
        *,
        account_label: Optional[str] = None,
        expires_at: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.account_label = account_label
        self.expires_at = expires_at
        self.domain = domain


def _candidate_auth_paths(config: ProviderConfig) -> list[Path]:
    candidates: list[Path] = []
    custom = config.extra.get("auth_file_path") or os.environ.get("GETEGGS_WORKBUDDY_AUTH_FILE")
    if custom:
        candidates.append(Path(str(custom)).expanduser())

    local_app_data = os.environ.get("LOCALAPPDATA")
    app_data = os.environ.get("APPDATA")
    home = Path.home()

    filenames = ("workbuddy-desktop.info", "workbuddy-desktop-ai.info")
    if local_app_data:
        base_local = Path(local_app_data)
        for root in ("CodeBuddyExtension", "WorkBuddy", "CodeBuddy"):
            for fn in filenames:
                candidates.append(base_local / root / "Data" / "Public" / "auth" / fn)
                candidates.append(base_local / root / fn)

    if app_data:
        base_roam = Path(app_data)
        for root in ("CodeBuddyExtension", "WorkBuddy", "CodeBuddy"):
            for fn in filenames:
                candidates.append(base_roam / root / "Data" / "Public" / "auth" / fn)
                candidates.append(base_roam / root / fn)

    for fn in filenames:
        candidates.append(home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth" / fn)
        candidates.append(home / ".config" / "CodeBuddyExtension" / "Data" / "Public" / "auth" / fn)
        candidates.append(home / ".codebuddy" / fn)
        candidates.append(home / ".workbuddy" / fn)

    # Preserve order while deduplicating.
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in candidates:
        resolved = p.resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _candidate_exe_paths(config: ProviderConfig) -> list[Path]:
    candidates: list[Path] = []
    custom = config.extra.get("client_exe_path")
    if custom:
        candidates.append(Path(str(custom)).expanduser())

    local_app_data = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")

    candidates.extend(
        [
            Path(r"D:\WorkBuddy\WorkBuddy.exe"),
            Path(r"D:\CodeBuddy\CodeBuddy.exe"),
        ]
    )
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "WorkBuddy" / "WorkBuddy.exe")
        candidates.append(Path(local_app_data) / "Programs" / "CodeBuddy" / "CodeBuddy.exe")
    if program_files:
        candidates.append(Path(program_files) / "WorkBuddy" / "WorkBuddy.exe")
        candidates.append(Path(program_files) / "CodeBuddy" / "CodeBuddy.exe")

    for cmd in ("workbuddy", "codebuddy"):
        which_path = shutil.which(cmd)
        if which_path:
            candidates.append(Path(which_path))

    return candidates


def _normalize_base_url(raw: Optional[str]) -> Optional[str]:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().rstrip("/")
    if not cleaned:
        return None
    if cleaned.startswith(("http://", "https://")):
        return cleaned
    return f"https://{cleaned}"


def _parse_expiry(raw_exp: Any) -> tuple[Optional[str], bool]:
    if raw_exp is None:
        return None, False
    try:
        if isinstance(raw_exp, (int, float)):
            # Heuristic: >= 1e12 is milliseconds
            ts = raw_exp / 1000.0 if raw_exp >= 1e12 else float(raw_exp)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(raw_exp, str) and raw_exp.strip():
            text = raw_exp.strip()
            if text.isdigit():
                num = int(text)
                ts = num / 1000.0 if num >= 1e12 else float(num)
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
        else:
            return None, False
        return dt.isoformat(), dt <= datetime.now(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return str(raw_exp), False


def _select_credential_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        for nested_key in ("auth", "session", "credential", "credentials", "data"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict) and (
                "accessToken" in nested or "token" in nested or "access_token" in nested
            ):
                merged = dict(payload)
                merged.update(nested)
                return merged
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    raise CredentialFormatError("登录态文件不是有效的 JSON 对象。")


def _is_encrypted_envelope(val: Any) -> bool:
    return isinstance(val, dict) and (
        bool(val.get("$wbEncrypted"))
        or val.get("encrypted") is True
        or "envelope" in val
        or "ciphertext" in val
    )


def _extract_metadata(obj: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], bool]:
    account = obj.get("account") if isinstance(obj.get("account"), dict) else {}
    user = obj.get("user") if isinstance(obj.get("user"), dict) else {}
    uid = (
        account.get("uid")
        or account.get("id")
        or user.get("uid")
        or obj.get("uid")
        or obj.get("userId")
        or obj.get("user_id")
    )
    domain = obj.get("domain") or obj.get("baseUrl") or obj.get("host") or obj.get("serverUrl")
    enterprise_id = obj.get("enterpriseId") or account.get("enterpriseId")
    expires_at, is_expired = _parse_expiry(
        obj.get("expiresAt") or obj.get("expires_at") or obj.get("expireTime")
    )
    return (
        str(uid).strip() if uid is not None else None,
        str(domain).strip() if isinstance(domain, str) and domain.strip() else None,
        str(enterprise_id).strip() if enterprise_id is not None else None,
        expires_at,
        is_expired,
    )


def parse_auth_file(path: Path) -> WorkBuddyCredentials:
    """Parse WorkBuddy `.info` session JSON defensively without exposing secrets."""
    try:
        raw_text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CredentialFormatError(f"无法读取登录态文件：{exc}") from exc

    if not raw_text:
        raise CredentialFormatError("登录态文件为空，请先在 WorkBuddy 客户端中登录账号。")

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CredentialFormatError(
            "登录态文件不是明文 JSON（可能已被新版客户端加密封装），暂无法直接读取。"
        ) from exc

    obj = _select_credential_dict(parsed)
    uid, domain, enterprise_id, expires_at, is_expired = _extract_metadata(obj)
    masked_uid = mask_secret(uid)

    raw_access = obj.get("accessToken") or obj.get("access_token") or obj.get("token")
    raw_refresh = obj.get("refreshToken") or obj.get("refresh_token")

    if obj.get("encrypted") is True or _is_encrypted_envelope(raw_access) or (
        isinstance(obj.get("ciphertext"), str) and not raw_access
    ):
        env_token = (os.environ.get("GETEGGS_WORKBUDDY_TOKEN") or "").strip()
        if env_token:
            return WorkBuddyCredentials(
                access_token=env_token,
                refresh_token=(
                    raw_refresh.strip()
                    if isinstance(raw_refresh, str) and raw_refresh.strip()
                    else None
                ),
                uid=os.environ.get("GETEGGS_WORKBUDDY_UID") or uid,
                domain=domain,
                enterprise_id=enterprise_id,
                expires_at=expires_at,
                is_expired=False,
                source_path=f"{path} (+GETEGGS_WORKBUDDY_TOKEN)",
            )
        raise CredentialFormatError(
            "检测到新版 WorkBuddy 已采用 $wbEncrypted 本地信封加密存储 accessToken，无法直接读取明文凭证；"
            "可设置环境变量 GETEGGS_WORKBUDDY_TOKEN（已登录网页/授权的 Bearer Token）配合本机账号信息运行，或在客户端内手动签到。",
            account_label=masked_uid,
            expires_at=expires_at,
            domain=domain,
        )

    if not isinstance(raw_access, str) or not raw_access.strip():
        raise CredentialFormatError(
            "登录态文件中未找到有效 accessToken，请确认 WorkBuddy 客户端已完成登录。",
            account_label=masked_uid,
            expires_at=expires_at,
            domain=domain,
        )

    return WorkBuddyCredentials(
        access_token=raw_access.strip(),
        refresh_token=raw_refresh.strip() if isinstance(raw_refresh, str) and raw_refresh.strip() else None,
        uid=uid,
        domain=domain,
        enterprise_id=enterprise_id,
        expires_at=expires_at,
        is_expired=is_expired,
        source_path=str(path),
    )


def _to_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _to_bool(val: Any) -> Optional[bool]:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"true", "1", "yes", "checked", "done", "signed"}:
            return True
        if low in {"false", "0", "no", "unchecked", "pending", "unsigned"}:
            return False
    return None


class WorkBuddyProvider(BaseProvider):
    """Adapter for Tencent WorkBuddy / CodeBuddy desktop check-in."""

    id = "workbuddy"
    display_name = "WorkBuddy"
    description = "腾讯 WorkBuddy（原 CodeBuddy）本地客户端或受控浏览器授权态每日自动签到"
    supports_browser_auth = True

    def __init__(
        self,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        browser_checkin_fn: Optional[Callable[..., Any]] = None,
        browser_auth_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._transport = transport
        self._browser_checkin_fn = browser_checkin_fn or browser_checkin_session
        self._browser_auth_fn = browser_auth_fn or interactive_browser_login

    def detect_client_exe(self, config: ProviderConfig) -> Optional[str]:
        for path in _candidate_exe_paths(config):
            if path.is_file():
                return str(path)
        return None

    def find_auth_file(self, config: ProviderConfig) -> Optional[Path]:
        for path in _candidate_auth_paths(config):
            if path.is_file():
                return path
        return None

    def _resolve_base_urls(
        self, config: ProviderConfig, creds: Optional[WorkBuddyCredentials]
    ) -> list[str]:
        urls: list[str] = []
        for raw in (
            config.extra.get("base_url"),
            creds.domain if creds else None,
            *DEFAULT_BASE_URLS,
        ):
            normalized = _normalize_base_url(raw)
            if normalized and normalized not in urls:
                urls.append(normalized)
        return urls

    def _build_headers(self, creds: WorkBuddyCredentials, base_url: str) -> dict[str, str]:
        domain_host = creds.domain or base_url.replace("https://", "").replace("http://", "").split("/")[0]
        headers = {
            "Content-Type": "application/json",
            "X-Domain": domain_host,
        }
        if creds.access_token:
            headers["Authorization"] = f"Bearer {creds.access_token}"
        if creds.uid:
            headers["X-User-Id"] = creds.uid
        if creds.enterprise_id:
            headers["X-Enterprise-Id"] = creds.enterprise_id
        return headers

    def _load_browser_credentials(
        self, config: ProviderConfig, info_meta: Optional[CredentialFormatError] = None
    ) -> Optional[WorkBuddyCredentials]:
        state_path = get_browser_state_path(config)
        parsed = parse_storage_state(state_path)
        if not parsed:
            return None
        uid = parsed.get("uid")
        domain = parsed.get("domain") or (info_meta.domain if info_meta else None)
        exp_iso, is_exp = _parse_expiry(parsed.get("expires_at"))
        return WorkBuddyCredentials(
            access_token=parsed.get("access_token"),
            refresh_token=parsed.get("refresh_token"),
            uid=uid,
            domain=domain,
            expires_at=exp_iso or (info_meta.expires_at if info_meta else None),
            is_expired=is_exp,
            source_path=str(state_path),
            cookies=parsed.get("cookies") or None,
        )

    async def authorize_browser(self, config: ProviderConfig) -> tuple[bool, str]:
        state_path = get_browser_state_path(config)
        base_url = _normalize_base_url(config.extra.get("base_url"))
        if not base_url:
            auth_path = self.find_auth_file(config)
            if auth_path:
                try:
                    creds = parse_auth_file(auth_path)
                    base_url = _normalize_base_url(creds.domain)
                except CredentialFormatError as exc:
                    base_url = _normalize_base_url(exc.domain)
        base_url = base_url or DEFAULT_BASE_URLS[0]
        return await self._browser_auth_fn(
            state_path,
            login_url=f"{base_url}/profile/usage",
        )

    async def doctor(self, config: ProviderConfig) -> DoctorResult:
        exe_path = self.detect_client_exe(config)
        auth_path = self.find_auth_file(config)
        browser_creds = self._load_browser_credentials(config)

        if not auth_path:
            if browser_creds:
                return DoctorResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    client_installed=exe_path is not None,
                    client_exe_path=exe_path,
                    auth_file_found=True,
                    auth_file_path=browser_creds.source_path,
                    auth_readable=True,
                    account_label=mask_secret(browser_creds.uid) or "浏览器会话",
                    token_expires_at=browser_creds.expires_at,
                    healthy=True,
                    message="已就绪（受控浏览器 Edge storageState 会话）。",
                    details={"auth_mode": "browser_state"},
                )
            searched = [str(p) for p in _candidate_auth_paths(config)[:4]]
            return DoctorResult(
                provider_id=self.id,
                display_name=self.display_name,
                client_installed=exe_path is not None,
                client_exe_path=exe_path,
                auth_file_found=False,
                auth_file_path=None,
                auth_readable=False,
                healthy=False,
                message="未找到 WorkBuddy 本地登录态文件，且尚未配置浏览器授权；请运行 `geteggs auth workbuddy` 完成一次授权。",
                details={"searched_paths_sample": searched},
            )

        try:
            creds = parse_auth_file(auth_path)
        except CredentialFormatError as exc:
            if browser_creds:
                return DoctorResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    client_installed=exe_path is not None,
                    client_exe_path=exe_path,
                    auth_file_found=True,
                    auth_file_path=browser_creds.source_path,
                    auth_readable=True,
                    account_label=exc.account_label or mask_secret(browser_creds.uid) or "浏览器会话",
                    token_expires_at=browser_creds.expires_at or exc.expires_at,
                    healthy=True,
                    message="已就绪（桌面端为 $wbEncrypted 加密，将自动使用受控浏览器 Edge 会话完成签到）。",
                    details={"auth_mode": "browser_state", "desktop_info": str(auth_path)},
                )
            return DoctorResult(
                provider_id=self.id,
                display_name=self.display_name,
                client_installed=exe_path is not None,
                client_exe_path=exe_path,
                auth_file_found=True,
                auth_file_path=str(auth_path),
                auth_readable=False,
                account_label=exc.account_label,
                token_expires_at=exc.expires_at,
                healthy=False,
                message=(
                    f"{exc} 请运行 `geteggs auth workbuddy`（或在 Web 控制台点击“浏览器授权”）完成一次受控浏览器登录授权。"
                ),
                details={"domain": exc.domain} if exc.domain else {},
            )

        if creds.is_expired and not creds.refresh_token:
            return DoctorResult(
                provider_id=self.id,
                display_name=self.display_name,
                client_installed=exe_path is not None,
                client_exe_path=exe_path,
                auth_file_found=True,
                auth_file_path=str(auth_path),
                auth_readable=True,
                account_label=mask_secret(creds.uid) or "已识别账号",
                token_expires_at=creds.expires_at,
                healthy=False,
                message="本地 accessToken 已过期且缺少 refreshToken，请打开 WorkBuddy 客户端重新登录。",
            )

        return DoctorResult(
            provider_id=self.id,
            display_name=self.display_name,
            client_installed=exe_path is not None,
            client_exe_path=exe_path,
            auth_file_found=True,
            auth_file_path=str(auth_path),
            auth_readable=True,
            account_label=mask_secret(creds.uid) or "已识别账号",
            token_expires_at=creds.expires_at,
            healthy=True,
            message="本地登录态有效，已具备自动签到条件。"
            + ("（Token 临近或已过期，将在执行时自动尝试刷新）" if creds.is_expired else ""),
            details={
                "has_refresh_token": bool(creds.refresh_token),
                "base_urls": self._resolve_base_urls(config, creds),
            },
        )

    async def _try_refresh_token(
        self,
        client: httpx.AsyncClient,
        creds: WorkBuddyCredentials,
        base_url: str,
        log: Optional[LogCallback],
    ) -> bool:
        if not creds.refresh_token:
            return False
        if log:
            log("info", "检测到令牌可能过期，尝试使用本地 refreshToken 刷新会话...")
        headers = self._build_headers(creds, base_url)
        headers["X-Auth-Refresh-Source"] = "plugin"
        headers["X-Refresh-Token"] = creds.refresh_token
        try:
            resp = await client.post(
                f"{base_url}/v2/plugin/auth/token/refresh",
                headers=headers,
                json={},
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            new_token = extract_first(data, "accessToken", "access_token", "token")
            if isinstance(new_token, str) and new_token.strip():
                creds.access_token = new_token.strip()
                creds.is_expired = False
                new_refresh = extract_first(data, "refreshToken", "refresh_token")
                if isinstance(new_refresh, str) and new_refresh.strip():
                    creds.refresh_token = new_refresh.strip()
                if log:
                    log("info", "会话令牌已在内存中刷新成功。")
                return True
        except (httpx.HTTPError, ValueError):
            return False
        return False

    async def _post_api(
        self,
        client: httpx.AsyncClient,
        creds: WorkBuddyCredentials,
        base_url: str,
        path: str,
    ) -> httpx.Response:
        return await client.post(
            f"{base_url}{path}",
            headers=self._build_headers(creds, base_url),
            json={},
        )

    async def _query_status(
        self,
        client: httpx.AsyncClient,
        creds: WorkBuddyCredentials,
        base_urls: list[str],
        log: Optional[LogCallback],
    ) -> tuple[str, Optional[dict[str, Any]], int]:
        """Try status endpoints across configured base URLs; return (chosen_base_url, json_payload, status_code)."""
        last_status = 0
        last_url = base_urls[0]
        for base_url in base_urls:
            for endpoint in (
                "/v2/billing/meter/checkin-status",
                "/v2/billing/meter/checkin-activity-status",
            ):
                try:
                    resp = await self._post_api(client, creds, base_url, endpoint)
                except httpx.HTTPError as exc:
                    if log:
                        log("warning", f"请求 {base_url}{endpoint} 网络异常：{exc}")
                    last_url = base_url
                    continue
                last_status = resp.status_code
                last_url = base_url
                if resp.status_code == 404:
                    continue
                if resp.status_code in (401, 403):
                    return base_url, None, resp.status_code
                try:
                    return base_url, resp.json(), resp.status_code
                except ValueError:
                    return base_url, None, resp.status_code
        return last_url, None, last_status

    def _parse_status_payload(self, payload: dict[str, Any]) -> tuple[Optional[bool], CreditSnapshot]:
        checked_in = _to_bool(
            extract_first(
                payload,
                "checkedIn",
                "checked_in",
                "todayCheckedIn",
                "today_checked_in",
                "isCheckedIn",
                "is_checked_in",
                "signed",
            )
        )
        balance = _to_int(
            extract_first(
                payload,
                "balance",
                "totalCredits",
                "total_credits",
                "credits",
                "remainingCredits",
                "remaining_credits",
                "availableCredits",
            )
        )
        streak = _to_int(
            extract_first(
                payload,
                "streakDays",
                "streak_days",
                "continuousDays",
                "continuous_days",
                "consecutiveDays",
            )
        )
        expiring_soon = _to_int(
            extract_first(
                payload,
                "expiringSoon",
                "expiring_soon",
                "expiringCredits",
                "expiring_credits",
                "expiringPoints",
                "expiring_points",
            )
        )
        expiring_at: Optional[str] = None
        raw_expiring_at = extract_first(
            payload,
            "expiringAt",
            "expiring_at",
            "expireAt",
            "expire_at",
            "expiringDate",
            "expiring_date",
        )
        if raw_expiring_at is not None:
            expiring_at, _ = _parse_expiry(raw_expiring_at)
            if expiring_at is None and isinstance(raw_expiring_at, str):
                expiring_at = raw_expiring_at.strip() or None

        return checked_in, CreditSnapshot(
            balance=balance,
            streak_days=streak,
            expiring_soon=expiring_soon,
            expiring_at=expiring_at,
        )

    def _interpret_checkin_payload(
        self,
        body: Any,
        status_code: int,
        pre_snapshot: CreditSnapshot,
        biz_date: str,
        duration_ms: int,
        post_snapshot: Optional[CreditSnapshot] = None,
    ) -> CheckinResult:
        """Normalize raw JSON response from either direct API or controlled browser fetch."""
        if status_code in (401, 403):
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.AUTH_EXPIRED,
                message="签到接口返回未授权（401/403），请重新完成客户端或浏览器授权。",
                biz_date=biz_date,
                duration_ms=duration_ms,
                raw_error_type=f"HTTP_{status_code}",
            )

        if status_code >= 500 or status_code == 0:
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.FAILED,
                message=f"WorkBuddy 服务端暂时异常（HTTP {status_code}）。",
                biz_date=biz_date,
                duration_ms=duration_ms,
                raw_error_type=f"HTTP_{status_code}",
            )

        if not isinstance(body, dict):
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.SCHEMA_CHANGED,
                message=f"签到接口响应结构异常（HTTP {status_code}，非 JSON 对象）。",
                biz_date=biz_date,
                duration_ms=duration_ms,
                raw_error_type="UnexpectedPayloadType",
            )

        code = body.get("code")
        msg_text = str(body.get("message") or body.get("msg") or "")
        low_msg = msg_text.lower()

        if code == 10001 or "already" in low_msg or "已签到" in msg_text or "重复" in msg_text:
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.ALREADY_DONE,
                message="今日已完成签到。",
                biz_date=biz_date,
                snapshot=pre_snapshot,
                duration_ms=duration_ms,
            )

        if code is not None and code != 0:
            if any(word in low_msg or word in msg_text for word in ("captcha", "verify", "风控", "验证")):
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.NEEDS_MANUAL,
                    message=f"触发人工校验或风控提示（{msg_text or f'code={code}'}），请打开 WorkBuddy 手动处理。",
                    biz_date=biz_date,
                    duration_ms=duration_ms,
                    raw_error_type=f"Code_{code}",
                )
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.FAILED,
                message=f"签到返回异常：code={code}, msg={msg_text or '未知错误'}",
                biz_date=biz_date,
                duration_ms=duration_ms,
                raw_error_type=f"Code_{code}",
            )

        delta = _to_int(
            extract_first(
                body,
                "rewardCredits",
                "reward_credits",
                "earnedCredits",
                "delta",
                "points",
                "credits",
                "reward",
            )
        ) or 0
        balance = _to_int(
            extract_first(
                body,
                "balance",
                "totalCredits",
                "total_credits",
                "remainingCredits",
                "availableCredits",
            )
        )
        streak = _to_int(
            extract_first(body, "streakDays", "streak_days", "continuousDays")
        )

        if post_snapshot is not None:
            if balance is None:
                balance = post_snapshot.balance
            if streak is None:
                streak = post_snapshot.streak_days
            if delta == 0 and pre_snapshot.balance is not None and balance is not None:
                delta = max(0, balance - pre_snapshot.balance)

        expiring_soon = (
            post_snapshot.expiring_soon
            if post_snapshot is not None and post_snapshot.expiring_soon is not None
            else pre_snapshot.expiring_soon
        )
        expiring_at = (
            post_snapshot.expiring_at
            if post_snapshot is not None and post_snapshot.expiring_at is not None
            else pre_snapshot.expiring_at
        )

        final_snap = CreditSnapshot(
            balance=balance if balance is not None else pre_snapshot.balance,
            delta=delta,
            streak_days=streak if streak is not None else pre_snapshot.streak_days,
            expiring_soon=expiring_soon,
            expiring_at=expiring_at,
        )
        detail_parts = ["WorkBuddy 今日签到成功"]
        if final_snap.delta > 0:
            detail_parts.append(f"+{final_snap.delta} 积分")
        if final_snap.balance is not None:
            detail_parts.append(f"当前余额 {final_snap.balance}")
        if final_snap.streak_days is not None:
            detail_parts.append(f"已连续 {final_snap.streak_days} 天")

        return CheckinResult(
            provider_id=self.id,
            display_name=self.display_name,
            status=CheckinStatus.SUCCESS,
            message="，".join(detail_parts) + "。",
            biz_date=biz_date,
            snapshot=final_snap,
            duration_ms=duration_ms,
        )

    async def _checkin_via_browser(
        self,
        config: ProviderConfig,
        biz_date: str,
        *,
        uid: Optional[str],
        base_url: str,
        dry_run: bool,
        log: Optional[LogCallback],
        started: float,
    ) -> CheckinResult:
        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        state_path = get_browser_state_path(config)
        try:
            outcome = await self._browser_checkin_fn(
                state_path,
                base_url=base_url,
                uid=uid,
                dry_run=dry_run,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.FAILED,
                message=f"受控浏览器执行异常：{exc}",
                biz_date=biz_date,
                duration_ms=elapsed_ms(),
                dry_run=dry_run,
                raw_error_type=type(exc).__name__,
            )

        if not outcome.get("authenticated"):
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.AUTH_EXPIRED,
                message=str(outcome.get("reason") or "浏览器登录态已失效，请重新运行 `geteggs auth workbuddy`。"),
                biz_date=biz_date,
                duration_ms=elapsed_ms(),
                dry_run=dry_run,
                raw_error_type="BrowserAuthExpired",
            )

        status_res = outcome.get("statusRes") or {}
        status_code = int(status_res.get("status") or 0)
        status_payload = status_res.get("data")

        if status_code in (401, 403):
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.AUTH_EXPIRED,
                message="浏览器上下文调用状态接口未授权（401/403），请重新执行 `geteggs auth workbuddy`。",
                biz_date=biz_date,
                duration_ms=elapsed_ms(),
                dry_run=dry_run,
                raw_error_type=f"HTTP_{status_code}",
            )

        if status_code == 404:
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.SCHEMA_CHANGED,
                message="受控浏览器未找到签到状态接口（HTTP 404），接口可能已改版，未提交签到。",
                biz_date=biz_date,
                duration_ms=elapsed_ms(),
                dry_run=dry_run,
                raw_error_type="HTTP_404",
            )

        if status_code == 0 or status_code >= 500:
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.FAILED,
                message=f"受控浏览器未能获取签到状态（HTTP {status_code}），本次未提交签到。",
                biz_date=biz_date,
                duration_ms=elapsed_ms(),
                dry_run=dry_run,
                raw_error_type=f"HTTP_{status_code}",
            )

        if status_code != 200:
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.FAILED,
                message=f"受控浏览器状态接口返回异常（HTTP {status_code}），本次未提交签到。",
                biz_date=biz_date,
                duration_ms=elapsed_ms(),
                dry_run=dry_run,
                raw_error_type=f"HTTP_{status_code}",
            )

        if not isinstance(status_payload, dict):
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.SCHEMA_CHANGED,
                message="受控浏览器状态接口响应结构异常（非 JSON 对象），可能已改版。",
                biz_date=biz_date,
                duration_ms=elapsed_ms(),
                dry_run=dry_run,
                raw_error_type="UnexpectedPayloadType",
            )

        pre_checked_in: Optional[bool] = None
        pre_snapshot = CreditSnapshot()
        if isinstance(status_payload, dict):
            pre_checked_in, pre_snapshot = self._parse_status_payload(status_payload)

        if pre_checked_in is True:
            if log:
                log("info", "受控浏览器预检显示今日已完成签到，幂等跳过。")
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.ALREADY_DONE,
                message="今日已完成签到（受控浏览器预检幂等跳过）。",
                biz_date=biz_date,
                snapshot=pre_snapshot,
                duration_ms=elapsed_ms(),
                dry_run=dry_run,
            )

        if dry_run:
            if pre_checked_in is None:
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.UNAVAILABLE,
                    message="[Dry-Run] 状态接口响应中未找到可识别的签到状态字段，可能已改版。",
                    biz_date=biz_date,
                    snapshot=pre_snapshot,
                    duration_ms=elapsed_ms(),
                    dry_run=True,
                    raw_error_type="UnknownCheckinState",
                )
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.SUCCESS,
                message="[Dry-Run] 受控浏览器会话与状态预检正常，未实际提交签到。",
                biz_date=biz_date,
                snapshot=pre_snapshot,
                duration_ms=elapsed_ms(),
                dry_run=True,
            )

        checkin_res = outcome.get("checkinRes") or {}
        if pre_checked_in is None and not checkin_res:
            return CheckinResult(
                provider_id=self.id,
                display_name=self.display_name,
                status=CheckinStatus.SCHEMA_CHANGED,
                message="受控浏览器状态接口响应中未找到可识别的签到状态字段，本次未提交签到。",
                biz_date=biz_date,
                snapshot=pre_snapshot,
                duration_ms=elapsed_ms(),
                dry_run=dry_run,
                raw_error_type="UnknownCheckinState",
            )
        post_snap: Optional[CreditSnapshot] = None
        post_status_res = outcome.get("postStatusRes") or {}
        if isinstance(post_status_res.get("data"), dict):
            _, post_snap = self._parse_status_payload(post_status_res["data"])

        return self._interpret_checkin_payload(
            checkin_res.get("data"),
            int(checkin_res.get("status") or 0),
            pre_snapshot,
            biz_date,
            elapsed_ms(),
            post_snapshot=post_snap,
        )

    async def checkin(
        self,
        config: ProviderConfig,
        biz_date: str,
        *,
        dry_run: bool = False,
        log: Optional[LogCallback] = None,
    ) -> CheckinResult:
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        auth_path = self.find_auth_file(config)
        creds: Optional[WorkBuddyCredentials] = None
        info_err: Optional[CredentialFormatError] = None
        using_browser_state = False

        if auth_path:
            try:
                creds = parse_auth_file(auth_path)
            except CredentialFormatError as exc:
                info_err = exc
        elif (os.environ.get("GETEGGS_WORKBUDDY_TOKEN") or "").strip():
            creds = WorkBuddyCredentials(
                access_token=(os.environ.get("GETEGGS_WORKBUDDY_TOKEN") or "").strip(),
                uid=os.environ.get("GETEGGS_WORKBUDDY_UID"),
            )

        if creds is None:
            browser_creds = self._load_browser_credentials(config, info_err)
            if browser_creds is None:
                msg = (
                    f"{info_err} 请运行 `geteggs auth workbuddy`（或在控制台点击“浏览器授权”）完成一次授权。"
                    if info_err
                    else "未找到可用的本地登录态或受控浏览器授权，请运行 `geteggs auth workbuddy` 完成一次授权。"
                )
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.NEEDS_MANUAL if info_err else CheckinStatus.UNAVAILABLE,
                    message=msg,
                    biz_date=biz_date,
                    duration_ms=elapsed_ms(),
                    dry_run=dry_run,
                    raw_error_type="CredentialFormatError" if info_err else "NoAuthState",
                )
            creds = browser_creds
            using_browser_state = True

        base_urls = self._resolve_base_urls(config, creds)

        # Cookie-only browser state skips direct httpx and runs directly in headless Edge context.
        if using_browser_state and not creds.access_token:
            return await self._checkin_via_browser(
                config,
                biz_date,
                uid=creds.uid,
                base_url=base_urls[0],
                dry_run=dry_run,
                log=log,
                started=started,
            )

        if log:
            source_hint = "受控浏览器会话" if using_browser_state else "本地凭证"
            log("info", f"已读取{source_hint}（账号 {mask_secret(creds.uid) or '未知'}），准备检查今日签到状态...")

        async with self.build_http_client(
            transport=self._transport,
            cookies=creds.cookies,
        ) as client:
            if creds.is_expired and creds.refresh_token and not dry_run:
                await self._try_refresh_token(client, creds, base_urls[0], log)

            active_url, status_payload, status_code = await self._query_status(
                client, creds, base_urls, log
            )

            if status_code in (401, 403) and creds.refresh_token and not dry_run:
                if await self._try_refresh_token(client, creds, active_url, log):
                    active_url, status_payload, status_code = await self._query_status(
                        client, creds, [active_url], log
                    )

            if status_code in (401, 403):
                if using_browser_state:
                    if log:
                        log("info", "静态会话令牌已过期，切换无头浏览器自动刷新上下文并签到...")
                    return await self._checkin_via_browser(
                        config,
                        biz_date,
                        uid=creds.uid,
                        base_url=active_url,
                        dry_run=dry_run,
                        log=log,
                        started=started,
                    )
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.AUTH_EXPIRED,
                    message="WorkBuddy 登录态已失效（HTTP 401/403），请打开客户端重新登录后再试。",
                    biz_date=biz_date,
                    duration_ms=elapsed_ms(),
                    dry_run=dry_run,
                    raw_error_type=f"HTTP_{status_code}",
                )

            if status_code == 0 and status_payload is None:
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.FAILED,
                    message="无法连接 WorkBuddy 服务器，请检查本机网络或代理配置。",
                    biz_date=biz_date,
                    duration_ms=elapsed_ms(),
                    dry_run=dry_run,
                    raw_error_type="NetworkError",
                )

            if status_code == 404:
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.SCHEMA_CHANGED,
                    message="WorkBuddy 签到状态接口不存在（HTTP 404），接口可能已改版，本次未提交签到。",
                    biz_date=biz_date,
                    duration_ms=elapsed_ms(),
                    dry_run=dry_run,
                    raw_error_type="HTTP_404",
                )

            if status_code >= 500:
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.FAILED,
                    message=f"WorkBuddy 服务端暂时异常（HTTP {status_code}），本次未提交签到。",
                    biz_date=biz_date,
                    duration_ms=elapsed_ms(),
                    dry_run=dry_run,
                    raw_error_type=f"HTTP_{status_code}",
                )

            if status_code == 200 and not isinstance(status_payload, dict):
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.SCHEMA_CHANGED,
                    message="WorkBuddy 状态接口响应结构异常（非 JSON 对象），可能已改版。",
                    biz_date=biz_date,
                    duration_ms=elapsed_ms(),
                    dry_run=dry_run,
                    raw_error_type="UnexpectedPayloadType",
                )

            if status_code != 200:
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.FAILED,
                    message=f"WorkBuddy 状态接口返回异常（HTTP {status_code}），本次未提交签到。",
                    biz_date=biz_date,
                    duration_ms=elapsed_ms(),
                    dry_run=dry_run,
                    raw_error_type=f"HTTP_{status_code}",
                )

            pre_checked_in: Optional[bool] = None
            pre_snapshot = CreditSnapshot()
            if isinstance(status_payload, dict):
                pre_checked_in, pre_snapshot = self._parse_status_payload(status_payload)

            if pre_checked_in is True:
                if log:
                    log("info", "远端状态显示今日已完成签到，幂等跳过。")
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.ALREADY_DONE,
                    message="今日已完成签到（已通过状态预检，幂等跳过）。",
                    biz_date=biz_date,
                    snapshot=pre_snapshot,
                    duration_ms=elapsed_ms(),
                    dry_run=dry_run,
                )

            if dry_run:
                if pre_checked_in is None:
                    return CheckinResult(
                        provider_id=self.id,
                        display_name=self.display_name,
                        status=CheckinStatus.UNAVAILABLE,
                        message="[Dry-Run] 状态接口响应中未找到可识别的签到状态字段，可能已改版。",
                        biz_date=biz_date,
                        snapshot=pre_snapshot,
                        duration_ms=elapsed_ms(),
                        dry_run=True,
                        raw_error_type="UnknownCheckinState",
                    )
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.SUCCESS,
                    message="[Dry-Run] 本地登录态与状态预检正常，未实际提交签到请求。",
                    biz_date=biz_date,
                    snapshot=pre_snapshot,
                    duration_ms=elapsed_ms(),
                    dry_run=True,
                )

            if log:
                log("info", f"正在向 {active_url} 提交今日签到...")

            try:
                checkin_resp = await self._post_api(
                    client, creds, active_url, "/v2/billing/meter/daily-checkin"
                )
            except httpx.HTTPError as exc:
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.FAILED,
                    message=f"签到请求发生网络异常：{exc}",
                    biz_date=biz_date,
                    snapshot=pre_snapshot,
                    duration_ms=elapsed_ms(),
                    raw_error_type=type(exc).__name__,
                )

            if checkin_resp.status_code in (401, 403) and using_browser_state:
                return await self._checkin_via_browser(
                    config,
                    biz_date,
                    uid=creds.uid,
                    base_url=active_url,
                    dry_run=dry_run,
                    log=log,
                    started=started,
                )

            if checkin_resp.status_code in (401, 403, 500) or checkin_resp.status_code >= 500:
                return self._interpret_checkin_payload(
                    None, checkin_resp.status_code, pre_snapshot, biz_date, elapsed_ms()
                )

            try:
                body = checkin_resp.json()
            except ValueError:
                return CheckinResult(
                    provider_id=self.id,
                    display_name=self.display_name,
                    status=CheckinStatus.SCHEMA_CHANGED,
                    message=f"签到接口返回非 JSON 响应（HTTP {checkin_resp.status_code}），可能已改版。",
                    biz_date=biz_date,
                    duration_ms=elapsed_ms(),
                    raw_error_type="InvalidJSON",
                )

            post_snap: Optional[CreditSnapshot] = None
            if isinstance(body, dict) and (body.get("code") in (None, 0)):
                _, post_payload, post_code = await self._query_status(
                    client, creds, [active_url], None
                )
                if post_code == 200 and isinstance(post_payload, dict):
                    _, post_snap = self._parse_status_payload(post_payload)

            return self._interpret_checkin_payload(
                body,
                checkin_resp.status_code,
                pre_snapshot,
                biz_date,
                elapsed_ms(),
                post_snapshot=post_snap,
            )
