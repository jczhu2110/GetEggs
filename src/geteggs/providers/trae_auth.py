"""Read-only Trae CN local login state and DPAPI-backed token cache."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from geteggs.core.config import get_trae_auth_path
from geteggs.core.models import ProviderConfig
from geteggs.core.secure_store import load_secure_json, save_secure_json
from geteggs.providers.base import mask_secret

DEFAULT_BASE_URL = "https://api.trae.cn"
CLOUDIDE_KEY = "iCubeAuthInfo://icube.cloudide"
DEVICE_KEY_PREFIX = "iCubeAuthInfo://icube-dc:"
STORAGE_SUFFIX = ("User", "globalStorage", "storage.json")
_DEVICE_ID_RE = re.compile(r"^\d{12,20}$")

_AES_HEADER = bytes([0x74, 0x63, 0x05, 0x10, 0x00, 0x00])
_AES_PRIVATE_HEADER = bytes([0x12, 0x39, 0x20, 0x20, 0x02, 0x03])
_SALT_AES = bytes.fromhex(
    "4dd4c2e6b83162090e52b3c7a6733ba41cb2462b829ab58a196b39db5717752"
    "4f49baf7f08e8d68d26a72e37c1a95a2f1f05a51892aef2949732b62a38aadd58"
)
_SALT_AES_PRIVATE = bytes.fromhex(
    "490cc21292b05d0cc06ccbe91fb92e21b5a7c538811dc85e27a96e28104e1cf6"
    "9c7166efab232807390da78a8b91e49c44b49a43feb941a1e46f2656e613e47b"
)


class TraeCredentialError(ValueError):
    """Raised when Trae login state exists but cannot be read or decrypted."""


@dataclass
class TraeCredentials:
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    access_expires_at: Optional[str] = None
    refresh_expires_at: Optional[str] = None
    user_id: Optional[str] = None
    username: Optional[str] = None
    host: str = DEFAULT_BASE_URL
    device_id: Optional[str] = None
    device_private_key_pem: Optional[str] = None
    device_public_key_pem: Optional[str] = None
    machine_id: Optional[str] = None
    source_path: Optional[str] = None
    req_source: int = 1
    cached: bool = False

    @property
    def account_label(self) -> Optional[str]:
        return mask_secret(self.username or self.user_id)

    @property
    def refreshable(self) -> bool:
        return bool(
            self.refresh_token
            and self.device_id
            and self.device_private_key_pem
            and self.device_public_key_pem
        )


def normalize_pem(value: Optional[str]) -> Optional[str]:
    """Restore newlines when a PEM arrives with literal ``\\n`` sequences."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if "\\n" in cleaned and "\n" not in cleaned:
        cleaned = cleaned.replace("\\n", "\n")
    return cleaned


def _aes_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise TraeCredentialError(
            "缺少 cryptography 依赖，无法读取 Trae 本地登录态；请执行 uv sync。"
        ) from exc
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def sign_ecdsa_p256(private_key_pem: str, data: bytes) -> str:
    """Sign bytes with the client device key and return base64(DER)."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise TraeCredentialError(
            "缺少 cryptography 依赖，无法续期 Trae 登录态；请执行 uv sync。"
        ) from exc
    try:
        private_key = serialization.load_pem_private_key(
            normalize_pem(private_key_pem).encode("utf-8"), password=None
        )
        der = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
        if s > order // 2:
            s = order - s
        return base64.b64encode(utils.encode_dss_signature(r, s)).decode("ascii")
    except TraeCredentialError:
        raise
    except Exception as exc:
        raise TraeCredentialError("Trae 设备私钥签名失败。") from exc


def decrypt_icube_value(value: str) -> dict[str, Any]:
    """Decrypt one ``iCubeAuthInfo`` value (or accept a plaintext JSON value)."""
    text = (value or "").strip()
    if not text:
        raise TraeCredentialError("Trae 登录态值为空。")
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise TraeCredentialError("Trae 登录态明文 JSON 无法解析。") from exc
        if not isinstance(parsed, dict):
            raise TraeCredentialError("Trae 登录态不是 JSON 对象。")
        return parsed

    try:
        raw = base64.b64decode(text, validate=False)
    except (ValueError, TypeError) as exc:
        raise TraeCredentialError("Trae 登录态不是有效的 Base64。") from exc
    if len(raw) < 102:
        raise TraeCredentialError("Trae 登录态长度异常。")

    header = raw[:6]
    if header == _AES_HEADER:
        salt = _SALT_AES
    elif header == _AES_PRIVATE_HEADER:
        salt = _SALT_AES_PRIVATE
    else:
        raise TraeCredentialError(
            "Trae 登录态使用了未知的加密格式，可能已被新版本客户端变更。"
        )

    random_bytes = raw[6:38]
    ciphertext = raw[38:]
    final_hash = hashlib.sha512(
        hashlib.sha512(random_bytes).digest() + salt
    ).digest()
    try:
        decrypted = _aes_decrypt(final_hash[:16], final_hash[16:32], ciphertext)
    except TraeCredentialError:
        raise
    except (ValueError, TypeError) as exc:
        raise TraeCredentialError(
            "Trae 登录态解密失败，加密格式可能已变更。"
        ) from exc

    stored_hash = decrypted[:64]
    plaintext = decrypted[64:]
    pad = plaintext[-1] if plaintext else 0
    if 1 <= pad <= 16:
        plaintext = plaintext[:-pad]
    else:
        plaintext = plaintext.rstrip(b"\x00").rstrip()
    if stored_hash != hashlib.sha512(plaintext).digest():
        raise TraeCredentialError("Trae 登录态解密校验失败，格式可能已变更。")
    try:
        parsed = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TraeCredentialError("Trae 登录态解密后不是有效 JSON。") from exc
    if not isinstance(parsed, dict):
        raise TraeCredentialError("Trae 登录态解密后不是 JSON 对象。")
    return parsed


def _custom_storage_path(config: ProviderConfig) -> Optional[Path]:
    raw = (
        str(config.extra.get("auth_file_path") or "").strip()
        or str(os.environ.get("GETEGGS_TRAE_STORAGE_PATH") or "").strip()
    )
    return Path(raw).expanduser() if raw else None


def _candidate_storage_paths(config: ProviderConfig) -> list[Path]:
    candidates: list[Path] = []
    custom = _custom_storage_path(config)
    if custom is not None:
        if custom.is_dir():
            candidates.append(custom.joinpath(*STORAGE_SUFFIX))
            candidates.append(custom / "storage.json")
            candidates.append(custom / "globalStorage" / "storage.json")
        else:
            candidates.append(custom)
    else:
        names = ("Trae CN", "TRAE SOLO CN", "TRAE SOLO", "Trae")
        appdata = os.environ.get("APPDATA")
        if appdata:
            for name in names:
                candidates.append(Path(appdata) / name / Path(*STORAGE_SUFFIX))
        home = Path.home()
        for name in names:
            candidates.append(
                home / "Library" / "Application Support" / name / Path(*STORAGE_SUFFIX)
            )
        for base in (home, home / ".config"):
            for name in (".trae-cn", ".trae", "Trae CN", "TRAE SOLO CN"):
                candidates.append(base / name / Path(*STORAGE_SUFFIX))

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path.expanduser().resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def find_storage_json(config: ProviderConfig) -> Optional[Path]:
    for path in _candidate_storage_paths(config):
        if path.is_file():
            return path
    return None


def _valid_device_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if _DEVICE_ID_RE.fullmatch(text) else None


def _coerce_req_source(value: Any, default: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number in (1, 2) else default


def find_device_ids(storage: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key, value in storage.items():
        text = str(key or "")
        if text.startswith(DEVICE_KEY_PREFIX):
            candidate = _valid_device_id(text[len(DEVICE_KEY_PREFIX) :])
        elif text.rstrip(":") == DEVICE_KEY_PREFIX.rstrip(":"):
            candidate = _valid_device_id(value)
        else:
            candidate = None
        if candidate and candidate not in found:
            found.append(candidate)
    return found


def find_device_id(storage: dict[str, Any]) -> Optional[str]:
    ids = find_device_ids(storage)
    return ids[0] if ids else None


def _load_device_info(
    storage: dict[str, Any], device_id: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    if not device_id:
        return None, None
    blob = storage.get(f"{DEVICE_KEY_PREFIX}{device_id}")
    if not isinstance(blob, str) or not blob.strip():
        return None, None
    try:
        info = decrypt_icube_value(blob)
    except TraeCredentialError:
        return None, None
    return (
        normalize_pem(info.get("privateKeyPEM")),
        normalize_pem(info.get("publicKeyPEM")),
    )


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return _parse_time(float(text))
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    moment = _parse_time(value)
    return moment.isoformat() if moment else None


def _jwt_claims(token: Optional[str]) -> dict[str, Any]:
    if not token or token.count(".") != 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def jwt_expiry(token: Optional[str]) -> Optional[str]:
    claims = _jwt_claims(token)
    for key in ("exp", "TokenExpireAt", "tokenExpireAt"):
        raw = claims.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = float(str(raw))
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        if value > 100_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return None


def jwt_user_id(token: Optional[str]) -> Optional[str]:
    claims = _jwt_claims(token)
    data = claims.get("data") if isinstance(claims.get("data"), dict) else {}
    uid = data.get("id") or claims.get("userId") or claims.get("user_id")
    return str(uid).strip() if uid is not None and str(uid).strip() else None


def token_expires_at(creds: TraeCredentials) -> Optional[str]:
    return (
        _iso(creds.access_expires_at)
        or jwt_expiry(creds.access_token)
        or creds.access_expires_at
    )


def token_is_expired(creds: TraeCredentials, *, skew_seconds: int = 60) -> bool:
    if not creds.access_token:
        return True
    expiry = _parse_time(token_expires_at(creds))
    if expiry is None:
        return False
    return expiry.timestamp() <= datetime.now(timezone.utc).timestamp() + skew_seconds


def _load_cache() -> Optional[dict[str, Any]]:
    path = get_trae_auth_path()
    if not path.is_file():
        return None
    try:
        data = load_secure_json(path)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_token_cache(creds: TraeCredentials) -> None:
    """Persist rotated tokens outside the Trae client; never touch storage.json."""
    if not creds.access_token and not creds.refresh_token:
        return
    payload = {
        "access_token": creds.access_token,
        "refresh_token": creds.refresh_token,
        "access_expires_at": creds.access_expires_at,
        "refresh_expires_at": creds.refresh_expires_at,
        "user_id": creds.user_id,
        "username": creds.username,
        "host": creds.host,
        "device_id": creds.device_id,
        "machine_id": creds.machine_id,
        "req_source": creds.req_source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_secure_json(get_trae_auth_path(), payload)


def _cache_credentials(cache: dict[str, Any], source_path: str) -> TraeCredentials:
    return TraeCredentials(
        access_token=str(cache.get("access_token") or "").strip() or None,
        refresh_token=str(cache.get("refresh_token") or "").strip() or None,
        access_expires_at=_iso(cache.get("access_expires_at")),
        refresh_expires_at=_iso(cache.get("refresh_expires_at")),
        user_id=str(cache.get("user_id") or "").strip() or None,
        username=str(cache.get("username") or "").strip() or None,
        host=str(cache.get("host") or DEFAULT_BASE_URL).rstrip("/"),
        device_id=_valid_device_id(cache.get("device_id")),
        machine_id=str(cache.get("machine_id") or "").strip() or None,
        source_path=source_path,
        req_source=_coerce_req_source(cache.get("req_source")),
        cached=True,
    )


def _merge_cache(creds: TraeCredentials, cache: Optional[dict[str, Any]]) -> TraeCredentials:
    if not cache:
        return creds
    cache_uid = str(cache.get("user_id") or "").strip() or None
    if creds.user_id and cache_uid and cache_uid != creds.user_id:
        return creds
    cache_device = _valid_device_id(cache.get("device_id"))
    if creds.device_id and cache_device and creds.device_id != cache_device:
        return creds
    if cache.get("req_source") is not None:
        if _coerce_req_source(cache.get("req_source")) != creds.req_source:
            return creds

    cached_access = str(cache.get("access_token") or "").strip() or None
    if not cached_access:
        return creds
    cached_expiry = _parse_time(cache.get("access_expires_at")) or _parse_time(
        jwt_expiry(cached_access)
    )
    current_expiry = _parse_time(token_expires_at(creds))
    if creds.access_token and current_expiry and cached_expiry:
        if cached_expiry <= current_expiry:
            return creds
    elif creds.access_token and not cached_expiry:
        return creds

    creds.access_token = cached_access
    cached_refresh = str(cache.get("refresh_token") or "").strip()
    if cached_refresh:
        creds.refresh_token = cached_refresh
    creds.access_expires_at = _iso(cache.get("access_expires_at")) or jwt_expiry(cached_access)
    creds.refresh_expires_at = _iso(cache.get("refresh_expires_at")) or creds.refresh_expires_at
    creds.user_id = creds.user_id or cache_uid
    creds.username = creds.username or str(cache.get("username") or "").strip() or None
    creds.device_id = creds.device_id or _valid_device_id(cache.get("device_id"))
    creds.machine_id = creds.machine_id or str(cache.get("machine_id") or "").strip() or None
    creds.host = str(cache.get("host") or creds.host).rstrip("/")
    creds.cached = True
    return creds


def _load_from_storage_path(storage_path: Path) -> TraeCredentials:
    try:
        storage = json.loads(storage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TraeCredentialError(
            f"无法读取 Trae 登录态文件 {storage_path}：{type(exc).__name__}"
        ) from exc
    if not isinstance(storage, dict):
        raise TraeCredentialError("Trae 登录态文件不是 JSON 对象。")

    blob = storage.get(CLOUDIDE_KEY)
    if not isinstance(blob, str) or not blob.strip():
        raise TraeCredentialError(
            "Trae CN 客户端当前未登录，请先打开客户端完成登录。"
        )
    try:
        account = decrypt_icube_value(blob)
    except TraeCredentialError as exc:
        raise TraeCredentialError(
            f"{exc} 请打开 Trae CN 客户端重新登录；如仍失败，客户端登录态格式可能已变更。"
        ) from exc

    token = account.get("token") or account.get("accessToken") or account.get("access_token")
    refresh = account.get("refreshToken") or account.get("refresh_token")
    access_token = token.strip() if isinstance(token, str) and token.strip() else None
    refresh_token = refresh.strip() if isinstance(refresh, str) and refresh.strip() else None
    if not access_token and not refresh_token:
        raise TraeCredentialError(
            "Trae 登录态中没有找到 accessToken 或 refreshToken，请重新登录客户端。"
        )

    device_id: Optional[str] = None
    private_pem: Optional[str] = None
    public_pem: Optional[str] = None
    for candidate in find_device_ids(storage):
        if device_id is None:
            device_id = candidate
        private, public = _load_device_info(storage, candidate)
        if private and public:
            device_id, private_pem, public_pem = candidate, private, public
            break

    user_id = str(account.get("userId") or "").strip() or jwt_user_id(access_token)
    raw_account = account.get("account") if isinstance(account.get("account"), dict) else {}
    username = str(raw_account.get("username") or "").strip() or None
    host = str(account.get("host") or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
    parts = {part.lower() for part in storage_path.parts}
    req_source = 2 if ("trae solo cn" in parts or "trae solo" in parts) else 1

    return TraeCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=_iso(account.get("expiredAt")) or jwt_expiry(access_token),
        refresh_expires_at=_iso(account.get("refreshExpiredAt")),
        user_id=user_id,
        username=username,
        host=host,
        device_id=device_id,
        device_private_key_pem=private_pem,
        device_public_key_pem=public_pem,
        machine_id=str(storage.get("telemetry.machineId") or "").strip() or None,
        source_path=str(storage_path),
        req_source=req_source,
        cached=False,
    )


def load_trae_credentials(
    config: ProviderConfig, *, use_cache: bool = True
) -> TraeCredentials:
    """Load Trae CN credentials from the local client, falling back to the cache."""
    try:
        custom_path = _custom_storage_path(config)
        candidates = _candidate_storage_paths(config)
    except (OSError, ValueError) as exc:
        raise TraeCredentialError(
            f"Trae 登录态路径无效：{type(exc).__name__}"
        ) from exc

    saw_storage = False
    last_error: Optional[TraeCredentialError] = None
    for storage_path in candidates:
        if not storage_path.is_file():
            continue
        saw_storage = True
        try:
            creds = _load_from_storage_path(storage_path)
        except TraeCredentialError as exc:
            last_error = exc
            continue
        return _merge_cache(creds, _load_cache()) if use_cache else creds

    if saw_storage and last_error is not None:
        raise last_error
    if custom_path is not None:
        raise TraeCredentialError(
            "指定的 Trae 登录态文件路径不存在，请检查高级配置中的 auth_file_path。"
        )

    if use_cache:
        cache = _load_cache()
        if cache:
            creds = _cache_credentials(cache, str(get_trae_auth_path()))
            if creds.access_token or creds.refresh_token:
                return creds
    if last_error is not None:
        raise last_error
    raise TraeCredentialError(
        "未找到 Trae CN 本地登录态。请安装并登录 Trae CN 客户端，"
        "或在 Trae 工具的高级配置中指定 storage.json 路径。"
    )