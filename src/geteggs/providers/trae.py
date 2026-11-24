"""Trae CN daily check-in provider (local desktop login state)."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from geteggs.core.config import is_unc_path, validate_provider_base_url
from geteggs.core.models import (
    DEFAULT_CHECKIN_CREDITS,
    MAX_CHECKIN_CREDITS,
    CheckinResult,
    CheckinStatus,
    CreditSnapshot,
    DoctorResult,
    ProviderConfig,
)
from geteggs.providers.base import BaseProvider, LogCallback, mask_secret
from geteggs.providers.trae_auth import (
    DEFAULT_BASE_URL,
    TraeCredentialError,
    TraeCredentials,
    find_storage_json,
    jwt_expiry,
    load_trae_credentials,
    save_token_cache,
    sign_ecdsa_p256,
    token_expires_at,
    token_is_expired,
)

STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
REFRESH_PATH = "/trae/api/v3/oauth/ExchangeToken"
CLIENT_ID_IDE = "ono9krqynydwx5"
CLIENT_ID_LEGACY = "en1oxy7wnw8j9n"
DEFAULT_IDE_VERSION = "1.107.1"
DEFAULT_CREDIT_VALIDITY_DAYS = 31
AUTH_CODES = (1001, 1002, 401, 403)


class TraeRefreshError(RuntimeError):
    """Raised when the local refresh token cannot be exchanged."""


def _to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no"}:
            return False
    return None


def _normalize_base_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().rstrip("/")
    if not text.startswith(("http://", "https://")):
        text = f"https://{text}"
    return text or None


def _root_first(payload: dict[str, Any], *keys: str) -> Any:
    """Trae responses put status/code at the JSON root; nested data is fallback."""
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    data = payload.get("data")
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] is not None:
                return data[key]
    return None


def _parse_business_code(payload: dict[str, Any]) -> Optional[int]:
    return _to_int(_root_first(payload, "code", "statusCode"))


def _parse_message(payload: dict[str, Any]) -> str:
    return str(_root_first(payload, "message", "msg") or "").strip()


def _payload_amount(payload: dict[str, Any]) -> Optional[int]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for source in (data, payload):
        for key in ("points", "credits", "credit", "extra_credits", "extraCredits"):
            amount = _to_int(source.get(key))
            if amount is not None and amount > 0:
                return min(amount, MAX_CHECKIN_CREDITS)
    return None


class _StatusInfo:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.enable = _to_bool(_root_first(payload, "enable", "enabled"))
        self.checked_in = _to_bool(
            _root_first(payload, "checked_in", "checkedIn")
        )
        did_checked_in = _to_bool(
            _root_first(payload, "did_checked_in", "didCheckedIn")
        )
        if did_checked_in is not None:
            if self.checked_in is None:
                self.checked_in = did_checked_in
            elif did_checked_in:
                self.checked_in = True
        self.credits = _to_int(_root_first(payload, "credits", "credit"))
        self.extra_credits = _to_int(
            _root_first(payload, "extra_credits", "extraCredits")
        )

    @property
    def schema_ok(self) -> bool:
        return self.enable is not None and self.checked_in is not None

    @property
    def reward_hint(self) -> Optional[int]:
        if self.credits is not None and self.credits > 0:
            return min(self.credits, MAX_CHECKIN_CREDITS)
        if self.extra_credits is not None and self.extra_credits > 0:
            return min(self.extra_credits, MAX_CHECKIN_CREDITS)
        return None


_TRAE_DISPLAY_RE = re.compile(r"(?<![A-Za-z])trae(?![A-Za-z])", re.IGNORECASE)
_REGISTRY_CACHE: dict[str, Any] = {"locations": None, "expires_at": 0.0}
_REGISTRY_CACHE_TTL_SECONDS = 60.0


def _registry_install_locations() -> list[Path]:
    if os.name != "nt":
        return []
    now = time.monotonic()
    cached = _REGISTRY_CACHE.get("locations")
    if cached is not None and now < float(_REGISTRY_CACHE.get("expires_at") or 0.0):
        return list(cached)
    try:
        import winreg
    except ImportError:
        return []
    locations: list[Path] = []
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    subkeys = (
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
        r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    for root in roots:
        for subkey in subkeys:
            try:
                base = winreg.OpenKey(root, subkey)
            except OSError:
                continue
            try:
                count = winreg.QueryInfoKey(base)[0]
                for index in range(count):
                    try:
                        name = winreg.EnumKey(base, index)
                        with winreg.OpenKey(base, name) as entry:
                            try:
                                display = str(
                                    winreg.QueryValueEx(entry, "DisplayName")[0]
                                )
                            except OSError:
                                continue
                            if not _TRAE_DISPLAY_RE.search(display):
                                continue
                            try:
                                location = str(
                                    winreg.QueryValueEx(entry, "InstallLocation")[0]
                                )
                            except OSError:
                                continue
                            if location.strip():
                                locations.append(Path(location.strip()))
                    except OSError:
                        continue
            finally:
                try:
                    winreg.CloseKey(base)
                except OSError:
                    pass
    _REGISTRY_CACHE["locations"] = list(locations)
    _REGISTRY_CACHE["expires_at"] = time.monotonic() + _REGISTRY_CACHE_TTL_SECONDS
    return locations


class TraeProvider(BaseProvider):
    """Trae CN daily credits check-in using the installed desktop login state."""

    id = "trae"
    display_name = "Trae 国内版"
    description = "字节跳动 Trae CN 每日签到领取积分（读取本地客户端登录态）"
    supports_browser_auth = False
    supports_streak = False
    fixed_business_day_start = "00:00"

    def __init__(
        self,
        transport: Optional[Any] = None,
        *,
        claim_verify_attempts: int = 3,
        claim_verify_delay_seconds: float = 0.5,
    ) -> None:
        self._transport = transport
        try:
            self._claim_verify_attempts = max(1, min(5, int(claim_verify_attempts)))
        except (TypeError, ValueError):
            self._claim_verify_attempts = 3
        try:
            self._claim_verify_delay_seconds = max(
                0.0, float(claim_verify_delay_seconds)
            )
        except (TypeError, ValueError):
            self._claim_verify_delay_seconds = 0.5

    # ---- BaseProvider contract ----

    def detect_client_exe(self, config: ProviderConfig) -> Optional[str]:
        candidates: list[Path] = []
        custom = str(config.extra.get("client_exe_path") or "").strip()
        if custom and "\x00" not in custom and not is_unc_path(custom):
            candidates.append(Path(custom).expanduser())

        local_app_data = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("ProgramFiles")
        if local_app_data:
            candidates.extend(
                [
                    Path(local_app_data) / "Programs" / "Trae CN" / "Trae CN.exe",
                    Path(local_app_data) / "Programs" / "Trae" / "Trae.exe",
                ]
            )
        if program_files:
            candidates.extend(
                [
                    Path(program_files) / "Trae CN" / "Trae CN.exe",
                    Path(program_files) / "Trae" / "Trae.exe",
                ]
            )
        candidates.extend(
            [
                Path(r"D:\Trae\Trae CN\Trae CN.exe"),
                Path(r"D:\Trae\Trae.exe"),
                Path(r"C:\Program Files\Trae CN\Trae CN.exe"),
            ]
        )

        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.expanduser().resolve(strict=False)
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.is_file():
                return str(resolved)

        # Prefer real .exe installations from Windows Uninstall registry over PATH .cmd shims.
        for location in _registry_install_locations():
            for exe_name in ("Trae CN.exe", "Trae.exe"):
                resolved = (location / exe_name).expanduser().resolve(strict=False)
                if resolved in seen:
                    continue
                seen.add(resolved)
                if resolved.is_file():
                    return str(resolved)

        for command in ("trae-cn", "trae"):
            found = shutil.which(command)
            if found:
                resolved = Path(found).expanduser().resolve(strict=False)
                if resolved not in seen and resolved.is_file():
                    return str(resolved)
        return None

    def _client_version(
        self, config: ProviderConfig, *, exe: Optional[str] = None
    ) -> str:
        configured = str(config.extra.get("app_version") or "").strip()
        if configured:
            return configured
        if exe is None:
            try:
                exe = self.detect_client_exe(config)
            except Exception:  # noqa: BLE001
                exe = None
        if exe:
            product = Path(exe).parent / "resources" / "app" / "product.json"
            try:
                data = json.loads(product.read_text(encoding="utf-8"))
                version = str(data.get("version") or "").strip()
                if version:
                    return version
            except (OSError, ValueError):
                pass
        return DEFAULT_IDE_VERSION

    def _base_url(self, config: ProviderConfig, creds: TraeCredentials) -> str:
        for raw in (config.extra.get("base_url"), creds.host):
            normalized = _normalize_base_url(raw)
            if not normalized:
                continue
            try:
                validated = validate_provider_base_url(self.id, normalized)
            except ValueError:
                continue
            if validated:
                return validated
        return DEFAULT_BASE_URL

    @staticmethod
    def _allow_token_refresh(config: ProviderConfig) -> bool:
        return bool(config.extra.get("allow_token_refresh", True))

    @staticmethod
    def _req_source(config: ProviderConfig, creds: TraeCredentials) -> int:
        configured = _to_int(config.extra.get("req_source"))
        if configured in (1, 2):
            return configured
        return creds.req_source if creds.req_source in (1, 2) else 1

    def _http_client(self, config: ProviderConfig) -> Any:
        import httpx

        options = {
            "timeout": httpx.Timeout(20.0),
            "follow_redirects": False,
            "transport": self._transport,
        }
        try:
            return httpx.AsyncClient(
                trust_env=bool(config.extra.get("use_system_proxy", True)),
                **options,
            )
        except Exception:  # noqa: BLE001
            # A malformed proxy environment should not take the whole tool down.
            return httpx.AsyncClient(trust_env=False, **options)

    def _headers(
        self, config: ProviderConfig, creds: TraeCredentials, version: str
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Cloud-IDE-JWT {creds.access_token or ''}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-User-Region": "CN",
            "User-Agent": f"Trae/{version}",
            "x-device-type": platform.system() or "Windows",
            "x-os-version": platform.version() or "10.0",
            "x-app-version": version,
        }
        if creds.device_id:
            headers["x-device-id"] = creds.device_id
        return headers

    async def _post(
        self,
        client: Any,
        config: ProviderConfig,
        creds: TraeCredentials,
        version: str,
        path: str,
        body: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        url = f"{self._base_url(config, creds)}{path}"
        response = await client.post(
            url, headers=self._headers(config, creds, version), json=body
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"data": payload}
        return response, payload

    async def _status(
        self,
        client: Any,
        config: ProviderConfig,
        creds: TraeCredentials,
        version: str,
    ) -> tuple[Any, dict[str, Any]]:
        return await self._post(
            client,
            config,
            creds,
            version,
            STATUS_PATH,
            {"req_source": self._req_source(config, creds)},
        )

    async def _claim(
        self,
        client: Any,
        config: ProviderConfig,
        creds: TraeCredentials,
        version: str,
    ) -> tuple[Any, dict[str, Any]]:
        return await self._post(
            client,
            config,
            creds,
            version,
            CLAIM_PATH,
            {"req_source": self._req_source(config, creds)},
        )

    # ---- Token refresh ----

    def _device_info(self, creds: TraeCredentials, version: str, platform_code: str) -> dict[str, Any]:
        return {
            "DeviceID": creds.device_id or "",
            "MachineID": creds.machine_id or "",
            "PlatformCode": platform_code,
            "DeviceType": "PC",
            "DeviceName": platform.node() or "PC",
            "DeviceModel": "",
            "ClientVersion": version,
            "DevicePublicKey": creds.device_public_key_pem or "",
            "DeviceBrand": "",
            "DeviceCPU": "",
            "OSInfo": platform.system() or "Windows",
            "OSVersion": platform.release() or "",
        }

    def _device_proof(
        self,
        creds: TraeCredentials,
        client_id: str,
        version: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        timestamp = int(time.time())
        nonce = os.urandom(16).hex()
        canonical = "\n".join(
            ["POST", REFRESH_PATH, client_id, creds.refresh_token or "", str(timestamp), nonce]
        )
        signature = sign_ecdsa_p256(
            creds.device_private_key_pem or "", canonical.encode("utf-8")
        )
        return (
            {"Timestamp": timestamp, "Nonce": nonce, "Signature": signature},
            self._device_info(creds, version, "IDE_PC"),
        )

    @staticmethod
    def _refresh_result(payload: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        result = payload.get("Result") if isinstance(payload.get("Result"), dict) else None
        if result is None and isinstance(payload.get("result"), dict):
            result = payload["result"]
        if result is None and isinstance(payload.get("data"), dict):
            result = payload["data"]
        source = result if isinstance(result, dict) else payload
        token = (
            source.get("Token")
            or source.get("token")
            or source.get("accessToken")
            or source.get("access_token")
        )
        refresh = (
            source.get("RefreshToken")
            or source.get("refreshToken")
            or source.get("refresh_token")
        )
        access_exp = (
            source.get("TokenExpireAt")
            or source.get("ExpireAt")
            or source.get("expiresAt")
            or source.get("expires_at")
        )
        refresh_exp = (
            source.get("RefreshTokenExpireAt")
            or source.get("RefreshExpireAt")
            or source.get("refreshExpiresAt")
            or source.get("refresh_expires_at")
        )
        return (
            token.strip() if isinstance(token, str) and token.strip() else None,
            refresh.strip() if isinstance(refresh, str) and refresh.strip() else None,
            str(access_exp).strip() if access_exp is not None else None,
            str(refresh_exp).strip() if refresh_exp is not None else None,
        )

    async def _refresh_token(
        self,
        client: Any,
        config: ProviderConfig,
        creds: TraeCredentials,
        version: str,
        log: Optional[LogCallback],
    ) -> TraeCredentials:
        if not creds.refresh_token:
            raise TraeRefreshError("缺少 refreshToken，请打开 Trae CN 客户端重新登录。")
        if not creds.refreshable:
            raise TraeRefreshError(
                "缺少设备号或设备密钥，无法自动续期；请打开 Trae CN 客户端重新登录。"
            )
        if log:
            log(
                "warning",
                "Trae 登录态已过期，正在尝试自动续期；续期会轮换 refreshToken，"
                "如 Trae 客户端掉线请重新登录。",
            )

        base = self._base_url(config, creds)
        combinations = (
            (CLIENT_ID_IDE, "IDE_PC"),
            (CLIENT_ID_LEGACY, "IDE_PC"),
            (CLIENT_ID_LEGACY, "SOLO_PC"),
        )
        last_error = "续期失败"
        for client_id, platform_code in combinations:
            try:
                proof, device_info = self._device_proof(creds, client_id, version)
            except TraeCredentialError as exc:
                raise TraeRefreshError(str(exc)) from exc
            if platform_code != "IDE_PC":
                device_info = self._device_info(creds, version, platform_code)
            body = {
                "ClientID": client_id,
                "ClientSecret": "",
                "RefreshToken": creds.refresh_token,
                "DeviceInfo": device_info,
                "DeviceProof": proof,
                "IDEVersion": version,
            }
            try:
                response = await client.post(
                    f"{base}{REFRESH_PATH}",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": f"Trae/{version}",
                        "x-cloudide-token": "",
                    },
                    json=body,
                )
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
            except Exception as exc:  # noqa: BLE001
                last_error = f"续期请求异常：{type(exc).__name__}"
                break

            token, refresh_token, access_exp, refresh_exp = self._refresh_result(payload)
            if response.is_success and token:
                new_creds = replace(
                    creds,
                    access_token=token,
                    refresh_token=refresh_token or creds.refresh_token,
                    access_expires_at=access_exp or jwt_expiry(token),
                    refresh_expires_at=refresh_exp or creds.refresh_expires_at,
                    cached=True,
                )
                try:
                    save_token_cache(new_creds)
                except Exception as exc:  # noqa: BLE001
                    if log:
                        log("warning", f"Trae 续期成功，但本地加密缓存写入失败：{exc}")
                if log:
                    log("info", "Trae 登录态续期成功，已更新本地加密缓存。")
                return new_creds

            code = _parse_business_code(payload)
            last_error = f"续期失败（HTTP {response.status_code}"
            if code is not None:
                last_error += f"，code={code}"
            last_error += "）"
            meta = payload.get("ResponseMetadata") if isinstance(payload, dict) else None
            meta_err = meta.get("Error") if isinstance(meta, dict) else None
            result_obj = payload.get("Result") if isinstance(payload, dict) else None
            candidate_codes = {
                code,
                _to_int(
                    _root_first(
                        payload,
                        "Code",
                        "errorCode",
                        "ErrorCode",
                        "error_code",
                        "subCode",
                        "SubCode",
                    )
                ),
                _to_int(meta_err.get("CodeN")) if isinstance(meta_err, dict) else None,
                _to_int(meta_err.get("Code")) if isinstance(meta_err, dict) else None,
                _to_int(result_obj.get("Code")) if isinstance(result_obj, dict) else None,
            }
            retryable_codes = {10101, 20403}
            if not (candidate_codes & retryable_codes):
                break
        raise TraeRefreshError(last_error)

    def _fallback_credentials(
        self, config: ProviderConfig, current: TraeCredentials
    ) -> Optional[TraeCredentials]:
        try:
            fresh = load_trae_credentials(config, use_cache=False)
        except TraeCredentialError:
            return None
        if not fresh.access_token and not fresh.refresh_token:
            return None
        if (
            fresh.access_token == current.access_token
            and fresh.refresh_token == current.refresh_token
        ):
            return None
        return fresh

    async def _refresh_with_fallback(
        self,
        client: Any,
        config: ProviderConfig,
        creds: TraeCredentials,
        version: str,
        log: Optional[LogCallback],
    ) -> TraeCredentials:
        try:
            return await self._refresh_token(client, config, creds, version, log)
        except TraeRefreshError:
            fallback = self._fallback_credentials(config, creds)
            if fallback is None:
                raise
            if log:
                log(
                    "warning",
                    "Trae 缓存续期失败，已回退到客户端当前登录态重试。",
                )
            if token_is_expired(fallback):
                if not fallback.refreshable:
                    raise
                return await self._refresh_token(
                    client, config, fallback, version, log
                )
            try:
                save_token_cache(fallback)
            except Exception as exc:  # noqa: BLE001
                if log:
                    log("warning", f"Trae 回退客户端登录态后，缓存更新失败：{exc}")
            return fallback

    async def _verify_claim_status(
        self,
        client: Any,
        config: ProviderConfig,
        creds: TraeCredentials,
        version: str,
    ) -> tuple[Optional[_StatusInfo], str]:
        last_info: Optional[_StatusInfo] = None
        last_status = "unconfirmed"
        for attempt in range(self._claim_verify_attempts):
            try:
                response, payload = await self._status(
                    client, config, creds, version
                )
            except Exception:  # noqa: BLE001
                last_status = "unavailable"
            else:
                code = _parse_business_code(payload)
                if not response.is_success or code not in (None, 0):
                    last_status = "unavailable"
                else:
                    info = _StatusInfo(payload)
                    if info.schema_ok and info.checked_in:
                        return info, "confirmed"
                    if info.schema_ok and not info.checked_in:
                        last_info = info
                        last_status = "unconfirmed"
                    else:
                        last_status = "unavailable"
            if (
                attempt + 1 < self._claim_verify_attempts
                and self._claim_verify_delay_seconds > 0
            ):
                await asyncio.sleep(self._claim_verify_delay_seconds)
        return last_info, last_status

    # ---- Doctor ----

    async def doctor(self, config: ProviderConfig) -> DoctorResult:
        import httpx

        try:
            exe = self.detect_client_exe(config)
            storage_path = find_storage_json(config)
        except Exception as exc:  # noqa: BLE001
            return DoctorResult(
                provider_id=self.id,
                display_name=self.display_name,
                client_installed=False,
                client_exe_path=None,
                auth_file_found=False,
                auth_file_path=None,
                auth_readable=False,
                healthy=False,
                message=f"检测 Trae 本地路径失败：{type(exc).__name__}。",
            )
        try:
            creds = load_trae_credentials(config)
        except TraeCredentialError as exc:
            return DoctorResult(
                provider_id=self.id,
                display_name=self.display_name,
                client_installed=bool(exe),
                client_exe_path=exe,
                auth_file_found=storage_path is not None,
                auth_file_path=str(storage_path) if storage_path else None,
                auth_readable=False,
                healthy=False,
                message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return DoctorResult(
                provider_id=self.id,
                display_name=self.display_name,
                client_installed=bool(exe),
                client_exe_path=exe,
                auth_file_found=storage_path is not None,
                auth_file_path=str(storage_path) if storage_path else None,
                auth_readable=False,
                healthy=False,
                message=f"读取 Trae 登录态失败：{type(exc).__name__}。",
            )

        allow_refresh = self._allow_token_refresh(config)

        def result(
            healthy: bool, message: str, **extra: Any
        ) -> DoctorResult:
            details = {
                "auth_mode": "cache" if creds.cached else "local_storage",
                "host": self._base_url(config, creds),
                "device_id": mask_secret(creds.device_id),
                "refreshable": creds.refreshable,
                "auto_refresh": allow_refresh,
                **extra,
            }
            return DoctorResult(
                provider_id=self.id,
                display_name=self.display_name,
                client_installed=bool(exe),
                client_exe_path=exe,
                auth_file_found=bool(creds.source_path),
                auth_file_path=creds.source_path,
                auth_readable=True,
                account_label=creds.account_label,
                token_expires_at=token_expires_at(creds),
                healthy=healthy,
                message=message,
                details=details,
            )

        if not creds.device_id:
            return result(
                False,
                "已读取 Trae 登录态，但未找到真实设备号；请打开 Trae CN 客户端重新登录。",
            )
        if token_is_expired(creds):
            if creds.refreshable and allow_refresh:
                return result(
                    True,
                    "Trae 登录态已过期，执行签到时会自动续期；如客户端掉线请重新登录。",
                    verified=False,
                )
            if creds.refreshable and not allow_refresh:
                return result(
                    False,
                    "Trae 登录态已过期，自动续期已关闭；请打开客户端刷新登录态。",
                    verified=False,
                )
            return result(
                False,
                "Trae 登录态已过期且无法自动续期，请打开 Trae CN 客户端重新登录。",
                verified=False,
            )

        version = self._client_version(config, exe=exe)
        details: dict[str, Any] = {"verified": False}
        message = "本地 Trae 登录态有效。"
        healthy = True
        try:
            async with self._http_client(config) as client:
                response, payload = await self._status(client, config, creds, version)
                code = _parse_business_code(payload)
                if response.status_code in (401, 403) or code in AUTH_CODES:
                    if creds.refreshable and allow_refresh:
                        message = "Trae 在线校验未通过，执行签到时会尝试自动续期。"
                        details.update(verified=False)
                    elif creds.refreshable and not allow_refresh:
                        healthy = False
                        message = "Trae 登录态已失效，自动续期已关闭；请打开客户端重新登录。"
                    else:
                        healthy = False
                        message = "Trae 登录态已失效，请打开客户端重新登录。"
                elif response.status_code in (404, 410):
                    healthy = False
                    message = f"Trae 签到接口返回 HTTP {response.status_code}，接口可能已变更。"
                elif response.status_code == 429 or response.status_code >= 500:
                    message = (
                        f"凭据已配置，但暂时无法在线校验"
                        f"（HTTP {response.status_code}）。"
                    )
                elif not response.is_success:
                    healthy = False
                    message = f"Trae 在线校验失败（HTTP {response.status_code}）。"
                elif code == 9074:
                    healthy = False
                    message = "Trae 设备校验未通过或排队限流，暂时无法在线校验。"
                elif code == 9004:
                    message = "本地登录态有效，今日 Trae 积分已领取。"
                    details.update(today_checked_in=True, verified=True)
                elif code == 9095:
                    healthy = False
                    message = "本机设备今日已签到其他 Trae 账号，当前账号未领取。"
                elif code in (9090, 9102):
                    message = "本地登录态有效，当前 Trae 每日签到活动未开启。"
                    details.update(activity_enabled=False, verified=True)
                elif code not in (None, 0):
                    healthy = False
                    message = f"Trae 签到接口返回 code={code}，可能已改版。"
                else:
                    info = _StatusInfo(payload)
                    if not info.schema_ok:
                        healthy = False
                        message = "Trae 状态接口响应结构异常，接口可能已变更。"
                    elif info.enable is False:
                        message = "本地登录态有效，当前 Trae 每日签到活动未开启。"
                        details.update(activity_enabled=False, verified=True)
                    elif info.checked_in:
                        message = "本地登录态有效，今日 Trae 积分已领取。"
                        details.update(today_checked_in=True, verified=True)
                    else:
                        message = "本地登录态有效，今日可以签到。"
                        details.update(today_checked_in=False, verified=True)
        except httpx.HTTPError as exc:
            message = f"凭据已配置，但暂时无法在线校验：{type(exc).__name__}。"
        except Exception as exc:  # noqa: BLE001
            message = f"凭据已配置，但暂时无法在线校验：{type(exc).__name__}。"
        return result(healthy, message, **details)

    # ---- Check-in ----

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

    @staticmethod
    def _is_auth_status(status_code: int, code: Optional[int]) -> bool:
        return status_code in (401, 403) or code in AUTH_CODES

    @staticmethod
    def _http_error_status(status_code: int) -> Optional[CheckinStatus]:
        if status_code in (404, 410):
            return CheckinStatus.SCHEMA_CHANGED
        if status_code == 429:
            return CheckinStatus.FAILED
        if status_code >= 500:
            return CheckinStatus.FAILED
        if not 200 <= status_code < 300:
            return CheckinStatus.FAILED
        return None

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

        try:
            creds = load_trae_credentials(config)
        except TraeCredentialError as exc:
            return self._result(
                CheckinStatus.NEEDS_MANUAL,
                str(exc),
                biz_date,
                started=started,
                dry_run=dry_run,
                raw_error_type="TraeCredentialError",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                CheckinStatus.FAILED,
                f"读取 Trae 登录态失败：{type(exc).__name__}。",
                biz_date,
                started=started,
                dry_run=dry_run,
                raw_error_type=type(exc).__name__,
            )
        if not creds.device_id:
            return self._result(
                CheckinStatus.NEEDS_MANUAL,
                "未找到 Trae 真实设备号，无法签到；请打开 Trae CN 客户端重新登录。",
                biz_date,
                started=started,
                dry_run=dry_run,
                raw_error_type="TraeDeviceError",
            )

        version = self._client_version(config)
        emit(
            "info",
            f"已读取 Trae 登录态（账号 {creds.account_label or '未知'}，"
            f"设备 {mask_secret(creds.device_id)}）。",
        )

        refreshed = False
        allow_refresh = self._allow_token_refresh(config)
        try:
            async with self._http_client(config) as client:
                if token_is_expired(creds):
                    if dry_run:
                        return self._result(
                            CheckinStatus.AUTH_EXPIRED,
                            "Dry-Run 不执行登录态续期；请先打开 Trae CN 客户端刷新登录态。",
                            biz_date,
                            started=started,
                            dry_run=dry_run,
                            raw_error_type="TraeAuthExpired",
                        )
                    if not allow_refresh:
                        return self._result(
                            CheckinStatus.AUTH_EXPIRED,
                            "Trae 登录态已过期，自动续期已关闭；请打开客户端刷新登录态。",
                            biz_date,
                            started=started,
                            dry_run=dry_run,
                            raw_error_type="TraeAuthExpired",
                        )
                    if not creds.refreshable:
                        return self._result(
                            CheckinStatus.AUTH_EXPIRED,
                            "Trae 登录态已过期且无法自动续期，请打开客户端重新登录。",
                            biz_date,
                            started=started,
                            dry_run=dry_run,
                            raw_error_type="TraeAuthExpired",
                        )
                    try:
                        creds = await self._refresh_with_fallback(
                            client, config, creds, version, emit
                        )
                        refreshed = True
                    except TraeRefreshError as exc:
                        return self._result(
                            CheckinStatus.AUTH_EXPIRED,
                            f"Trae 登录态续期失败：{exc}",
                            biz_date,
                            started=started,
                            dry_run=dry_run,
                            raw_error_type="TraeRefreshError",
                        )

                emit("info", "正在查询 Trae 今日签到状态...")
                response, payload = await self._status(
                    client, config, creds, version
                )
                code = _parse_business_code(payload)
                if self._is_auth_status(response.status_code, code):
                    if (
                        not dry_run
                        and not refreshed
                        and creds.refreshable
                        and allow_refresh
                    ):
                        try:
                            creds = await self._refresh_with_fallback(
                                client, config, creds, version, emit
                            )
                            refreshed = True
                        except TraeRefreshError as exc:
                            return self._result(
                                CheckinStatus.AUTH_EXPIRED,
                                f"Trae 登录态续期失败：{exc}",
                                biz_date,
                                started=started,
                                dry_run=dry_run,
                                raw_error_type="TraeRefreshError",
                            )
                        response, payload = await self._status(
                            client, config, creds, version
                        )
                        code = _parse_business_code(payload)
                    if self._is_auth_status(response.status_code, code):
                        message = (
                            "Trae 登录态已失效，自动续期已关闭；请打开客户端重新登录。"
                            if not allow_refresh
                            else "Trae 登录态已失效，请打开 Trae CN 客户端重新登录。"
                        )
                        return self._result(
                            CheckinStatus.AUTH_EXPIRED,
                            message,
                            biz_date,
                            started=started,
                            dry_run=dry_run,
                            raw_error_type="TraeAuthError",
                        )

                http_status = self._http_error_status(response.status_code)
                if http_status is not None:
                    if http_status == CheckinStatus.SCHEMA_CHANGED:
                        message = f"Trae 签到接口返回 HTTP {response.status_code}，接口可能已变更。"
                    elif response.status_code == 429:
                        message = "Trae 请求过于频繁，请稍后重试。"
                    elif response.status_code >= 500:
                        message = f"Trae 服务端暂时异常（HTTP {response.status_code}）。"
                    else:
                        message = f"Trae 状态查询失败（HTTP {response.status_code}）。"
                    return self._result(
                        http_status,
                        message,
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type=f"HTTP_{response.status_code}",
                    )

                if code == 9074:
                    return self._result(
                        CheckinStatus.FAILED,
                        "Trae 设备校验或排队限流（code=9074），本次未领取，稍后重试。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="TraeRateLimit",
                    )
                if code == 9095:
                    return self._result(
                        CheckinStatus.NEEDS_MANUAL,
                        "本机设备今日已签到其他 Trae 账号，当前账号未领取；请更换设备或明日再试。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="TraeDeviceConflict",
                    )
                if code == 9004:
                    return self._result(
                        CheckinStatus.ALREADY_DONE,
                        "今日 Trae 积分已领取。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        snapshot=CreditSnapshot(delta=0),
                    )
                if code in (9090, 9102):
                    return self._result(
                        CheckinStatus.UNAVAILABLE,
                        "当前没有可用的 Trae 每日签到活动。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type=f"TraeCode_{code}",
                    )
                if code not in (None, 0):
                    return self._result(
                        CheckinStatus.FAILED,
                        f"Trae 状态接口返回 code={code}：{_parse_message(payload) or '未知错误'}",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type=f"TraeCode_{code}",
                    )

                info = _StatusInfo(payload)
                if not info.schema_ok:
                    return self._result(
                        CheckinStatus.SCHEMA_CHANGED,
                        "Trae 状态接口响应结构异常（缺少 enable/checked_in），接口可能已变更。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="TraeSchemaError",
                    )
                if info.enable is False:
                    return self._result(
                        CheckinStatus.UNAVAILABLE,
                        "当前 Trae 账号未开启每日签到活动。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                    )
                if info.checked_in:
                    return self._result(
                        CheckinStatus.ALREADY_DONE,
                        "今日 Trae 积分已领取。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        snapshot=CreditSnapshot(delta=0),
                    )

                amount_hint = info.reward_hint or config.checkin_credits
                if dry_run:
                    return self._result(
                        CheckinStatus.SUCCESS,
                        f"Dry-Run：今日可签到（预计 {amount_hint} 积分，未提交）。",
                        biz_date,
                        started=started,
                        dry_run=True,
                    )

                emit("info", "正在提交 Trae 每日签到...")
                claim_response, claim_payload = await self._claim(
                    client, config, creds, version
                )
                claim_code = _parse_business_code(claim_payload)
                if self._is_auth_status(claim_response.status_code, claim_code):
                    if not refreshed and creds.refreshable and allow_refresh:
                        try:
                            creds = await self._refresh_with_fallback(
                                client, config, creds, version, emit
                            )
                            refreshed = True
                        except TraeRefreshError as exc:
                            return self._result(
                                CheckinStatus.AUTH_EXPIRED,
                                f"Trae 登录态续期失败：{exc}",
                                biz_date,
                                started=started,
                                dry_run=dry_run,
                                raw_error_type="TraeRefreshError",
                            )
                        claim_response, claim_payload = await self._claim(
                            client, config, creds, version
                        )
                        claim_code = _parse_business_code(claim_payload)
                    if self._is_auth_status(claim_response.status_code, claim_code):
                        message = (
                            "Trae 领取凭证已失效，自动续期已关闭；请打开客户端重新登录。"
                            if not allow_refresh
                            else "Trae 领取凭证已失效，请打开客户端重新登录。"
                        )
                        return self._result(
                            CheckinStatus.AUTH_EXPIRED,
                            message,
                            biz_date,
                            started=started,
                            dry_run=dry_run,
                            raw_error_type="TraeAuthError",
                        )

                claim_http = self._http_error_status(claim_response.status_code)
                if claim_http is not None:
                    if claim_http == CheckinStatus.SCHEMA_CHANGED:
                        message = f"Trae 领取接口返回 HTTP {claim_response.status_code}，接口可能已变更。"
                    elif claim_response.status_code == 429:
                        message = "Trae 请求过于频繁，请稍后重试。"
                    elif claim_response.status_code >= 500:
                        message = f"Trae 服务端暂时异常（HTTP {claim_response.status_code}）。"
                    else:
                        message = f"Trae 领取失败（HTTP {claim_response.status_code}）。"
                    return self._result(
                        claim_http,
                        message,
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type=f"HTTP_{claim_response.status_code}",
                    )

                if claim_code is None:
                    return self._result(
                        CheckinStatus.SCHEMA_CHANGED,
                        "Trae 领取响应缺少业务码，接口可能已变更。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="TraeSchemaError",
                    )
                claim_message = _parse_message(claim_payload)
                if claim_code == 9004:
                    return self._result(
                        CheckinStatus.ALREADY_DONE,
                        "今日 Trae 积分已领取。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        snapshot=CreditSnapshot(delta=0),
                    )
                if claim_code == 9095:
                    return self._result(
                        CheckinStatus.NEEDS_MANUAL,
                        "本机设备今日已签到其他 Trae 账号，当前账号未领取；请更换设备或明日再试。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="TraeDeviceConflict",
                    )
                if claim_code == 9074:
                    return self._result(
                        CheckinStatus.FAILED,
                        "Trae 设备校验或排队限流（code=9074），本次未领取，稍后重试。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type="TraeRateLimit",
                    )
                if claim_code in (9090, 9102):
                    return self._result(
                        CheckinStatus.UNAVAILABLE,
                        "当前没有可用的 Trae 每日签到活动。",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type=f"TraeCode_{claim_code}",
                    )
                if claim_code != 0:
                    return self._result(
                        CheckinStatus.FAILED,
                        f"Trae 签到返回 code={claim_code}：{claim_message or '未知错误'}",
                        biz_date,
                        started=started,
                        dry_run=dry_run,
                        raw_error_type=f"TraeCode_{claim_code}",
                    )

                emit("info", "Trae 领取接口返回成功，正在二次确认到账状态...")
                claim_amount = _payload_amount(claim_payload)
                amount = claim_amount or info.reward_hint or config.checkin_credits
                verify_info, verify_state = await self._verify_claim_status(
                    client, config, creds, version
                )
                verification_warning = ""
                verify_error: Optional[str] = None
                if verify_state == "confirmed":
                    amount = (
                        claim_amount
                        or (verify_info.reward_hint if verify_info else None)
                        or amount
                    )
                elif verify_state == "unconfirmed":
                    verification_warning = "（状态暂未刷新，请稍后在 Trae 客户端确认）"
                    verify_error = "TraeClaimUnconfirmed"
                else:
                    verification_warning = "（二次确认暂不可用）"
                    verify_error = "TraeClaimUnconfirmed"

                amount = min(max(amount, 0), MAX_CHECKIN_CREDITS)
                snapshot = CreditSnapshot(
                    delta=amount,
                    expiring_soon=amount if amount > 0 else None,
                    expiring_at=(
                        datetime.now(timezone.utc)
                        + timedelta(days=DEFAULT_CREDIT_VALIDITY_DAYS)
                    ).isoformat()
                    if amount > 0
                    else None,
                )
                return self._result(
                    CheckinStatus.SUCCESS,
                    f"已领取 Trae 国内版每日 {amount} 积分{verification_warning}。",
                    biz_date,
                    started=started,
                    dry_run=dry_run,
                    snapshot=snapshot,
                    raw_error_type=verify_error,
                )
        except httpx.HTTPError as exc:
            return self._result(
                CheckinStatus.FAILED,
                f"Trae 网络请求失败：{type(exc).__name__}",
                biz_date,
                started=started,
                dry_run=dry_run,
                raw_error_type=type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                CheckinStatus.FAILED,
                f"Trae 适配器执行异常：{type(exc).__name__}",
                biz_date,
                started=started,
                dry_run=dry_run,
                raw_error_type=type(exc).__name__,
            )