"""Qoder CN daily check-in provider (Personal Access Token route)."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

if TYPE_CHECKING:
    import httpx

from geteggs.core.config import (
    allow_custom_config,
    get_qoder_pat_path,
    is_unc_path,
    validate_provider_base_url,
)
from geteggs.core.models import (
    CheckinResult,
    CheckinStatus,
    DoctorResult,
    ProviderConfig,
)
from geteggs.core.secure_store import load_secure_json
from geteggs.providers.base import BaseProvider, LogCallback, parse_timestamp

QODER_CN_BASE_URL = "https://openapi.qoder.com.cn"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
DEFAULT_EXCHANGE_PATH = "/api/v1/jobToken/exchange"
PAT_ENV_NAMES = ("QODERCN_PERSONAL_ACCESS_TOKEN", "QODER_PERSONAL_ACCESS_TOKEN")
MAX_JOB_TOKEN_TTL_SECONDS = 86_400.0


class QoderProvider(BaseProvider):
    """Qoder CN daily check-in using a PAT.

    The campaign API is not officially documented. Failures are surfaced as
    normal provider results and never contain the PAT.
    """

    id = "qoder"
    display_name = "Qoder"
    description = "Qoder CN 每日自动签到（PAT）"
    supports_browser_auth = False
    fixed_business_day_start = "10:00"

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._transport = transport
        self._token_cache: dict[tuple[str, str], tuple[str, float]] = {}

    def _token_cache_key(
        self, config: ProviderConfig, pat: str
    ) -> tuple[str, str]:
        digest = hashlib.sha256(pat.encode("utf-8")).hexdigest()
        return self._base_url(config), digest

    def _cached_job_token(
        self, config: ProviderConfig, pat: str
    ) -> Optional[str]:
        key = self._token_cache_key(config, pat)
        cached = self._token_cache.get(key)
        if not cached:
            return None
        token, expires_at = cached
        if expires_at <= time.monotonic():
            self._token_cache.pop(key, None)
            return None
        return token

    def _remember_job_token(
        self,
        config: ProviderConfig,
        pat: str,
        token: str,
        expires_in: Any,
    ) -> None:
        try:
            ttl = float(expires_in)
        except (TypeError, ValueError):
            ttl = 3600.0
        if ttl <= 0:
            # 已过期的令牌不值得缓存，下一次请求会重新兑换。
            return
        ttl = min(ttl, MAX_JOB_TOKEN_TTL_SECONDS)
        if ttl <= 60.0:
            return
        ttl -= 60.0
        if len(self._token_cache) >= 4:
            self._token_cache.clear()
        self._token_cache[self._token_cache_key(config, pat)] = (
            token,
            time.monotonic() + ttl,
        )

    def _forget_job_token(self, config: ProviderConfig, pat: str) -> None:
        self._token_cache.pop(self._token_cache_key(config, pat), None)

    def clear_token_cache(self) -> None:
        """Drop all cached exchanged job tokens (e.g., after PAT update/clear)."""
        self._token_cache.clear()

    # ---- BaseProvider contract ----

    def detect_client_exe(self, config: ProviderConfig) -> Optional[str]:
        candidates: list[Path] = []
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Programs" / "Qoder" / "Qoder.exe")
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(env_name)
            if root:
                candidates.append(Path(root) / "Qoder" / "Qoder.exe")
        candidates.extend(
            [
                Path(r"D:\Qoder\Qoder.exe"),
                Path(r"C:\Program Files\Qoder\Qoder.exe"),
            ]
        )
        custom = str(config.extra.get("client_exe_path") or "").strip()
        if custom and "\x00" not in custom and not is_unc_path(custom):
            candidates.insert(0, Path(custom).expanduser())
        for cmd in ("qoder", "qoder.exe"):
            found = shutil.which(cmd)
            if found:
                candidates.append(Path(found))
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    async def doctor(self, config: ProviderConfig) -> DoctorResult:
        token, source = self._resolve_pat(config)
        exe = self.detect_client_exe(config)
        details: dict[str, Any] = {"region": "cn", "auth_source": source or ""}

        def result(healthy: bool, message: str, **extra: Any) -> DoctorResult:
            return DoctorResult(
                provider_id=self.id,
                display_name=self.display_name,
                client_installed=bool(exe),
                client_exe_path=exe,
                auth_file_found=bool(token),
                auth_file_path=(source or "环境变量") if token else None,
                auth_readable=bool(token),
                healthy=healthy,
                message=message,
                details={**details, **extra},
            )

        if not token:
            return result(
                False,
                "未配置 Qoder CN PAT。请在网页 Qoder 设置中直接填写 PAT，"
                "或设置环境变量 QODERCN_PERSONAL_ACCESS_TOKEN。",
            )

        import httpx

        try:
            async with self.build_http_client(self._transport, timeout=10.0) as client:
                response, payload, access_token = await self._fetch_campaigns_with_auth(
                    client, config, token, lambda _level, _message: None
                )
                if not access_token:
                    return result(
                        False,
                        "Qoder CN PAT 在线校验失败或访问令牌兑换失败，"
                        "请确认 PAT 有效且未过期。",
                        verified=False,
                    )
                if response.status_code in (401, 403):
                    self._forget_job_token(config, token)
                    return result(
                        False,
                        f"Qoder CN PAT 在线校验失败（HTTP {response.status_code}），请更新 PAT。",
                        verified=False,
                        http_status=response.status_code,
                    )
                if response.status_code in (404, 410):
                    return result(
                        False,
                        f"Qoder CN 签到接口返回 HTTP {response.status_code}，接口可能已变更。",
                        verified=False,
                        http_status=response.status_code,
                    )
                if not response.is_success:
                    transient = response.status_code == 429 or response.status_code >= 500
                    return result(
                        transient,
                        f"PAT 已配置，但在线校验返回 HTTP {response.status_code}。",
                        verified=False,
                        http_status=response.status_code,
                    )

                campaigns = self._campaigns_from(payload)
                if not self._has_campaigns_collection(payload):
                    return result(
                        False,
                        "PAT 在线校验通过，但活动响应缺少 campaigns 字段，接口可能已变更。",
                        verified=True,
                        schema_ok=False,
                    )
                active = [
                    campaign
                    for campaign in campaigns
                    if str(campaign.get("actionType") or "").upper()
                    == "CLAIM_BENEFIT"
                    and self._active_now(campaign)
                ]
                claimable = any(
                    str(campaign.get("claimStatus") or "").upper() == "CLAIMABLE"
                    for campaign in active
                )
                claimed = any(
                    str(campaign.get("claimStatus") or "").upper() == "CLAIMED"
                    for campaign in active
                )
                if claimable:
                    message = "PAT 在线校验通过，当前有可参与的 Qoder CN 每日签到活动。"
                elif claimed:
                    message = "PAT 在线校验通过，今日 Qoder CN 已完成签到。"
                else:
                    message = "PAT 在线校验通过，当前没有可参与的 Qoder CN 每日签到活动。"
                return result(
                    True,
                    message,
                    verified=True,
                    active_campaigns=len(active),
                    claimable=claimable,
                    claimed=claimed,
                )
        except (ValueError, httpx.InvalidURL) as exc:
            return result(False, f"配置无效：{type(exc).__name__}" if isinstance(exc, httpx.InvalidURL) else str(exc), verified=False)
        except httpx.HTTPError as exc:
            reason = type(exc).__name__
            return result(
                True,
                f"PAT 已配置，但暂时无法在线校验：{reason}",
                verified=False,
            )

    async def checkin(
        self,
        config: ProviderConfig,
        biz_date: str,
        *,
        dry_run: bool = False,
        log: Optional[LogCallback] = None,
    ) -> CheckinResult:
        import httpx

        started = time.perf_counter()
        emit = log or (lambda _level, _message: None)
        pat, source = self._resolve_pat(config)
        if not pat:
            return self._result(
                CheckinStatus.NEEDS_MANUAL,
                "未配置 Qoder CN PAT。请在网页 Qoder 设置中填写 PAT，或设置 QODERCN_PERSONAL_ACCESS_TOKEN。",
                biz_date,
                started=started,
                dry_run=dry_run,
            )

        emit("info", f"正在查询 Qoder CN 每日签到活动（凭证来源：{source or '未知'}）...")
        try:
            async with self.build_http_client(self._transport) as client:
                response, payload, access_token = await self._fetch_campaigns_with_auth(
                    client, config, pat, emit
                )
                if not access_token:
                    return self._result(
                        CheckinStatus.AUTH_EXPIRED,
                        "Qoder CN PAT 兑换访问令牌失败，请确认 PAT 有效且未过期后重试。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderAuthError",
                    )
                if response.status_code in (401, 403):
                    self._forget_job_token(config, pat)
                    return self._result(
                        CheckinStatus.AUTH_EXPIRED,
                        "Qoder CN 登录凭证已失效，请更新 PAT。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderAuthError",
                    )
                if response.status_code in (404, 410):
                    return self._result(
                        CheckinStatus.SCHEMA_CHANGED,
                        f"Qoder CN 签到接口返回 HTTP {response.status_code}，接口可能已变更。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderSchemaError",
                    )
                if response.status_code == 429:
                    return self._result(
                        CheckinStatus.FAILED,
                        "Qoder CN 请求过于频繁，请降低执行频率。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderRateLimit",
                    )
                if not response.is_success:
                    return self._result(
                        CheckinStatus.FAILED,
                        f"Qoder CN 活动查询失败（HTTP {response.status_code}）。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderHttpError",
                    )

                campaigns = self._campaigns_from(payload)
                if not self._has_campaigns_collection(payload):
                    emit("warning", "Qoder CN 活动响应缺少 campaigns 字段，接口可能已变更。")
                    return self._result(
                        CheckinStatus.SCHEMA_CHANGED,
                        "Qoder CN 活动响应结构异常（缺少 campaigns 字段），接口可能已变更。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderSchemaError",
                    )
                active = [
                    campaign
                    for campaign in campaigns
                    if str(campaign.get("actionType") or "").upper()
                    == "CLAIM_BENEFIT"
                    and self._active_now(campaign)
                ]
                claimable = next(
                    (
                        campaign
                        for campaign in active
                        if str(campaign.get("claimStatus") or "").upper()
                        == "CLAIMABLE"
                    ),
                    None,
                )
                claimed = next(
                    (
                        campaign
                        for campaign in active
                        if str(campaign.get("claimStatus") or "").upper()
                        == "CLAIMED"
                    ),
                    None,
                )

                if claimable is None:
                    if claimed is not None:
                        emit("info", "Qoder CN 今日已完成签到。")
                        return self._result(
                            CheckinStatus.ALREADY_DONE,
                            "今日 Qoder CN 已完成签到。",
                            biz_date,
                            started=started,
                            dry_run=dry_run,
                        )
                    return self._result(
                        CheckinStatus.UNAVAILABLE,
                        "当前没有可参与的 Qoder CN 每日签到活动。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                    )

                campaign_id = str(claimable.get("campaignId") or "").strip()
                if not campaign_id:
                    return self._result(
                        CheckinStatus.SCHEMA_CHANGED,
                        "Qoder CN 活动缺少 campaignId，接口可能已变更。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderSchemaError",
                    )

                if dry_run:
                    emit("info", "Dry-Run：检测到今日可签到活动，未提交签到。")
                    return self._result(
                        CheckinStatus.SUCCESS,
                        "Dry-Run：今日可签到（未提交）。",
                        biz_date,
                        started=started,
                        dry_run=True,
                    )

                claim_path = f"{CAMPAIGNS_PATH}/{quote(campaign_id, safe='')}/claim"
                claim_response, claim_payload = await self._request(
                    client,
                    config,
                    "POST",
                    claim_path,
                    access_token,
                    body={},
                )
                if claim_response.status_code in (401, 403):
                    self._forget_job_token(config, pat)
                    return self._result(
                        CheckinStatus.AUTH_EXPIRED,
                        "Qoder CN 签到凭证已失效，请更新 PAT。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderAuthError",
                    )
                if claim_response.status_code in (404, 410):
                    return self._result(
                        CheckinStatus.SCHEMA_CHANGED,
                        f"Qoder CN 签到接口返回 HTTP {claim_response.status_code}，接口可能已变更。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderSchemaError",
                    )
                if claim_response.status_code == 429:
                    return self._result(
                        CheckinStatus.FAILED,
                        "Qoder CN 签到请求频率受限（HTTP 429），请稍后重试。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderRateLimit",
                    )
                if not claim_response.is_success:
                    return self._result(
                        CheckinStatus.FAILED,
                        f"Qoder CN 签到失败（HTTP {claim_response.status_code}）。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderHttpError",
                    )

                claim_status = self._claim_status(claim_payload)
                if claim_status != "CLAIMED":
                    return self._result(
                        CheckinStatus.FAILED,
                        "Qoder CN 未确认签到成功。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderClaimUnconfirmed",
                    )

                emit("info", "已完成 Qoder CN 今日签到。")
                return self._result(
                    CheckinStatus.SUCCESS,
                    "已完成 Qoder CN 今日签到。",
                    biz_date,
                    started=started,
                    dry_run=dry_run,
                )
        except ValueError as exc:
            return self._result(
                CheckinStatus.UNAVAILABLE,
                str(exc),
                biz_date,
                started=started,
                dry_run=dry_run,
                raw_error_type="InvalidConfig",
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            return self._result(
                CheckinStatus.FAILED,
                f"Qoder CN 网络请求失败：{type(exc).__name__}",
                biz_date,
                started=started,
                dry_run=dry_run,
                raw_error_type=type(exc).__name__,
            )

    # ---- Internals ----

    def pat_status(self, config: ProviderConfig) -> dict[str, Any]:
        """Return PAT availability and source without exposing the token."""
        token, source = self._resolve_pat(config)
        return {
            "configured": bool(token),
            "source": source if token else None,
            "format_ok": bool(token and token.startswith(("pt-", "jt-"))),
        }

    def _resolve_pat(self, config: ProviderConfig) -> tuple[Optional[str], Optional[str]]:
        stored = self._load_stored_pat()
        if stored:
            return stored, "网页输入（本机加密存储）"

        env_names: list[str] = []
        custom_env = str(config.extra.get("pat_env") or "").strip()
        if custom_env:
            env_names.append(custom_env)
        env_names.extend(name for name in PAT_ENV_NAMES if name not in env_names)
        for name in env_names:
            value = (os.environ.get(name) or "").strip()
            if value:
                return value, f"环境变量 {name}"

        path_value = (
            str(config.extra.get("auth_file_path") or "").strip()
            or str(config.extra.get("pat_file") or "").strip()
            or (os.environ.get("GETEGGS_QODER_PAT_FILE") or "").strip()
        )
        if not path_value:
            return None, None
        path = Path(path_value).expanduser()
        if not path.is_file():
            return None, str(path)
        try:
            if path.stat().st_size > 64_000:
                return None, str(path)
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None, str(path)
        allowed_keys = set(env_names)
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                normalized_key = key.strip()
                if normalized_key.lower().startswith("export "):
                    normalized_key = normalized_key[7:].strip()
                if normalized_key in allowed_keys:
                    line = value.strip().strip('"').strip("'")
                elif normalized_key.isidentifier():
                    continue
                else:
                    line = line.strip('"').strip("'")
            else:
                line = line.strip('"').strip("'")
            if line:
                return line, str(path)
        return None, str(path)

    @staticmethod
    def _load_stored_pat() -> Optional[str]:
        path = get_qoder_pat_path()
        try:
            if not path.is_file() or path.stat().st_size > 64_000:
                return None
            data = load_secure_json(path)
        except (OSError, ValueError):
            return None
        token = str(data.get("pat") or "").strip()
        return token or None

    @staticmethod
    def _base_url(config: ProviderConfig) -> str:
        raw = str(config.extra.get("base_url") or "").strip()
        if not raw:
            return QODER_CN_BASE_URL
        return validate_provider_base_url(QoderProvider.id, raw)

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cosy-ClientType": "10",
            "User-Agent": "Qoder",
        }

    async def _request(
        self,
        client: httpx.AsyncClient,
        config: ProviderConfig,
        method: str,
        path: str,
        token: str,
        *,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, str]] = None,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        response = await client.request(
            method,
            f"{self._base_url(config)}{path}",
            headers=self._headers(token),
            params=params,
            json=body,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"data": payload}
        return response, payload

    async def _fetch_campaigns(
        self,
        client: httpx.AsyncClient,
        config: ProviderConfig,
        token: str,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        return await self._request(
            client,
            config,
            "GET",
            CAMPAIGNS_PATH,
            token,
            params={"clientType": "10"},
        )

    async def _fetch_campaigns_with_auth(
        self,
        client: httpx.AsyncClient,
        config: ProviderConfig,
        pat: str,
        emit: LogCallback,
    ) -> tuple[httpx.Response, dict[str, Any], str]:
        """Query campaigns with a cached job token, the PAT, or a fresh exchange."""
        cached = self._cached_job_token(config, pat)
        if cached:
            response, payload = await self._fetch_campaigns(client, config, cached)
            if response.is_success or response.status_code in (400, 404, 410, 429):
                return response, payload, cached
            self._forget_job_token(config, pat)

        response, payload = await self._fetch_campaigns(client, config, pat)
        if response.status_code not in (401, 403):
            return response, payload, pat

        emit("info", "Qoder CN PAT 直连未通过，正在兑换访问令牌...")
        exchanged, expires_in = await self._exchange_pat(client, config, pat, emit)
        if not exchanged:
            return response, payload, ""
        self._remember_job_token(config, pat, exchanged, expires_in)
        response, payload = await self._fetch_campaigns(client, config, exchanged)
        return response, payload, exchanged

    async def _exchange_pat(
        self,
        client: httpx.AsyncClient,
        config: ProviderConfig,
        pat: str,
        emit: LogCallback,
    ) -> tuple[Optional[str], Optional[float]]:
        path = str(config.extra.get("pat_exchange_path") or DEFAULT_EXCHANGE_PATH).strip()
        if not allow_custom_config():
            if not path.startswith("/") or "://" in path or ".." in path or "\\" in path:
                raise ValueError("Qoder PAT 兑换路径必须是以 / 开头的相对路径。")
        response, payload = await self._request(
            client,
            config,
            "POST",
            path,
            pat,
            body={"personal_token": pat},
        )
        if not response.is_success:
            emit("warning", f"Qoder CN PAT 兑换访问令牌失败（HTTP {response.status_code}）。")
            return None, None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        token = (
            payload.get("token")
            or payload.get("device_token")
            or payload.get("access_token")
            or data.get("token")
        )
        if isinstance(token, str) and token.strip():
            ttl = self._job_token_ttl_seconds(
                payload.get("expires_at") or data.get("expires_at"),
                payload.get("expires_in") or data.get("expires_in"),
            )
            emit("info", "Qoder CN 访问令牌兑换成功。")
            return token.strip(), ttl
        emit("warning", "Qoder CN 访问令牌兑换响应缺少 token。")
        return None, None

    @classmethod
    def _job_token_ttl_seconds(
        cls, expires_at: Any, expires_in: Any
    ) -> Optional[float]:
        """Normalize the exchange response expiry to seconds."""
        expiry = cls._parse_time(expires_at)
        if expiry is not None:
            return (expiry - datetime.now(timezone.utc)).total_seconds()
        try:
            ttl = float(expires_in)
        except (TypeError, ValueError):
            return None
        if ttl > 10_000_000:
            # Qoder returns milliseconds (e.g. 86400000 for 24 hours).
            ttl /= 1000.0
        return ttl

    @staticmethod
    def _campaigns_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        if isinstance(data, dict):
            candidates = payload.get("campaigns") or data.get("campaigns")
        elif isinstance(data, list):
            candidates = data
        else:
            candidates = payload.get("campaigns")
        if not isinstance(candidates, list):
            return []
        return [item for item in candidates if isinstance(item, dict)]

    @staticmethod
    def _has_campaigns_collection(payload: dict[str, Any]) -> bool:
        """Return True when the response explicitly carries a campaigns list."""
        data = payload.get("data")
        if isinstance(data, dict):
            return isinstance(payload.get("campaigns"), list) or isinstance(
                data.get("campaigns"), list
            )
        if isinstance(data, list):
            return True
        return isinstance(payload.get("campaigns"), list)

    @classmethod
    def _active_now(cls, campaign: dict[str, Any]) -> bool:
        now = datetime.now(timezone.utc)
        start = cls._parse_time(campaign.get("startAt"))
        end = cls._parse_time(campaign.get("endAt"))
        return (start is None or now >= start) and (end is None or now <= end)

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        return parse_timestamp(value)

    @staticmethod
    def _claim_status(payload: dict[str, Any]) -> str:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        statuses = (
            payload.get("status"),
            data.get("status"),
            payload.get("claimStatus"),
            data.get("claimStatus"),
        )
        for value in statuses:
            text = str(value or "").upper()
            if text == "CLAIMED":
                return "CLAIMED"
        return next((str(value).upper() for value in statuses if value), "")

    def _result(
        self,
        status: CheckinStatus,
        message: str,
        biz_date: str,
        *,
        started: float,
        dry_run: bool,
        raw_error_type: Optional[str] = None,
    ) -> CheckinResult:
        return CheckinResult(
            provider_id=self.id,
            display_name=self.display_name,
            status=status,
            message=message,
            biz_date=biz_date,
            duration_ms=int((time.perf_counter() - started) * 1000),
            dry_run=dry_run,
            raw_error_type=raw_error_type,
        )
