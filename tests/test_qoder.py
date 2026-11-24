"""Unit tests for the Qoder CN PAT provider."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from geteggs.core.models import CheckinStatus, ProviderConfig
from geteggs.providers.qoder import QoderProvider

PAT = "pt-test-secret-token-123456"
BIZ_DATE = "2026-11-17"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path / "home"))


def _cfg(**extra: Any) -> ProviderConfig:
    return ProviderConfig(provider_id="qoder", enabled=True, extra=extra)


def _campaign(status: str = "CLAIMABLE", campaign_id: str = "camp-1") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "campaignId": campaign_id,
        "actionType": "CLAIM_BENEFIT",
        "claimStatus": status,
        "startAt": (now - timedelta(hours=1)).isoformat(),
        "endAt": (now + timedelta(hours=1)).isoformat(),
        "benefit": {"amount": 100},
    }


def _set_pat(monkeypatch: pytest.MonkeyPatch, value: str = PAT) -> None:
    monkeypatch.setenv("QODERCN_PERSONAL_ACCESS_TOKEN", value)
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GETEGGS_QODER_PAT_FILE", raising=False)


def _campaigns_response(campaigns: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"data": {"campaigns": campaigns}})


def test_qoder_reports_no_streak_support() -> None:
    assert QoderProvider().supports_streak is False


def test_qoder_already_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sash/api/v1/me/campaigns")
        assert request.url.params.get("clientType") == "10"
        assert request.headers.get("authorization") == f"Bearer {PAT}"
        return _campaigns_response([_campaign("CLAIMED")])

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.ALREADY_DONE
    assert result.snapshot is not None
    assert result.snapshot.delta == 0


def test_qoder_claims_daily_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)
    claimed = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _campaigns_response([_campaign("CLAIMABLE")])
        assert request.method == "POST"
        assert request.url.path.endswith("/sash/api/v1/me/campaigns/camp-1/claim")
        claimed["value"] = True
        return httpx.Response(200, json={"status": "CLAIMED"})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    cfg = _cfg()
    cfg.checkin_credits = 250
    result = asyncio.run(provider.checkin(cfg, BIZ_DATE))

    assert claimed["value"] is True
    assert result.status == CheckinStatus.SUCCESS
    assert result.snapshot is not None
    assert result.snapshot.delta == 100
    assert result.snapshot.expiring_soon == 100
    assert result.snapshot.expiring_at


def test_qoder_accepts_claim_status_field(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _campaigns_response([_campaign("CLAIMABLE")])
        assert request.method == "POST"
        return httpx.Response(200, json={"status": "OK", "claimStatus": "CLAIMED"})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.SUCCESS
    assert result.snapshot is not None
    assert result.snapshot.delta == 100


def test_qoder_uses_campaign_validity_days(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)
    campaign = _campaign("CLAIMABLE")
    campaign["benefit"] = {
        "amount": 100,
        "validity": {"mode": "RELATIVE_DAYS", "days": 7},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _campaigns_response([campaign])
        return httpx.Response(200, json={"status": "CLAIMED"})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.SUCCESS
    assert result.snapshot is not None
    assert result.snapshot.expiring_at is not None
    expiry = datetime.fromisoformat(result.snapshot.expiring_at)
    remaining = expiry - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < remaining <= timedelta(days=7, minutes=1)


def test_qoder_invalid_validity_days_falls_back_to_30() -> None:
    campaign = {"benefit": {"validity": {"mode": "RELATIVE_DAYS", "days": 0}}}
    assert QoderProvider._benefit_validity_days(campaign) == 30


def test_qoder_uses_configured_credits_when_benefit_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)
    campaign = _campaign("CLAIMABLE")
    campaign.pop("benefit")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _campaigns_response([campaign])
        return httpx.Response(200, json={"status": "CLAIMED"})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    cfg = _cfg()
    cfg.checkin_credits = 250
    result = asyncio.run(provider.checkin(cfg, BIZ_DATE))

    assert result.status == CheckinStatus.SUCCESS
    assert result.snapshot is not None
    assert result.snapshot.delta == 250


def test_qoder_clamps_excessive_benefit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)
    campaign = _campaign("CLAIMABLE")
    campaign["benefit"] = {"amount": 2_000_000}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _campaigns_response([campaign])
        return httpx.Response(200, json={"status": "CLAIMED"})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.SUCCESS
    assert result.snapshot is not None
    assert result.snapshot.delta == 1_000_000


def test_qoder_explicit_zero_benefit_stays_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)
    campaign = _campaign("CLAIMABLE")
    campaign["benefit"] = {"amount": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _campaigns_response([campaign])
        return httpx.Response(200, json={"status": "CLAIMED"})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.SUCCESS
    assert result.snapshot is not None
    assert result.snapshot.delta == 0


def test_qoder_dry_run_does_not_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)
    posted = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted["value"] = True
        return _campaigns_response([_campaign("CLAIMABLE")])

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE, dry_run=True))

    assert posted["value"] is False
    assert result.status == CheckinStatus.SUCCESS
    assert result.dry_run is True
    assert result.snapshot is None


def test_qoder_no_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return _campaigns_response([])

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.UNAVAILABLE


def test_qoder_missing_campaigns_field_is_schema_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.SCHEMA_CHANGED
    assert "campaigns" in result.message


def test_qoder_doctor_reports_schema_change(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.doctor(_cfg()))

    assert result.healthy is False
    assert result.details.get("verified") is True
    assert result.details.get("schema_ok") is False
    assert "campaigns" in result.message


def test_qoder_exchanges_pat_when_direct_call_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)
    exchange_called = {"value": False}
    logs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        if request.url.path == "/api/v1/jobToken/exchange":
            exchange_called["value"] = True
            assert auth == f"Bearer {PAT}"
            assert json.loads(request.content) == {"personal_token": PAT}
            return httpx.Response(
                200,
                json={
                    "token": "jt-short-token",
                    "refresh_token": "jrt-test",
                    "expires_in": 86400000,
                },
            )
        if request.url.path.endswith("/sash/api/v1/me/campaigns"):
            if auth == f"Bearer {PAT}":
                return httpx.Response(401, json={"message": "unauthorized"})
            assert auth == "Bearer jt-short-token"
            return _campaigns_response([_campaign("CLAIMED")])
        return httpx.Response(404, json={})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(
        provider.checkin(
            _cfg(),
            BIZ_DATE,
            log=lambda level, message: logs.append(message),
        )
    )

    assert exchange_called["value"] is True
    assert result.status == CheckinStatus.ALREADY_DONE
    assert PAT not in "\n".join(logs)


def test_qoder_auth_expired_when_exchange_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/jobToken/exchange":
            return httpx.Response(401, json={})
        return httpx.Response(401, json={})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.AUTH_EXPIRED


def test_qoder_server_error_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)
    claimed = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            claimed["value"] = True
        return httpx.Response(500, json={})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.FAILED
    assert claimed["value"] is False


def test_qoder_missing_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QODERCN_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GETEGGS_QODER_PAT_FILE", raising=False)

    provider = QoderProvider()
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.NEEDS_MANUAL
    assert "QODERCN_PERSONAL_ACCESS_TOKEN" in result.message


def test_qoder_doctor_verifies_pat_online(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)
    calls = {"exchange": 0, "claim": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/jobToken/exchange":
            calls["exchange"] += 1
            return httpx.Response(
                200, json={"token": "jt-doctor", "expires_in": 86400000}
            )
        if request.url.path.endswith("/claim"):
            calls["claim"] += 1
            return httpx.Response(200, json={"status": "CLAIMED"})
        if request.headers.get("authorization") == f"Bearer {PAT}":
            return httpx.Response(401, json={})
        return _campaigns_response([_campaign("CLAIMABLE")])

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.doctor(_cfg()))

    assert result.healthy is True
    assert result.details.get("verified") is True
    assert result.details.get("claimable") is True
    assert "在线校验通过" in result.message
    assert calls == {"exchange": 1, "claim": 0}


def test_qoder_doctor_reports_invalid_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.doctor(_cfg()))

    assert result.healthy is False
    assert result.details.get("verified") is False
    assert "在线校验失败" in result.message


def test_qoder_doctor_tolerates_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("", request=request)

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.doctor(_cfg()))

    assert result.healthy is True
    assert result.details.get("verified") is False
    assert "暂时无法在线校验" in result.message
    assert "ConnectError" in result.message
    assert not result.message.endswith("：")


def test_qoder_reuses_cached_job_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)
    stats = {"pat": 0, "token": 0, "exchange": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        if request.url.path == "/api/v1/jobToken/exchange":
            stats["exchange"] += 1
            return httpx.Response(
                200, json={"token": "jt-cached", "expires_in": 86400000}
            )
        if request.url.path.endswith("/sash/api/v1/me/campaigns"):
            if auth == f"Bearer {PAT}":
                stats["pat"] += 1
                return httpx.Response(401, json={})
            if auth == "Bearer jt-cached":
                stats["token"] += 1
                return _campaigns_response([_campaign("CLAIMED")])
        return httpx.Response(404, json={})

    provider = QoderProvider(transport=httpx.MockTransport(handler))

    assert (
        asyncio.run(provider.checkin(_cfg(), BIZ_DATE)).status
        == CheckinStatus.ALREADY_DONE
    )
    assert (
        asyncio.run(provider.checkin(_cfg(), BIZ_DATE)).status
        == CheckinStatus.ALREADY_DONE
    )
    doctor_result = asyncio.run(provider.doctor(_cfg()))

    assert doctor_result.details.get("verified") is True
    assert stats["exchange"] == 1
    assert stats["pat"] == 1
    assert stats["token"] == 3


def test_qoder_cached_token_ttl_normalizes_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        if request.url.path == "/api/v1/jobToken/exchange":
            return httpx.Response(
                200, json={"token": "jt-ms", "expires_in": 86400000}
            )
        if request.url.path.endswith("/sash/api/v1/me/campaigns"):
            if auth == f"Bearer {PAT}":
                return httpx.Response(401, json={})
            return _campaigns_response([_campaign("CLAIMED")])
        return httpx.Response(404, json={})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    cached = list(provider._token_cache.values())
    assert len(cached) == 1
    ttl = cached[0][1] - time.monotonic()
    assert 0 < ttl <= 86_400


def test_qoder_cached_token_ttl_prefers_expires_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        if request.url.path == "/api/v1/jobToken/exchange":
            return httpx.Response(
                200,
                json={
                    "token": "jt-exp",
                    "expires_in": 86400000,
                    "expires_at": expires_at,
                },
            )
        if request.url.path.endswith("/sash/api/v1/me/campaigns"):
            if auth == f"Bearer {PAT}":
                return httpx.Response(401, json={})
            return _campaigns_response([_campaign("CLAIMED")])
        return httpx.Response(404, json={})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    ttl = list(provider._token_cache.values())[0][1] - time.monotonic()
    assert 6_900 < ttl <= 7_200


def test_qoder_expired_job_token_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)
    expires_at = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        if request.url.path == "/api/v1/jobToken/exchange":
            return httpx.Response(
                200,
                json={
                    "token": "jt-expired",
                    "expires_in": 86400000,
                    "expires_at": expires_at,
                },
            )
        if request.url.path.endswith("/sash/api/v1/me/campaigns"):
            if auth == f"Bearer {PAT}":
                return httpx.Response(401, json={})
            return _campaigns_response([_campaign("CLAIMED")])
        return httpx.Response(404, json={})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert provider._token_cache == {}


def test_qoder_refreshes_cached_token_after_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)
    stats = {"exchange": 0, "token": 0}
    token_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        if request.url.path == "/api/v1/jobToken/exchange":
            stats["exchange"] += 1
            return httpx.Response(
                200, json={"token": "jt-cached", "expires_in": 86400000}
            )
        if request.url.path.endswith("/sash/api/v1/me/campaigns"):
            if auth == f"Bearer {PAT}":
                return httpx.Response(401, json={})
            if auth == "Bearer jt-cached":
                stats["token"] += 1
                token_calls["count"] += 1
                if token_calls["count"] == 2:
                    return httpx.Response(500, json={})
                return _campaigns_response([_campaign("CLAIMED")])
        return httpx.Response(404, json={})

    provider = QoderProvider(transport=httpx.MockTransport(handler))

    assert (
        asyncio.run(provider.checkin(_cfg(), BIZ_DATE)).status
        == CheckinStatus.ALREADY_DONE
    )
    assert (
        asyncio.run(provider.checkin(_cfg(), BIZ_DATE)).status
        == CheckinStatus.ALREADY_DONE
    )

    assert stats == {"exchange": 2, "token": 3}


def test_qoder_reads_pat_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QODERCN_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GETEGGS_QODER_PAT_FILE", raising=False)
    pat_file = tmp_path / "qoder_pat.txt"
    pat_file.write_text("QODERCN_PERSONAL_ACCESS_TOKEN=pt-file-token\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer pt-file-token"
        return _campaigns_response([_campaign("CLAIMED")])

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(
        provider.checkin(_cfg(auth_file_path=str(pat_file)), BIZ_DATE)
    )

    assert result.status == CheckinStatus.ALREADY_DONE


def test_qoder_reads_pat_from_secure_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QODERCN_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GETEGGS_QODER_PAT_FILE", raising=False)
    from geteggs.core.secure_store import save_secure_json

    save_secure_json(
        tmp_path / "home" / "qoder_pat.json", {"pat": "pt-web-token-123456"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer pt-web-token-123456"
        return _campaigns_response([_campaign("CLAIMED")])

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.ALREADY_DONE
    status = provider.pat_status(_cfg())
    assert status["configured"] is True
    assert "网页输入" in (status["source"] or "")
    assert "pt-web-token-123456" not in str(status)


def test_qoder_parses_epoch_campaign_timestamps() -> None:
    now = datetime.now(timezone.utc)
    seconds = int(now.timestamp())
    millis = seconds * 1000
    for value in (seconds, millis, str(seconds)):
        parsed = QoderProvider._parse_time(value)
        assert parsed is not None
        assert abs((parsed - now).total_seconds()) < 2


def test_qoder_doctor_does_not_leak_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return _campaigns_response([_campaign("CLAIMED")])

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.doctor(_cfg()))

    assert result.healthy is True
    assert result.auth_file_found is True
    assert PAT not in result.message
    assert PAT not in (result.auth_file_path or "")
    assert PAT not in json.dumps(result.model_dump(), ensure_ascii=False)


def test_qoder_claim_not_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _campaigns_response([_campaign("CLAIMABLE")])
        return httpx.Response(200, json={"status": "PENDING"})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.checkin(_cfg(), BIZ_DATE))

    assert result.status == CheckinStatus.FAILED


def test_qoder_claim_401_evicts_cached_job_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _campaigns_response([_campaign("CLAIMABLE")])
        return httpx.Response(401, json={"error": "expired"})

    provider = QoderProvider(transport=httpx.MockTransport(handler))
    cfg = _cfg()
    provider._remember_job_token(cfg, PAT, "stale-job-token", 3600)
    assert provider._cached_job_token(cfg, PAT) == "stale-job-token"

    result = asyncio.run(provider.checkin(cfg, BIZ_DATE))
    assert result.status == CheckinStatus.AUTH_EXPIRED
    assert provider._cached_job_token(cfg, PAT) is None


def test_qoder_parse_time_and_fixed_business_day_start() -> None:
    from datetime import datetime, timezone
    from geteggs.providers.base import parse_timestamp

    for bad in (True, False, "²", float("nan"), float("inf"), 1e20, -1e20):
        assert parse_timestamp(bad) is None
        assert QoderProvider._parse_time(bad) is None

    # Fixed 10:00 business day must override ProviderConfig.business_day_start="00:00"
    provider = QoderProvider()
    cfg = _cfg()
    cfg.business_day_start = "00:00"
    before_reset = datetime(2026, 11, 24, 0, 30, tzinfo=timezone.utc)  # 08:30 Asia/Shanghai
    after_reset = datetime(2026, 11, 24, 2, 30, tzinfo=timezone.utc)   # 10:30 Asia/Shanghai
    assert provider.business_date(cfg, "Asia/Shanghai", now=before_reset) == "2026-11-23"
    assert provider.business_date(cfg, "Asia/Shanghai", now=after_reset) == "2026-11-24"


def test_qoder_pat_env_file_ignores_unrelated_keys_and_handles_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QODERCN_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CUSTOM_QODER_PAT", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nFOO=bar\nexport CUSTOM_QODER_PAT=\"qdr_live_token_123456\"\n",
        encoding="utf-8",
    )
    cfg = _cfg(pat_env="CUSTOM_QODER_PAT", pat_file=str(env_file))
    provider = QoderProvider()
    pat, source = provider._resolve_pat(cfg)
    assert pat == "qdr_live_token_123456"
    assert source and str(env_file) in source


def test_qoder_429_preserves_cached_job_token_and_doctor_400_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pat(monkeypatch)

    def handler_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    provider = QoderProvider(transport=httpx.MockTransport(handler_429))
    cfg = _cfg()
    provider._remember_job_token(cfg, PAT, "keep-me-token", 3600)
    res = asyncio.run(provider.checkin(cfg, BIZ_DATE))
    assert res.status == CheckinStatus.FAILED
    assert provider._cached_job_token(cfg, PAT) == "keep-me-token"

    def handler_400(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    provider_400 = QoderProvider(transport=httpx.MockTransport(handler_400))
    doc = asyncio.run(provider_400.doctor(_cfg()))
    assert doc.healthy is False
    assert "400" in doc.message


def test_base_build_http_client_falls_back_on_invalid_proxy_and_huge_int_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from geteggs.providers.base import BaseProvider, parse_timestamp

    assert parse_timestamp(10**400) is None

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:abc")
    client = BaseProvider.build_http_client()
    asyncio.run(client.aclose())


def test_qoder_pat_file_accepts_raw_token_with_equals_padding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QODERCN_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN", raising=False)
    pat_file = tmp_path / "raw_pat.txt"
    pat_file.write_text("pt-base64token==\n", encoding="utf-8")
    provider = QoderProvider()
    pat, _source = provider._resolve_pat(_cfg(pat_file=str(pat_file)))
    assert pat == "pt-base64token=="
