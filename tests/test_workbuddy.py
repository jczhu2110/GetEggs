"""Unit tests for the WorkBuddy provider adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from geteggs.core.models import CheckinResult, CheckinStatus, ProviderConfig
from geteggs.providers.workbuddy import (
    CredentialFormatError,
    WorkBuddyCredentials,
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
        if request.url.path == "/v2/billing/meter/checkin-activity-status":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"checkedIn": True, "streakDays": 7},
                },
            )
        raise AssertionError(f"Unexpected call to {request.url.path}")

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.ALREADY_DONE
    assert called_paths == ["/v2/billing/meter/checkin-activity-status"]


def test_workbuddy_falls_back_to_browser_when_desktop_auth_fails(
    tmp_path: Path,
) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_desktop", "account": {"uid": "u1"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in (
            "/v2/billing/meter/checkin-status",
            "/v2/billing/meter/checkin-activity-status",
        ):
            return httpx.Response(401, json={})
        return httpx.Response(404, json={})

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    provider._load_browser_credentials = lambda config, info_meta=None: WorkBuddyCredentials(
        uid="u2", cookies={"sid": "cookie"}
    )
    called = {"value": False}

    async def fake_browser(
        config: ProviderConfig,
        biz_date: str,
        *,
        uid: Optional[str],
        base_url: str,
        dry_run: bool,
        log: Optional[object],
        started: float,
    ) -> CheckinResult:
        called["value"] = True
        assert uid == "u2"
        return CheckinResult(
            provider_id="workbuddy",
            display_name="WorkBuddy",
            status=CheckinStatus.SUCCESS,
            message="browser fallback ok",
            biz_date=biz_date,
        )

    provider._checkin_via_browser = fake_browser
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-19"))

    assert called["value"] is True
    assert res.status == CheckinStatus.SUCCESS
    assert res.message == "browser fallback ok"


def test_workbuddy_falls_back_to_browser_when_checkin_post_is_unauthorized(
    tmp_path: Path,
) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_desktop", "account": {"uid": "u1"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in (
            "/v2/billing/meter/checkin-status",
            "/v2/billing/meter/checkin-activity-status",
        ):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"checkedIn": False, "streakDays": 5}},
            )
        if request.url.path == "/v2/billing/meter/daily-checkin":
            return httpx.Response(401, json={})
        return httpx.Response(404, json={})

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    provider._load_browser_credentials = lambda config, info_meta=None: WorkBuddyCredentials(
        uid="u2", cookies={"sid": "cookie"}
    )
    called = {"value": False}

    async def fake_browser(
        config: ProviderConfig,
        biz_date: str,
        *,
        uid: Optional[str],
        base_url: str,
        dry_run: bool,
        log: Optional[object],
        started: float,
    ) -> CheckinResult:
        called["value"] = True
        assert uid == "u2"
        return CheckinResult(
            provider_id="workbuddy",
            display_name="WorkBuddy",
            status=CheckinStatus.SUCCESS,
            message="browser fallback ok",
            biz_date=biz_date,
        )

    provider._checkin_via_browser = fake_browser
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-19"))

    assert called["value"] is True
    assert res.status == CheckinStatus.SUCCESS


def test_workbuddy_treats_claimed_message_as_already_done(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"checkedIn": False, "streakDays": 5}},
            )
        if request.url.path == "/v2/billing/meter/daily-checkin":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "今日奖励已领取",
                    "data": {},
                },
            )
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={"auth_file_path": str(auth_file)},
    )
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.ALREADY_DONE


def test_workbuddy_reward_wins_over_already_like_message(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"checkedIn": False, "streakDays": 5}},
            )
        if request.url.path == "/v2/billing/meter/daily-checkin":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "签到成功，已领取 100 积分",
                    "data": {"rewardCredits": 100},
                },
            )
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={"auth_file_path": str(auth_file)},
    )
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.SUCCESS


def test_workbuddy_success_wording_without_reward_stays_success(
    tmp_path: Path,
) -> None:
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
                        "streakDays": 6 if state["checked"] else 5,
                    },
                },
            )
        if request.url.path == "/v2/billing/meter/daily-checkin":
            state["checked"] = True
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "签到成功，已领取 100 积分",
                    "data": {},
                },
            )
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.SUCCESS
    assert "签到成功" in res.message


def test_workbuddy_checkin_success(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.url.path == "/v2/billing/meter/checkin-status":
            status_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "data": {"checkedIn": False}},
            )
        if request.url.path == "/v2/billing/meter/daily-checkin":
            return httpx.Response(200, json={"code": 0, "data": {"rewardCredits": 100}})
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.SUCCESS
    assert status_calls == 1
    assert "已连续" not in res.message
    assert "积分" not in res.message


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
            return httpx.Response(200, json={"code": 0, "data": {"checkedIn": False}})
        if request.url.path == "/v2/billing/meter/daily-checkin":
            assert request.headers.get("Authorization") == "Bearer new_tok"
            return httpx.Response(200, json={"code": 0, "data": {"rewardCredits": 50}})
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={"auth_file_path": str(auth_file)},
    )
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == CheckinStatus.SUCCESS


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


def test_workbuddy_real_run_unknown_state_refuses_checkin(tmp_path: Path) -> None:
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
    res = asyncio.run(provider.checkin(cfg, "2026-11-16", dry_run=False))

    assert res.status == CheckinStatus.SCHEMA_CHANGED
    assert res.raw_error_type == "UnknownCheckinState"
    assert "/v2/billing/meter/daily-checkin" not in called_paths


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (404, {"message": "Not Found"}, CheckinStatus.SCHEMA_CHANGED),
        (429, {"error": "rate limit"}, CheckinStatus.FAILED),
        (400, {"error": "bad request"}, CheckinStatus.FAILED),
        (200, {}, CheckinStatus.SCHEMA_CHANGED),
    ],
)
def test_workbuddy_does_not_misclassify_4xx_or_empty_200_as_success(
    tmp_path: Path, status_code: int, body: dict, expected: CheckinStatus
) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(200, json={"code": 0, "data": {"checkedIn": False}})
        if request.url.path == "/v2/billing/meter/daily-checkin":
            return httpx.Response(status_code, json=body)
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))

    assert res.status == expected


def test_workbuddy_already_hint_with_zero_reward_and_parse_expiry_edges(
    tmp_path: Path,
) -> None:
    from geteggs.providers.workbuddy import _parse_expiry

    assert _parse_expiry(True) == (None, False)
    assert _parse_expiry(1e20) == (None, False)

    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(200, json={"code": 0, "data": {"checkedIn": False}})
        if request.url.path == "/v2/billing/meter/daily-checkin":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "今日已签到",
                    "data": {"rewardCredits": 0},
                },
            )
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))
    assert res.status == CheckinStatus.ALREADY_DONE


def test_workbuddy_desktop_fallback_uses_browser_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {
            "accessToken": "desktop_expired_tok",
            "domain": "https://www.codebuddy.cn",
            "account": {"uid": "u_desktop"},
        },
    )
    browser_state = tmp_path / "browser-state.json"
    browser_state.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "wb_sess",
                        "value": "cookie_val",
                        "domain": ".workbuddy.cn",
                        "path": "/",
                    }
                ],
                "origins": [
                    {
                        "origin": "https://copilot.tencent.com",
                        "localStorage": [
                            {"name": "uid", "value": "u_browser"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    async def fake_browser_checkin(state_path, *, base_url, uid, dry_run, log):
        captured["base_url"] = base_url
        captured["uid"] = uid
        return {
            "authenticated": True,
            "statusRes": {
                "status": 200,
                "data": {"code": 0, "data": {"checkedIn": True}},
            },
        }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    provider = WorkBuddyProvider(
        transport=httpx.MockTransport(handler),
        browser_checkin_fn=fake_browser_checkin,
    )
    cfg = ProviderConfig(
        provider_id="workbuddy",
        extra={
            "auth_file_path": str(auth_file),
            "browser_state_path": str(browser_state),
        },
    )
    res = asyncio.run(provider.checkin(cfg, "2026-11-16"))
    assert res.status == CheckinStatus.ALREADY_DONE
    assert captured["base_url"] == "https://copilot.tencent.com"


@pytest.mark.parametrize("dry_run", [False, True])
def test_workbuddy_inactive_activity_returns_unavailable_without_submitting(
    tmp_path: Path, dry_run: bool
) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )
    called_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called_paths.append(request.url.path)
        if request.url.path in (
            "/v2/billing/meter/checkin-activity-status",
            "/v2/billing/meter/checkin-status",
        ):
            return httpx.Response(
                200,
                json={"code": 0, "msg": "OK", "data": {"active": False, "today_checked_in": False}},
            )
        raise AssertionError(f"Should not call {request.url.path} when activity is inactive")

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-26", dry_run=dry_run))

    assert res.status == CheckinStatus.UNAVAILABLE
    assert res.raw_error_type == "ActivityInactive"
    assert "没有可用" in res.message
    assert called_paths == [
        "/v2/billing/meter/checkin-activity-status",
        "/v2/billing/meter/checkin-status",
    ]


def test_workbuddy_prefers_activity_status_over_inactive_legacy_status(
    tmp_path: Path,
) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )
    called_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called_paths.append(request.url.path)
        if request.url.path == "/v2/billing/meter/checkin-activity-status":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "OK", "data": {"active": True, "today_checked_in": True}},
            )
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "OK", "data": {"active": False, "today_checked_in": False}},
            )
        raise AssertionError(f"Unexpected call to {request.url.path}")

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-27"))

    assert res.status == CheckinStatus.ALREADY_DONE
    assert called_paths == ["/v2/billing/meter/checkin-activity-status"]


def test_workbuddy_falls_back_when_first_status_endpoint_is_inactive(
    tmp_path: Path,
) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )
    called_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called_paths.append(request.url.path)
        if request.url.path == "/v2/billing/meter/checkin-activity-status":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "OK", "data": {"active": False, "today_checked_in": False}},
            )
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "OK", "data": {"active": True, "today_checked_in": False}},
            )
        if request.url.path == "/v2/billing/meter/daily-checkin":
            return httpx.Response(200, json={"code": 0, "data": {"rewardCredits": 100}})
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-27"))

    assert res.status == CheckinStatus.SUCCESS
    assert called_paths == [
        "/v2/billing/meter/checkin-activity-status",
        "/v2/billing/meter/checkin-status",
        "/v2/billing/meter/daily-checkin",
    ]


def test_workbuddy_http_400_includes_server_error_detail(tmp_path: Path) -> None:
    auth_file = _write_info(
        tmp_path / "workbuddy-desktop.info",
        {"accessToken": "tok_ok", "account": {"uid": "u1"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(200, json={"code": 0, "data": {"active": True, "checkedIn": False}})
        if request.url.path == "/v2/billing/meter/daily-checkin":
            return httpx.Response(400, json={"code": 10002, "msg": "签到活动未开启"})
        return httpx.Response(404)

    provider = WorkBuddyProvider(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(provider_id="workbuddy", extra={"auth_file_path": str(auth_file)})
    res = asyncio.run(provider.checkin(cfg, "2026-11-26"))

    assert res.status == CheckinStatus.FAILED
    assert res.raw_error_type == "HTTP_400"
    assert "code=10002" in res.message
    assert "签到活动未开启" in res.message
