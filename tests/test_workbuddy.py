"""Unit tests for the WorkBuddy provider adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from geteggs.core.models import CheckinStatus, ProviderConfig
from geteggs.providers.workbuddy import (
    CredentialFormatError,
    WorkBuddyProvider,
    parse_auth_file,
)


def _write_info(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_parse_auth_file_supports_top_level_and_nested(tmp_path: Path) -> None:
    p = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {
            "accessToken": "tok_abc123456",
            "refreshToken": "ref_xyz",
            "account": {"uid": "user_998877"},
            "domain": "copilot.tencent.com",
            "expiresAt": 4102444800000,
        },
    )
    creds = parse_auth_file(p)
    assert creds.access_token == "tok_abc123456"
    assert creds.refresh_token == "ref_xyz"
    assert creds.uid == "user_998877"
    assert creds.domain == "copilot.tencent.com"
    assert creds.is_expired is False


def test_parse_auth_file_rejects_encrypted_envelope(tmp_path: Path) -> None:
    p = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"encrypted": True, "ciphertext": "opaque_blob"},
    )
    with pytest.raises(CredentialFormatError, match="加密"):
        parse_auth_file(p)


def test_parse_auth_file_detects_wb_encrypted_and_keeps_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GETEGGS_WORKBUDDY_TOKEN", raising=False)
    p = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {
            "auth": {
                "accessToken": {"$wbEncrypted": 1, "envelope": "base64_envelope"},
                "domain": "www.codebuddy.cn",
                "expiresAt": 4102444800000,
            },
            "account": {"uid": "e3d28070-1234-5678-9999-abcdef123456"},
        },
    )
    with pytest.raises(CredentialFormatError, match=r"\$wbEncrypted") as exc_info:
        parse_auth_file(p)
    assert exc_info.value.account_label == "e3d***456"
    assert exc_info.value.domain == "www.codebuddy.cn"

    monkeypatch.setenv("GETEGGS_WORKBUDDY_TOKEN", "manual_bearer_tok")
    creds = parse_auth_file(p)
    assert creds.access_token == "manual_bearer_tok"
    assert creds.uid == "e3d28070-1234-5678-9999-abcdef123456"
    assert creds.domain == "www.codebuddy.cn"


def test_workbuddy_doctor_masks_uid_and_never_leaks_token(tmp_path: Path) -> None:
    secret_token = "super_secret_bearer_token_999999"
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {
            "accessToken": secret_token,
            "refreshToken": "refresh_secret_111",
            "account": {"uid": "wx_1234567890"},
            "expiresAt": 4102444800000,
        },
    )
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={"auth_file_path": str(auth_file)},
    )
    provider = WorkBuddyProvider()
    res = asyncio.run(provider.doctor(cfg))

    assert res.healthy is True
    assert res.auth_file_found is True
    assert res.account_label == "wx_***890"
    dumped = res.model_dump_json()
    assert secret_token not in dumped
    assert "refresh_secret_111" not in dumped


def test_workbuddy_checkin_idempotent_when_already_checked_in(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )
    called_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called_paths.append(request.url.path)
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"checkedIn": True, "balance": 1250, "streakDays": 7},
                },
            )
        raise AssertionError(f"Unexpected call to {request.url.path}")

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.ALREADY_DONE
    assert res.snapshot is not None
    assert res.snapshot.balance == 1250
    assert res.snapshot.streak_days == 7
    assert called_paths == ["/v2/billing/meter/checkin-status"]


def test_workbuddy_checkin_success_and_status_refresh(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )
    state = {"checked": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "checkedIn": state["checked"],
                        "balance": 1300 if state["checked"] else 1200,
                        "streakDays": 8 if state["checked"] else 7,
                    },
                },
            )
        if request.url.path == "/v2/billing/meter/daily-checkin":
            state["checked"] = True
            return httpx.Response(200, json={"code": 0, "data": {"rewardCredits": 100}})
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.SUCCESS
    assert res.snapshot is not None
    assert res.snapshot.delta == 100
    assert res.snapshot.balance == 1300
    assert res.snapshot.streak_days == 8


def test_workbuddy_refreshes_token_on_401(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "old_tok", "refreshToken": "ref_tok", "account": {"uid": "u1"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/plugin/auth/token/refresh":
            assert request.headers.get("X-Refresh-Token") == "ref_tok"
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "new_tok"}})
        if request.url.path == "/v2/billing/meter/checkin-status":
            if request.headers.get("Authorization") == "Bearer old_tok":
                return httpx.Response(401, json={"code": 401})
            assert request.headers.get("Authorization") == "Bearer new_tok"
            return httpx.Response(200, json={"code": 0, "data": {"checkedIn": False, "balance": 500}})
        if request.url.path == "/v2/billing/meter/daily-checkin":
            assert request.headers.get("Authorization") == "Bearer new_tok"
            return httpx.Response(200, json={"code": 0, "data": {"rewardCredits": 50, "balance": 550}})
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.SUCCESS
    assert res.snapshot is not None
    assert res.snapshot.delta == 50
    assert res.snapshot.balance == 550


def test_workbuddy_marks_needs_manual_on_captcha_response(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(200, json={"code": 0, "data": {"checkedIn": False}})
        if request.url.path == "/v2/billing/meter/daily-checkin":
            return httpx.Response(200, json={"code": 4003, "msg": "请完成安全验证后重试"})
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.NEEDS_MANUAL


def test_workbuddy_schema_changed_when_status_endpoints_404(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )
    called_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called_paths.append(request.url.path)
        return httpx.Response(404, json={"code": 404})

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.SCHEMA_CHANGED
    assert res.raw_error_type == "HTTP_404"
    assert "/v2/billing/meter/daily-checkin" not in called_paths


def test_workbuddy_does_not_submit_checkin_when_status_server_errors(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )
    called_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called_paths.append(request.url.path)
        return httpx.Response(503, json={"code": 503})

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.FAILED
    assert res.raw_error_type == "HTTP_503"
    assert "/v2/billing/meter/daily-checkin" not in called_paths


def test_workbuddy_parses_expiring_credits(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "checkedIn": True,
                        "balance": 900,
                        "expiringSoon": 30,
                        "expiringAt": 4102444800000,
                    },
                },
            )
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.ALREADY_DONE
    assert res.snapshot is not None
    assert res.snapshot.expiring_soon == 30
    assert res.snapshot.expiring_at is not None
    assert res.snapshot.expiring_at.startswith("2100-01-01")


def test_workbuddy_dry_run_unknown_state_is_unavailable(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )
    called_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called_paths.append(request.url.path)
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16", dry_run=True))

    assert res.status == CheckinStatus.UNAVAILABLE
    assert "/v2/billing/meter/daily-checkin" not in called_paths
