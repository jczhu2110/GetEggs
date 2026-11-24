"""Unit tests for the Trae CN provider and local credential reader."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from geteggs.core.config import get_trae_auth_path
from geteggs.core.models import CheckinStatus, ProviderConfig
from geteggs.core.secure_store import save_secure_json
from geteggs.core.storage import Storage
from geteggs.providers.registry import get_provider
from geteggs.providers.trae import (
    TraeProvider,
    _parse_business_code,
    _StatusInfo,
)
from geteggs.providers.trae_auth import (
    CLOUDIDE_KEY,
    DEVICE_KEY_PREFIX,
    TraeCredentialError,
    _SALT_AES,
    _AES_HEADER,
    decrypt_icube_value,
    load_trae_credentials,
)

DEVICE_ID = "1234567890123456"
BIZ_DATE = "2026-11-21"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path / "home"))


def _encrypt_value(value: dict[str, Any]) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    random_bytes = os.urandom(32)
    final_hash = hashlib.sha512(
        hashlib.sha512(random_bytes).digest() + _SALT_AES
    ).digest()
    plaintext = json.dumps(value, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha512(plaintext).digest()
    pad = 16 - (len(plaintext) % 16)
    padded = digest + plaintext + bytes([pad]) * pad
    encryptor = Cipher(
        algorithms.AES(final_hash[:16]), modes.CBC(final_hash[16:32])
    ).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(_AES_HEADER + random_bytes + ciphertext).decode("ascii")


def _device_keys() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_pem, public_pem


def _storage_payload(
    *,
    access_expires: Optional[datetime] = None,
    include_keys: bool = True,
) -> dict[str, Any]:
    if access_expires is None:
        access_expires = datetime.now(timezone.utc) + timedelta(days=10)
    refresh_expires = datetime.now(timezone.utc) + timedelta(days=180)
    account = {
        "token": "test-access-token",
        "refreshToken": "test-refresh-token",
        "expiredAt": access_expires.isoformat().replace("+00:00", "Z"),
        "refreshExpiredAt": refresh_expires.isoformat().replace("+00:00", "Z"),
        "userId": "user-123",
        "host": "https://api.trae.cn",
        "account": {"username": "testuser"},
    }
    storage: dict[str, Any] = {
        CLOUDIDE_KEY: _encrypt_value(account),
        "telemetry.machineId": "machine-1",
    }
    if include_keys:
        private_pem, public_pem = _device_keys()
        storage[f"{DEVICE_KEY_PREFIX}{DEVICE_ID}"] = _encrypt_value(
            {"privateKeyPEM": private_pem, "publicKeyPEM": public_pem}
        )
    else:
        storage[f"{DEVICE_KEY_PREFIX}{DEVICE_ID}"] = _encrypt_value({})
    return storage


def _write_storage_file(
    path: Path,
    *,
    access_expires: Optional[datetime] = None,
    include_keys: bool = True,
) -> Path:
    payload = _storage_payload(
        access_expires=access_expires, include_keys=include_keys
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_storage(
    tmp_path: Path,
    *,
    access_expires: Optional[datetime] = None,
    include_keys: bool = True,
) -> Path:
    return _write_storage_file(
        tmp_path / "storage.json",
        access_expires=access_expires,
        include_keys=include_keys,
    )


def _config(path: Optional[Path] = None, **extra: Any) -> ProviderConfig:
    payload: dict[str, Any] = {"app_version": "1.107.1", **extra}
    if path is not None:
        payload["auth_file_path"] = str(path)
    return ProviderConfig(provider_id="trae", extra=payload)


def _provider(handler: Any) -> TraeProvider:
    return TraeProvider(transport=httpx.MockTransport(handler))


def test_trae_local_credentials_and_device_keys(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    creds = load_trae_credentials(_config(path))

    assert creds.access_token == "test-access-token"
    assert creds.refresh_token == "test-refresh-token"
    assert creds.device_id == DEVICE_ID
    assert creds.host == "https://api.trae.cn"
    assert creds.account_label == "tes***ser"
    assert creds.refreshable is True


def test_trae_already_checked_in(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path.endswith("/checkin_credits/status")
        assert request.headers["authorization"] == "Cloud-IDE-JWT test-access-token"
        assert request.headers["x-device-id"] == DEVICE_ID
        assert json.loads(request.content) == {"req_source": 1}
        return httpx.Response(
            200,
            json={"code": 0, "enable": True, "checked_in": True, "credits": 100},
        )

    result = asyncio.run(_provider(handler).checkin(_config(path), BIZ_DATE))

    assert result.status == CheckinStatus.ALREADY_DONE
    assert calls == ["/trae/api/v2/ug/checkin_credits/status"]


def test_trae_claim_success_and_verification(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    status_calls = 0
    claim_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls, claim_calls
        if request.url.path.endswith("/checkin_credits/status"):
            status_calls += 1
            checked = status_calls > 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "enable": True,
                    "checked_in": checked,
                    "credits": 100 if checked else 0,
                },
            )
        if request.url.path.endswith("/checkin_credits/claim"):
            claim_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "message": "ok", "data": {"points": 100}},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    result = asyncio.run(_provider(handler).checkin(_config(path), BIZ_DATE))

    assert result.status == CheckinStatus.SUCCESS
    assert claim_calls == 1
    assert status_calls == 2


def test_trae_dry_run_does_not_claim(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    claim_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal claim_calls
        if request.url.path.endswith("/checkin_credits/status"):
            return httpx.Response(
                200,
                json={"code": 0, "enable": True, "checked_in": False, "credits": 100},
            )
        if request.url.path.endswith("/checkin_credits/claim"):
            claim_calls += 1
            return httpx.Response(200, json={"code": 0})
        raise AssertionError(f"unexpected request: {request.url}")

    result = asyncio.run(
        _provider(handler).checkin(_config(path), BIZ_DATE, dry_run=True)
    )

    assert result.status == CheckinStatus.SUCCESS
    assert result.dry_run is True
    assert claim_calls == 0


@pytest.mark.parametrize(
    ("code", "expected", "raw_error"),
    [
        (9004, CheckinStatus.ALREADY_DONE, None),
        (9095, CheckinStatus.NEEDS_MANUAL, "TraeDeviceConflict"),
        (9074, CheckinStatus.FAILED, "TraeRateLimit"),
        (9090, CheckinStatus.UNAVAILABLE, "TraeCode_9090"),
        (9102, CheckinStatus.UNAVAILABLE, "TraeCode_9102"),
        (4001, CheckinStatus.FAILED, "TraeCode_4001"),
    ],
)
def test_trae_claim_business_codes(
    tmp_path: Path,
    code: int,
    expected: CheckinStatus,
    raw_error: Optional[str],
) -> None:
    path = _write_storage(tmp_path)
    claim_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal claim_calls
        if request.url.path.endswith("/checkin_credits/status"):
            return httpx.Response(
                200,
                json={"code": 0, "enable": True, "checked_in": False, "credits": 100},
            )
        if request.url.path.endswith("/checkin_credits/claim"):
            claim_calls += 1
            return httpx.Response(200, json={"code": code, "message": "test"})
        raise AssertionError(f"unexpected request: {request.url}")

    result = asyncio.run(_provider(handler).checkin(_config(path), BIZ_DATE))

    assert result.status == expected
    if raw_error:
        assert result.raw_error_type == raw_error
    assert claim_calls == 1


def test_trae_status_schema_changed_does_not_claim(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    claim_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal claim_calls
        if request.url.path.endswith("/checkin_credits/status"):
            return httpx.Response(200, json={"code": 0, "enable": True})
        claim_calls += 1
        return httpx.Response(200, json={"code": 0})

    result = asyncio.run(_provider(handler).checkin(_config(path), BIZ_DATE))

    assert result.status == CheckinStatus.SCHEMA_CHANGED
    assert claim_calls == 0


def test_trae_refreshes_expired_token_and_persists_cache(tmp_path: Path) -> None:
    path = _write_storage(
        tmp_path, access_expires=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    refresh_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        if request.url.path.endswith("/oauth/ExchangeToken"):
            refresh_calls += 1
            body = json.loads(request.content)
            assert body["RefreshToken"] == "test-refresh-token"
            assert body["DeviceInfo"]["DeviceID"] == DEVICE_ID
            assert body["DeviceProof"]["Signature"]
            return httpx.Response(
                200,
                json={"Result": {"Token": "new-token", "RefreshToken": "new-refresh"}},
            )
        if request.url.path.endswith("/checkin_credits/status"):
            return httpx.Response(
                200,
                json={"code": 0, "enable": True, "checked_in": True, "credits": 100},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    result = asyncio.run(_provider(handler).checkin(_config(path), BIZ_DATE))

    assert result.status == CheckinStatus.ALREADY_DONE
    assert refresh_calls == 1
    from geteggs.core.config import get_trae_auth_path
    from geteggs.core.secure_store import load_secure_json

    cached = load_secure_json(get_trae_auth_path())
    assert cached["access_token"] == "new-token"
    assert cached["refresh_token"] == "new-refresh"


def test_trae_doctor_is_read_only(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        assert request.url.path.endswith("/checkin_credits/status")
        return httpx.Response(
            200,
            json={"code": 0, "enable": True, "checked_in": False, "credits": 100},
        )

    doctor = asyncio.run(_provider(handler).doctor(_config(path)))

    assert doctor.healthy is True
    assert "可以签到" in doctor.message
    assert called == ["/trae/api/v2/ug/checkin_credits/status"]


def test_trae_malformed_ciphertext_raises_credential_error() -> None:
    blob = base64.b64encode(
        _AES_HEADER + os.urandom(32) + os.urandom(65)
    ).decode("ascii")

    with pytest.raises(TraeCredentialError):
        decrypt_icube_value(blob)


def test_trae_doctor_marks_device_check_unverified(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 9074, "message": "device check"})

    doctor = asyncio.run(_provider(handler).doctor(_config(path)))

    assert doctor.healthy is False
    assert doctor.details.get("verified") is False
    assert "设备校验" in doctor.message


def test_trae_registered_and_disabled_by_default(tmp_path: Path) -> None:
    provider = get_provider("trae")
    assert provider.display_name == "Trae 国内版"
    assert provider.fixed_business_day_start == "00:00"

    storage = Storage(tmp_path / "trae.db")
    cfg = storage.get_provider_config("trae")
    assert cfg.enabled is False
    assert cfg.business_day_start == "00:00"


def test_trae_unconfirmed_claim_is_soft_success(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    status_calls = 0
    claim_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls, claim_calls
        if request.url.path.endswith("/checkin_credits/status"):
            status_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "enable": True, "checked_in": False, "credits": 100},
            )
        if request.url.path.endswith("/checkin_credits/claim"):
            claim_calls += 1
            return httpx.Response(
                200, json={"code": 0, "data": {"points": 100}}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = TraeProvider(
        transport=httpx.MockTransport(handler),
        claim_verify_attempts=3,
        claim_verify_delay_seconds=0,
    )
    result = asyncio.run(provider.checkin(_config(path), BIZ_DATE))

    assert result.status == CheckinStatus.SUCCESS
    assert result.raw_error_type == "TraeClaimUnconfirmed"
    assert "状态暂未刷新" in result.message
    assert claim_calls == 1
    assert status_calls == 4


def test_trae_claim_verification_retries_until_checked(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.url.path.endswith("/checkin_credits/status"):
            status_calls += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "enable": True,
                    "checked_in": status_calls >= 3,
                    "credits": 100,
                },
            )
        if request.url.path.endswith("/checkin_credits/claim"):
            return httpx.Response(
                200, json={"code": 0, "data": {"points": 100}}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = TraeProvider(
        transport=httpx.MockTransport(handler),
        claim_verify_attempts=3,
        claim_verify_delay_seconds=0,
    )
    result = asyncio.run(provider.checkin(_config(path), BIZ_DATE))

    assert result.status == CheckinStatus.SUCCESS
    assert result.raw_error_type is None
    assert status_calls == 3


def test_trae_claim_verification_unavailable_is_soft_success(
    tmp_path: Path,
) -> None:
    path = _write_storage(tmp_path)
    post_claim = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_claim
        if request.url.path.endswith("/checkin_credits/status"):
            if post_claim:
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={"code": 0, "enable": True, "checked_in": False, "credits": 100},
            )
        if request.url.path.endswith("/checkin_credits/claim"):
            post_claim = True
            return httpx.Response(
                200, json={"code": 0, "data": {"points": 100}}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    result = asyncio.run(_provider(handler).checkin(_config(path), BIZ_DATE))

    assert result.status == CheckinStatus.SUCCESS
    assert result.raw_error_type == "TraeClaimUnconfirmed"
    assert "二次确认暂不可用" in result.message


def test_trae_disabled_auto_refresh_does_not_call_api(tmp_path: Path) -> None:
    path = _write_storage(
        tmp_path, access_expires=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        return httpx.Response(500)

    result = asyncio.run(
        _provider(handler).checkin(
            _config(path, allow_token_refresh=False), BIZ_DATE
        )
    )

    assert result.status == CheckinStatus.AUTH_EXPIRED
    assert result.raw_error_type == "TraeAuthExpired"
    assert called == []


def test_trae_doctor_reports_disabled_auto_refresh(tmp_path: Path) -> None:
    path = _write_storage(
        tmp_path, access_expires=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    doctor = asyncio.run(
        _provider(lambda request: httpx.Response(500)).doctor(
            _config(path, allow_token_refresh=False)
        )
    )

    assert doctor.healthy is False
    assert "自动续期已关闭" in doctor.message
    assert doctor.details.get("auto_refresh") is False


def test_trae_cache_device_mismatch_is_ignored(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    save_secure_json(
        get_trae_auth_path(),
        {
            "access_token": "cache-token",
            "refresh_token": "cache-refresh",
            "access_expires_at": (
                datetime.now(timezone.utc) + timedelta(days=20)
            ).isoformat(),
            "user_id": "user-123",
            "host": "https://api.trae.cn",
            "device_id": "9999999999999999",
        },
    )

    creds = load_trae_credentials(_config(path))

    assert creds.access_token == "test-access-token"
    assert creds.device_id == DEVICE_ID


def test_trae_cache_req_source_mismatch_is_ignored(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    save_secure_json(
        get_trae_auth_path(),
        {
            "access_token": "cache-token",
            "refresh_token": "cache-refresh",
            "access_expires_at": (
                datetime.now(timezone.utc) + timedelta(days=20)
            ).isoformat(),
            "user_id": "user-123",
            "host": "https://api.trae.cn",
            "device_id": DEVICE_ID,
            "req_source": 2,
        },
    )

    creds = load_trae_credentials(_config(path))

    assert creds.access_token == "test-access-token"
    assert creds.req_source == 1


def test_trae_legacy_cache_keeps_solo_req_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdata = tmp_path / "appdata"
    _write_storage_file(
        appdata / "TRAE SOLO CN" / "User" / "globalStorage" / "storage.json"
    )
    monkeypatch.setenv("APPDATA", str(appdata))
    save_secure_json(
        get_trae_auth_path(),
        {
            "access_token": "cache-token",
            "refresh_token": "cache-refresh",
            "access_expires_at": (
                datetime.now(timezone.utc) + timedelta(days=20)
            ).isoformat(),
            "user_id": "user-123",
            "host": "https://api.trae.cn",
            "device_id": DEVICE_ID,
        },
    )

    creds = load_trae_credentials(_config())

    assert creds.access_token == "cache-token"
    assert creds.req_source == 2


def test_trae_custom_path_does_not_fall_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdata = tmp_path / "appdata"
    default = appdata / "Trae CN" / "User" / "globalStorage" / "storage.json"
    _write_storage_file(default)
    monkeypatch.setenv("APPDATA", str(appdata))

    custom = tmp_path / "custom" / "storage.json"
    custom.parent.mkdir(parents=True)
    custom.write_text(
        json.dumps({"telemetry.machineId": "machine-1"}), encoding="utf-8"
    )

    with pytest.raises(TraeCredentialError, match="未登录"):
        load_trae_credentials(_config(custom))


def test_trae_custom_missing_path_does_not_use_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdata = tmp_path / "appdata"
    _write_storage_file(
        appdata / "Trae CN" / "User" / "globalStorage" / "storage.json"
    )
    monkeypatch.setenv("APPDATA", str(appdata))
    save_secure_json(
        get_trae_auth_path(),
        {
            "access_token": "cache-token",
            "refresh_token": "cache-refresh",
            "access_expires_at": (
                datetime.now(timezone.utc) + timedelta(days=20)
            ).isoformat(),
            "user_id": "user-123",
            "host": "https://api.trae.cn",
            "device_id": DEVICE_ID,
        },
    )

    missing = tmp_path / "missing" / "storage.json"
    with pytest.raises(TraeCredentialError, match="路径不存在"):
        load_trae_credentials(_config(missing))


def test_trae_solo_word_in_parent_is_not_solo_client(tmp_path: Path) -> None:
    path = (
        tmp_path
        / "solo_user"
        / "Trae CN"
        / "User"
        / "globalStorage"
        / "storage.json"
    )
    _write_storage_file(path)

    creds = load_trae_credentials(_config(path))

    assert creds.req_source == 1


def test_trae_refresh_failure_falls_back_to_client(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    save_secure_json(
        get_trae_auth_path(),
        {
            "access_token": "cache-token",
            "refresh_token": "cache-refresh",
            "access_expires_at": (
                datetime.now(timezone.utc) + timedelta(days=20)
            ).isoformat(),
            "user_id": "user-123",
            "host": "https://api.trae.cn",
            "device_id": DEVICE_ID,
        },
    )
    refresh_calls = 0
    client_status_calls = 0
    claim_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls, client_status_calls, claim_calls
        auth = request.headers.get("authorization", "")
        if request.url.path.endswith("/oauth/ExchangeToken"):
            refresh_calls += 1
            return httpx.Response(400, json={"code": 20101})
        if request.url.path.endswith("/checkin_credits/status"):
            if "cache-token" in auth:
                return httpx.Response(401, json={"code": 1002})
            client_status_calls += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "enable": True,
                    "checked_in": client_status_calls >= 2,
                    "credits": 100,
                },
            )
        if request.url.path.endswith("/checkin_credits/claim"):
            claim_calls += 1
            return httpx.Response(
                200, json={"code": 0, "data": {"points": 100}}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = TraeProvider(
        transport=httpx.MockTransport(handler),
        claim_verify_delay_seconds=0,
    )
    result = asyncio.run(provider.checkin(_config(path), BIZ_DATE))

    assert result.status == CheckinStatus.SUCCESS
    assert refresh_calls > 0
    assert claim_calls == 1
    cached = load_trae_credentials(_config(path), use_cache=True)
    assert cached.access_token == "test-access-token"


def test_trae_prefers_valid_storage_and_uses_solo_req_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdata = tmp_path / "appdata"
    logged_out = appdata / "Trae CN" / "User" / "globalStorage" / "storage.json"
    logged_out.parent.mkdir(parents=True)
    logged_out.write_text(
        json.dumps({"telemetry.machineId": "machine-1"}), encoding="utf-8"
    )
    solo = appdata / "TRAE SOLO CN" / "User" / "globalStorage" / "storage.json"
    _write_storage_file(solo)
    monkeypatch.setenv("APPDATA", str(appdata))

    config = _config()
    creds = load_trae_credentials(config)
    assert creds.source_path and "TRAE SOLO CN" in creds.source_path
    assert creds.req_source == 2

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"code": 0, "enable": True, "checked_in": True, "credits": 100},
        )

    result = asyncio.run(_provider(handler).checkin(config, BIZ_DATE))

    assert result.status == CheckinStatus.ALREADY_DONE
    assert bodies and bodies[0]["req_source"] == 2


def test_trae_picks_device_with_keys(tmp_path: Path) -> None:
    payload = _storage_payload()
    payload.pop(f"{DEVICE_KEY_PREFIX}{DEVICE_ID}")
    first = "1111111111111111"
    second = "2222222222222222"
    payload[f"{DEVICE_KEY_PREFIX}{first}"] = _encrypt_value({})
    private_pem, public_pem = _device_keys()
    payload[f"{DEVICE_KEY_PREFIX}{second}"] = _encrypt_value(
        {"privateKeyPEM": private_pem, "publicKeyPEM": public_pem}
    )
    path = tmp_path / "storage.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    creds = load_trae_credentials(_config(path))

    assert creds.device_id == second
    assert creds.refreshable is True


def test_trae_doctor_handles_bad_custom_path() -> None:
    config = _config(auth_file_path="bad\x00path")
    doctor = asyncio.run(TraeProvider().doctor(config))

    assert doctor.healthy is False
    assert "路径" in doctor.message


def test_trae_response_parsing_prefers_root() -> None:
    assert _parse_business_code({"code": 0, "data": {"code": 4001}}) == 0

    info = _StatusInfo(
        {
            "enable": True,
            "checked_in": False,
            "data": {"enable": False, "checked_in": True},
        }
    )
    assert info.enable is True
    assert info.checked_in is False


def test_trae_detect_client_exe_skips_registry_when_custom_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from geteggs.providers import trae as trae_mod

    exe = tmp_path / "Trae CN.exe"
    exe.write_bytes(b"MZ")
    called = {"registry": False}

    def fail_registry():
        called["registry"] = True
        return []

    monkeypatch.setattr(trae_mod, "_registry_install_locations", fail_registry)
    found = TraeProvider().detect_client_exe(_config(client_exe_path=str(exe)))
    assert found == str(exe.resolve())
    assert called["registry"] is False


def test_trae_bad_device_key_during_refresh_returns_auth_expired(
    tmp_path: Path,
) -> None:
    payload = _storage_payload(
        access_expires=datetime.now(timezone.utc) - timedelta(hours=1),
        include_keys=False,
    )
    payload[f"{DEVICE_KEY_PREFIX}{DEVICE_ID}"] = _encrypt_value(
        {"privateKeyPEM": "NOT-A-VALID-PEM", "publicKeyPEM": "NOT-A-VALID-PEM"}
    )
    path = tmp_path / "bad_key_storage.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"should not call network on bad PEM: {request.url}")

    result = asyncio.run(_provider(handler).checkin(_config(path), BIZ_DATE))
    assert result.status == CheckinStatus.AUTH_EXPIRED


def test_trae_decrypt_rejects_corrupt_padding_and_sign_validates_curve() -> None:
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from geteggs.providers.trae_auth import decrypt_icube_value, sign_ecdsa_p256

    # Flipping last byte breaks PKCS#7 padding -> must raise ValueError
    raw_enc = _encrypt_value({"ok": True})
    raw_bytes = bytearray(base64.b64decode(raw_enc))
    raw_bytes[-1] ^= 0xFF
    corrupted = base64.b64encode(bytes(raw_bytes)).decode("ascii")
    with pytest.raises(ValueError):
        decrypt_icube_value(corrupted)

    # Empty key or non-P-256 curve must raise ValueError
    with pytest.raises(ValueError):
        sign_ecdsa_p256("", "payload")

    p384_key = ec.generate_private_key(ec.SECP384R1())
    p384_pem = p384_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    with pytest.raises(ValueError):
        sign_ecdsa_p256(p384_pem, "payload")


def test_trae_merge_cache_ignores_expired_cached_token(tmp_path: Path) -> None:
    path = _write_storage(tmp_path)
    save_secure_json(
        get_trae_auth_path(),
        {
            "access_token": "expired-cache-token",
            "refresh_token": "expired-cache-refresh",
            "access_expires_at": (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat(),
            "user_id": "user-123",
            "host": "https://api.trae.cn",
            "device_id": DEVICE_ID,
        },
    )
    creds = load_trae_credentials(_config(path), use_cache=True)
    assert creds.access_token == "test-access-token"


def test_trae_verify_retries_transient_error(
    tmp_path: Path,
) -> None:
    path = _write_storage(tmp_path)
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.url.path.endswith("/checkin_credits/status"):
            status_calls += 1
            if status_calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "enable": True,
                        "checked_in": False,
                        "credits": 100,
                    },
                )
            if status_calls == 2:
                # Transient server error on first verification attempt
                return httpx.Response(503, json={"code": 503})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "enable": True,
                    "checked_in": True,
                    "credits": 100,
                },
            )
        if request.url.path.endswith("/checkin_credits/claim"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"points": 250}},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    result = asyncio.run(_provider(handler).checkin(_config(path), BIZ_DATE))
    assert result.status == CheckinStatus.SUCCESS
    assert result.raw_error_type is None
    assert status_calls == 3


def test_trae_merge_cache_keeps_newer_expired_rotated_refresh_token() -> None:
    from geteggs.providers.trae_auth import TraeCredentials, _merge_cache

    now = datetime.now(timezone.utc)
    creds = TraeCredentials(
        access_token="old-storage-access",
        refresh_token="revoked-storage-refresh",
        access_expires_at=(now - timedelta(days=5)).isoformat(),
        user_id="user-123",
        device_id=DEVICE_ID,
        req_source=1,
    )
    cache = {
        "access_token": "rotated-access-expired-today",
        "refresh_token": "valid-rotated-refresh",
        "access_expires_at": (now - timedelta(hours=1)).isoformat(),
        "user_id": "user-123",
        "device_id": DEVICE_ID,
        "req_source": 1,
    }
    merged = _merge_cache(creds, cache)
    assert merged.refresh_token == "valid-rotated-refresh"
    assert merged.access_token == "rotated-access-expired-today"


def test_trae_oversized_storage_error_preserved_and_cjk_display_re(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from geteggs.providers.trae import _TRAE_DISPLAY_RE
    from geteggs.providers.trae_auth import _load_from_storage_path

    assert _TRAE_DISPLAY_RE.search("Trae国内版") is not None
    assert _TRAE_DISPLAY_RE.search("字节Trae") is not None
    assert _TRAE_DISPLAY_RE.search("Portrait Editor") is None

    big = tmp_path / "storage.json"
    big.write_text("{}", encoding="utf-8")

    class _FakeStat:
        st_size = 10_000_001

    monkeypatch.setattr(Path, "stat", lambda self, *a, **kw: _FakeStat() if self == big else os.stat(self))
    with pytest.raises(TraeCredentialError, match="体积异常过大"):
        _load_from_storage_path(big)


def test_trae_refresh_retries_pascalcase_volcengine_error_code(tmp_path: Path) -> None:
    path = _write_storage(
        tmp_path,
        access_expires=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    exchange_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal exchange_calls
        if request.url.path.endswith("/oauth/ExchangeToken"):
            exchange_calls += 1
            if exchange_calls == 1:
                return httpx.Response(
                    400,
                    json={"ResponseMetadata": {"Error": {"CodeN": 10101, "Code": "InvalidClient"}}},
                )
            return httpx.Response(
                200,
                json={
                    "Result": {
                        "Token": "refreshed-access-tok",
                        "RefreshToken": "refreshed-refresh-tok",
                        "TokenExpireAt": int((datetime.now(timezone.utc) + timedelta(hours=12)).timestamp()),
                    }
                },
            )
        if request.url.path.endswith("/checkin_credits/status"):
            return httpx.Response(
                200,
                json={"code": 0, "enable": True, "checked_in": True, "credits": 100},
            )
        raise AssertionError(f"unexpected: {request.url}")

    result = asyncio.run(_provider(handler).checkin(_config(path), BIZ_DATE))
    assert exchange_calls == 2
    assert result.status == CheckinStatus.ALREADY_DONE
