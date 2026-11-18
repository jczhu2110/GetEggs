"""Qoder CN daily Credits provider (Personal Access Token route)."""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx

from geteggs.core.config import get_qoder_pat_path
from geteggs.core.models import (
    CheckinResult,
    CheckinStatus,
    CreditSnapshot,
    DoctorResult,
    ProviderConfig,
)
from geteggs.core.secure_store import load_secure_json
from geteggs.providers.base import BaseProvider, LogCallback

QODER_CN_BASE_URL = "https://openapi.qoder.com.cn"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
DEFAULT_EXCHANGE_PATH = "/api/v1/jobToken/exchange"
PAT_ENV_NAMES = ("QODERCN_PERSONAL_ACCESS_TOKEN", "QODER_PERSONAL_ACCESS_TOKEN")


class QoderProvider(BaseProvider):
    """Qoder CN daily 100 Credits claim using a PAT.

    The campaign API is not officially documented. Failures are surfaced as
    normal provider results and never contain the PAT.
    """

    id = "qoder"
    display_name = "Qoder"
    description = "Qoder CN 每日 100 Credits 自动领取（PAT）"
    supports_browser_auth = False

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._transport = transport

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
        if custom:
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
        if not token:
            return DoctorResult(
                provider_id=self.id,
                display_name=self.display_name,
                client_installed=bool(exe),
                client_exe_path=exe,
                auth_file_found=False,
                auth_file_path=None,
                auth_readable=False,
                healthy=False,
                message=(
                    "未配置 Qoder CN PAT。请在网页 Qoder 设置中直接填写 PAT，"
                    "或设置环境变量 QODERCN_PERSONAL_ACCESS_TOKEN。"
                ),
                details={"region": "cn"},
            )
        message = "Qoder CN PAT 已配置，可执行每日 Credits 领取。"
        if not token.startswith(("pt-", "jt-")):
            message = "已检测到凭证，但格式不是 pt- / jt- 开头，请确认是否为 Qoder CN PAT。"
        return DoctorResult(
            provider_id=self.id,
            display_name=self.display_name,
            client_installed=bool(exe),
            client_exe_path=exe,
            auth_file_found=True,
            auth_file_path=source or "环境变量",
            auth_readable=True,
            account_label=None,
            healthy=True,
            message=message,
            details={"region": "cn", "auth_source": source or ""},
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

        emit("info", f"正在查询 Qoder CN 每日 Credits 活动（凭证来源：{source or '未知'}）...")
        try:
            async with self.build_http_client(self._transport) as client:
                response, payload = await self._fetch_campaigns(client, config, pat)
                access_token = pat

                if response.status_code in (401, 403):
                    emit("info", "Qoder CN PAT 直连未通过，正在兑换访问令牌...")
                    exchanged = await self._exchange_pat(
                        client, config, pat, emit
                    )
                    if not exchanged:
                        return self._result(
                            CheckinStatus.AUTH_EXPIRED,
                            "Qoder CN PAT 兑换访问令牌失败，请确认 PAT 有效且未过期后重试。",
                            biz_date,
                            started=started,
                            dry_run=dry_run,
                            raw_error_type="QoderAuthError",
                        )
                    access_token = exchanged
                    response, payload = await self._fetch_campaigns(
                        client, config, access_token
                    )

                if response.status_code in (401, 403):
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
                        emit("info", "Qoder CN 今日 Credits 已领取。")
                        return self._result(
                            CheckinStatus.ALREADY_DONE,
                            "今日 Qoder Credits 已领取。",
                            biz_date,
                            started=started,
                            dry_run=dry_run,
                            snapshot=CreditSnapshot(delta=0),
                        )
                    return self._result(
                        CheckinStatus.UNAVAILABLE,
                        "当前没有可领取的 Qoder CN 每日 Credits 活动。",
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

                amount = self._benefit_amount(claimable)
                if dry_run:
                    emit("info", f"Dry-Run：检测到可领取 {amount} Credits，未提交领取。")
                    return self._result(
                        CheckinStatus.SUCCESS,
                        f"Dry-Run：今日可领取 {amount} Qoder Credits（未提交）。",
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
                    return self._result(
                        CheckinStatus.AUTH_EXPIRED,
                        "Qoder CN 领取凭证已失效，请更新 PAT。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderAuthError",
                    )
                if claim_response.status_code in (404, 410):
                    return self._result(
                        CheckinStatus.SCHEMA_CHANGED,
                        f"Qoder CN 领取接口返回 HTTP {claim_response.status_code}，接口可能已变更。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderSchemaError",
                    )
                if not claim_response.is_success:
                    return self._result(
                        CheckinStatus.FAILED,
                        f"Qoder CN Credits 领取失败（HTTP {claim_response.status_code}）。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderHttpError",
                    )

                claim_status = self._claim_status(claim_payload)
                if claim_status != "CLAIMED":
                    return self._result(
                        CheckinStatus.FAILED,
                        "Qoder CN 未确认 Credits 领取成功。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="QoderClaimUnconfirmed",
                    )

                emit("info", f"已领取 Qoder CN 每日 {amount} Credits。")
                expiring_at = (
                    datetime.now(timezone.utc) + timedelta(days=30)
                ).isoformat()
                return self._result(
                    CheckinStatus.SUCCESS,
                    f"已领取 Qoder CN 每日 {amount} Credits。",
                    biz_date,
                    started=started,
                    dry_run=dry_run,
                    snapshot=CreditSnapshot(
                        balance=None,
                        delta=amount,
                        expiring_soon=amount,
                        expiring_at=expiring_at,
                    ),
                )
        except httpx.HTTPError as exc:
            return self._result(
                CheckinStatus.FAILED,
                f"Qoder CN 网络请求失败：{exc}",
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
        for raw_line in text.splitlines():
            line = raw_line.strip().strip('"').strip("'")
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                if key.strip() in PAT_ENV_NAMES:
                    line = value.strip().strip('"').strip("'")
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
        return str(config.extra.get("base_url") or QODER_CN_BASE_URL).rstrip("/")

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

    async def _exchange_pat(
        self,
        client: httpx.AsyncClient,
        config: ProviderConfig,
        pat: str,
        emit: LogCallback,
    ) -> Optional[str]:
        path = str(config.extra.get("pat_exchange_path") or DEFAULT_EXCHANGE_PATH)
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
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        token = (
            payload.get("token")
            or payload.get("device_token")
            or payload.get("access_token")
            or data.get("token")
        )
        if isinstance(token, str) and token.strip():
            emit("info", "Qoder CN 访问令牌兑换成功。")
            return token.strip()
        emit("warning", "Qoder CN 访问令牌兑换响应缺少 token。")
        return None

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

    @classmethod
    def _active_now(cls, campaign: dict[str, Any]) -> bool:
        now = datetime.now(timezone.utc)
        start = cls._parse_time(campaign.get("startAt"))
        end = cls._parse_time(campaign.get("endAt"))
        return (start is None or now >= start) and (end is None or now <= end)

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        if isinstance(value, (int, float)):
            return QoderProvider._epoch_to_datetime(float(value))
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.isdigit():
            return QoderProvider._epoch_to_datetime(float(text))
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment

    @staticmethod
    def _epoch_to_datetime(value: float) -> datetime:
        seconds = value / 1000.0 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)

    @staticmethod
    def _benefit_amount(campaign: dict[str, Any]) -> int:
        benefit = campaign.get("benefit")
        if isinstance(benefit, dict):
            amount = benefit.get("amount")
            if isinstance(amount, int) and amount > 0:
                return amount
        return 100

    @staticmethod
    def _claim_status(payload: dict[str, Any]) -> str:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status = payload.get("status") or data.get("status")
        return str(status or "").upper()

    def _result(
        self,
        status: CheckinStatus,
        message: str,
        biz_date: str,
        *,
        started: float,
        dry_run: bool,
        snapshot: Optional[CreditSnapshot] = None,
        raw_error_type: Optional[str] = None,
    ) -> CheckinResult:
        return CheckinResult(
            provider_id=self.id,
            display_name=self.display_name,
            status=status,
            message=message,
            biz_date=biz_date,
            snapshot=snapshot,
            duration_ms=int((time.perf_counter() - started) * 1000),
            dry_run=dry_run,
            raw_error_type=raw_error_type,
        )
