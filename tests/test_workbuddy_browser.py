"""Tests for WorkBuddy Option B controlled browser session fallback."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from geteggs.core.models import CheckinStatus, ProviderConfig
from geteggs.core.secure_store import load_secure_json, save_secure_json
from geteggs.providers.workbuddy import WorkBuddyProvider
from geteggs.providers.workbuddy_browser import _is_portal_url, parse_storage_state


def _write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_secure_store_roundtrip_and_plaintext_compat(tmp_path: Path) -> None:
    data = {"cookies": [{"name": "session", "value": "abc"}]}
    encrypted_path = tmp_path / "encrypted.json"
    save_secure_json(encrypted_path, data)
    assert load_secure_json(encrypted_path) == data
    if os.name == "nt":
        assert encrypted_path.read_bytes().startswith(b"GETEGGS1\n")

    plaintext_path = _write_json(tmp_path / "plaintext.json", data)
    assert load_secure_json(plaintext_path) == data


def test_is_portal_url_accepts_current_portal_domains() -> None:
    assert _is_portal_url("https://www.workbuddy.cn/profile/usage")
    assert _is_portal_url("https://www.codebuddy.cn/profile/usage")
    assert _is_portal_url("https://copilot.tencent.com/profile/usage")
    assert not _is_portal_url("https://www.workbuddy.cn/login")
    assert not _is_portal_url("https://www.codebuddy.cn/signin?from=profile")
    assert not _is_portal_url("https://example.com/profile/usage")
    assert not _is_portal_url("http://www.codebuddy.cn/profile/usage")
    assert not _is_portal_url("https://codebuddy.cn.evil.com/profile/usage")
    assert not _is_portal_url(
        "https://open.weixin.qq.com/connect/qrconnect?redirect_uri=https://www.codebuddy.cn/profile/usage"
    )


def test_parse_storage_state_filters_untrusted_origins_and_expired_cookies(
    tmp_path: Path,
) -> None:
    state_file = _write_json(
        tmp_path / "strict_state.json",
        {
            "cookies": [
                {"name": "valid", "value": "ok", "domain": ".codebuddy.cn", "expires": 4102444800},
                {"name": "expired", "value": "old", "domain": ".codebuddy.cn", "expires": 100},
                {"name": "sso_broad", "value": "nope", "domain": ".tencent.com"},
                {"name": "spoof", "value": "nope", "domain": "evil-workbuddy.com"},
            ],
            "origins": [
                {
                    "origin": "https://evil.example",
                    "localStorage": [{"name": "accessToken", "value": "evil_tok"}],
                },
                {
                    "origin": "https://www.codebuddy.cn",
                    "localStorage": [{"name": "uid", "value": "u_portal"}],
                },
            ],
        },
    )
    parsed = parse_storage_state(state_file)
    assert parsed is not None
    assert parsed["cookies"] == {"valid": "ok"}
    assert len(parsed["cookie_entries"]) == 1
    assert parsed["cookie_entries"][0]["domain"] == ".codebuddy.cn"
    assert parsed["access_token"] is None


def test_authorize_browser_uses_desktop_metadata_domain(tmp_path: Path) -> None:
    auth_file = _write_json(
        tmp_path / "workbuddy-desktop.info",
        {"encrypted": True, "ciphertext": "opaque", "domain": "www.workbuddy.cn"},
    )
    captured: dict[str, str] = {}

    async def fake_auth(state_path: Path, *, login_url: str) -> tuple[bool, str]:
        captured["login_url"] = login_url
        return True, "ok"

    provider = WorkBuddyProvider(browser_auth_fn=fake_auth)
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={
            "auth_file_path": str(auth_file),
            "browser_state_path": str(tmp_path / "state.json"),
        },
    )
    ok, _ = asyncio.run(provider.authorize_browser(cfg))
    assert ok is True
    assert captured["login_url"] == "https://www.workbuddy.cn/profile/usage"

    cfg.extra["base_url"] = "https://copilot.tencent.com"
    ok, _ = asyncio.run(provider.authorize_browser(cfg))
    assert ok is True
    assert captured["login_url"] == "https://copilot.tencent.com/profile/usage"


def test_parse_storage_state_reads_encrypted_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    save_secure_json(
        state_path,
        {
            "cookies": [{"name": "session", "value": "cookie_abc", "domain": ".codebuddy.cn"}],
            "origins": [
                {
                    "origin": "https://www.codebuddy.cn",
                    "localStorage": [
                        {
                            "name": "user_auth",
                            "value": json.dumps(
                                {"accessToken": "encrypted_tok", "uid": "uid_123"}
                            ),
                        }
                    ],
                }
            ],
        },
    )
    parsed = parse_storage_state(state_path)
    assert parsed is not None
    assert parsed["access_token"] == "encrypted_tok"
    assert parsed["uid"] == "uid_123"
    assert parsed["cookies"] == {"session": "cookie_abc"}


def test_parse_storage_state_extracts_nested_local_storage_and_cookies(tmp_path: Path) -> None:
    state_file = _write_json(
        tmp_path / "state.json",
        {
            "cookies": [
                {"name": "session", "value": "cookie_abc", "domain": ".codebuddy.cn"},
                {"name": "other", "value": "skip", "domain": ".example.com"},
            ],
            "origins": [
                {
                    "origin": "https://www.codebuddy.cn",
                    "localStorage": [
                        {
                            "name": "user_auth",
                            "value": json.dumps(
                                {
                                    "accessToken": "ls_tok_123",
                                    "refreshToken": "ls_ref_456",
                                    "uid": "uid_7890",
                                }
                            ),
                        }
                    ],
                }
            ],
        },
    )
    parsed = parse_storage_state(state_file)
    assert parsed is not None
    assert parsed["access_token"] == "ls_tok_123"
    assert parsed["refresh_token"] == "ls_ref_456"
    assert parsed["uid"] == "uid_7890"
    assert parsed["cookies"] == {"session": "cookie_abc"}
    assert parsed["domain"] == "https://www.codebuddy.cn"


def test_browser_state_origin_wins_over_encrypted_desktop_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GETEGGS_WORKBUDDY_TOKEN", raising=False)
    auth_file = _write_json(
        tmp_path / "workbuddy-desktop.info",
        {
            "encrypted": True,
            "ciphertext": "opaque_blob",
            "domain": "www.workbuddy.cn",
        },
    )
    state_file = _write_json(
        tmp_path / "browser_state.json",
        {
            "cookies": [{"name": "session", "value": "cookie_val", "domain": ".codebuddy.cn"}],
            "origins": [{"origin": "https://www.codebuddy.cn", "localStorage": []}],
        },
    )
    base_urls: list[str] = []

    async def fake_browser(state_path: Path, **kwargs: Any) -> dict[str, Any]:
        base_urls.append(kwargs.get("base_url", ""))
        return {"authenticated": False, "reason": "stop after URL assertion"}

    provider = WorkBuddyProvider(browser_checkin_fn=fake_browser)
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={
            "auth_file_path": str(auth_file),
            "browser_state_path": str(state_file),
        },
    )
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))
    assert res.status == CheckinStatus.AUTH_EXPIRED
    assert base_urls == ["https://www.codebuddy.cn"]


def test_workbuddy_doctor_uses_browser_state_when_desktop_info_encrypted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GETEGGS_WORKBUDDY_TOKEN", raising=False)
    auth_file = _write_json(
        tmp_path / "workbuddy-desktop.info",
        {
            "auth": {
                "accessToken": {"$wbEncrypted": 1, "envelope": "enc"},
                "domain": "www.codebuddy.cn",
                "expiresAt": 4102444800000,
            },
            "account": {"uid": "371dd1b7-0000-0000-0000-000000000ba6"},
        },
    )
    state_file = _write_json(
        tmp_path / "browser_state.json",
        {
            "cookies": [{"name": "session", "value": "s_ok", "domain": ".codebuddy.cn"}],
            "origins": [{"origin": "https://www.codebuddy.cn", "localStorage": []}],
        },
    )

    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={
            "auth_file_path": str(auth_file),
            "browser_state_path": str(state_file),
        },
    )
    provider = WorkBuddyProvider()
    res = asyncio.run(provider.doctor(cfg))
    assert res.healthy is True
    assert res.account_label == "371***ba6"
    assert res.details["auth_mode"] == "browser_state"


def test_workbuddy_checkin_fast_path_from_storage_state_and_fallback_to_browser_on_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GETEGGS_WORKBUDDY_TOKEN", raising=False)
    auth_file = _write_json(
        tmp_path / "workbuddy-desktop.info",
        {
            "auth": {"$wbEncrypted": 1, "accessToken": {"$wbEncrypted": 1, "envelope": "enc"}},
            "account": {"uid": "u_test"},
        },
    )
    state_file = _write_json(
        tmp_path / "browser_state.json",
        {
            "cookies": [{"name": "session", "value": "cookie_val", "domain": ".codebuddy.cn"}],
            "origins": [
                {
                    "origin": "https://www.codebuddy.cn",
                    "localStorage": [{"name": "accessToken", "value": "stale_tok"}],
                }
            ],
        },
    )

    def expired_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401})

    browser_calls: list[dict[str, Any]] = []

    async def fake_browser_checkin(state_path: Path, **kwargs: Any) -> dict[str, Any]:
        browser_calls.append({"state_path": str(state_path), **kwargs})
        return {
            "authenticated": True,
            "active_url": "https://www.codebuddy.cn",
            "statusRes": {"status": 200, "data": {"code": 0, "data": {"checkedIn": False}}},
            "checkinRes": {"status": 200, "data": {"code": 0, "data": {"rewardCredits": 100}}},
        }

    provider = WorkBuddyProvider(
        transport=httpx.MockTransport(expired_handler),
        browser_checkin_fn=fake_browser_checkin,
    )
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={
            "auth_file_path": str(auth_file),
            "browser_state_path": str(state_file),
        },
    )
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))
    assert len(browser_calls) == 1
    assert res.status == CheckinStatus.SUCCESS


def test_workbuddy_browser_succeeds_when_reward_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GETEGGS_WORKBUDDY_TOKEN", raising=False)
    auth_file = _write_json(
        tmp_path / "workbuddy-desktop.info",
        {
            "auth": {"$wbEncrypted": 1, "accessToken": {"$wbEncrypted": 1, "envelope": "enc"}},
            "account": {"uid": "u_test"},
        },
    )
    state_file = _write_json(
        tmp_path / "browser_state.json",
        {
            "cookies": [{"name": "session", "value": "cookie_val", "domain": ".codebuddy.cn"}],
            "origins": [{"origin": "https://www.codebuddy.cn", "localStorage": []}],
        },
    )

    def expired_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401})

    async def fake_browser_checkin(state_path: Path, **kwargs: Any) -> dict[str, Any]:
        return {
            "authenticated": True,
            "active_url": "https://www.codebuddy.cn",
            "statusRes": {"status": 200, "data": {"code": 0, "data": {"checkedIn": False}}},
            "checkinRes": {"status": 200, "data": {"code": 0, "data": {}}},
        }

    provider = WorkBuddyProvider(
        transport=httpx.MockTransport(expired_handler),
        browser_checkin_fn=fake_browser_checkin,
    )
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={
            "auth_file_path": str(auth_file),
            "browser_state_path": str(state_file),
        },
    )
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.SUCCESS


def test_workbuddy_browser_checkin_returns_auth_expired_when_redirected_to_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GETEGGS_WORKBUDDY_TOKEN", raising=False)
    state_file = _write_json(
        tmp_path / "browser_state.json",
        {
            "cookies": [{"name": "session", "value": "cookie_only", "domain": ".codebuddy.cn"}],
            "origins": [{"origin": "https://www.codebuddy.cn", "localStorage": []}],
        },
    )

    async def expired_browser(state_path: Path, **kwargs: Any) -> dict[str, Any]:
        return {"authenticated": False, "reason": "受控浏览器登录态已过期"}

    provider = WorkBuddyProvider(browser_checkin_fn=expired_browser)
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={
            "auth_file_path": str(tmp_path / "missing.info"),
            "browser_state_path": str(state_file),
        },
    )
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))
    assert res.status == CheckinStatus.AUTH_EXPIRED


def test_workbuddy_browser_refuses_checkin_on_status_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GETEGGS_WORKBUDDY_TOKEN", raising=False)
    state_file = _write_json(
        tmp_path / "browser_state.json",
        {
            "cookies": [{"name": "session", "value": "cookie_only", "domain": ".codebuddy.cn"}],
            "origins": [{"origin": "https://www.codebuddy.cn", "localStorage": []}],
        },
    )

    async def error_browser(state_path: Path, **kwargs: Any) -> dict[str, Any]:
        return {
            "authenticated": True,
            "statusRes": {"status": 500, "data": None},
            "checkinRes": {"status": 200, "data": {"code": 0, "data": {"rewardCredits": 100}}},
        }

    provider = WorkBuddyProvider(browser_checkin_fn=error_browser)
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={
            "auth_file_path": str(tmp_path / "missing.info"),
            "browser_state_path": str(state_file),
        },
    )
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))
    assert res.status == CheckinStatus.FAILED
    assert res.raw_error_type == "HTTP_500"


def test_workbuddy_browser_dry_run_with_unknown_state_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GETEGGS_WORKBUDDY_TOKEN", raising=False)
    state_file = _write_json(
        tmp_path / "browser_state.json",
        {
            "cookies": [{"name": "session", "value": "cookie_only", "domain": ".codebuddy.cn"}],
            "origins": [{"origin": "https://www.codebuddy.cn", "localStorage": []}],
        },
    )

    async def unknown_state_browser(state_path: Path, **kwargs: Any) -> dict[str, Any]:
        return {
            "authenticated": True,
            "statusRes": {"status": 200, "data": {}},
            "checkinRes": None,
        }

    provider = WorkBuddyProvider(browser_checkin_fn=unknown_state_browser)
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={
            "auth_file_path": str(tmp_path / "missing.info"),
            "browser_state_path": str(state_file),
        },
    )
    res = asyncio.run(provider.checkin(cfg, "2026-11-16", dry_run=True))
    assert res.status == CheckinStatus.UNAVAILABLE
    assert res.raw_error_type == "UnknownCheckinState"
