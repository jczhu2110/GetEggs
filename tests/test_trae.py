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

from geteggs.core.models import CheckinStatus, ProviderConfig
from geteggs.core.storage import Storage
from geteggs.providers.registry import get_provider
from geteggs.providers.trae import TraeProvider
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


def _write_storage(
    tmp_path: Path,
    *,
    access_expires: Optional[datetime] = None,
    include_keys: bool = True,
) -> Path:
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
    path = tmp_path / "storage.json"
    path.write_text(json.dumps(storage, ensure_ascii=False), encoding="utf-8")
    return path


def _config(path: Path) -> ProviderConfig:
    return ProviderConfig(
        provider_id="trae",
        extra={"auth_file_path": str(path), "app_version": "1.107.1"},
    )


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
    assert result.snapshot is not None
    assert result.snapshot.delta == 0
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
    assert result.snapshot is not None
    assert result.snapshot.delta == 100
    assert result.snapshot.expiring_soon == 100
    assert result.snapshot.expiring_at
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

    assert doctor.healthy is True
    assert doctor.details.get("verified") is False
    assert "无法在线校验" in doctor.message


def test_trae_registered_and_disabled_by_default(tmp_path: Path) -> None:
    provider = get_provider("trae")
    assert provider.display_name == "Trae 国内版"
    assert provider.fixed_business_day_start == "00:00"
    assert provider.supports_streak is False

    storage = Storage(tmp_path / "trae.db")
    cfg = storage.get_provider_config("trae")
    assert cfg.enabled is False
    assert cfg.business_day_start == "00:00"