"""Integration tests for the FastAPI local Web UI and session security."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

from geteggs.core.models import (
    CheckinResult,
    CheckinStatus,
    DoctorResult,
    ProviderConfig,
)
from geteggs.core.config import get_qoder_pat_path, today_biz_date
from geteggs.core.lock import acquire_run_lock
from geteggs.core.runner import CheckinRunner
from geteggs.core.storage import Storage
from geteggs.notifiers.base import BaseNotifier
from geteggs.providers.base import BaseProvider, LogCallback
from geteggs.providers.qoder import QoderProvider
from geteggs.web.app import create_app


class DummyNotifier(BaseNotifier):
    channel_id = "dummy"

    def send(
        self,
        title: str,
        message: str,
        *,
        level: str = "info",
        action_label: Optional[str] = None,
        action_url: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        return True, None


class FakeWebProvider(BaseProvider):
    """Hermetic provider used by Web API tests; never touches the network."""

    id = "workbuddy"
    display_name = "Fake WorkBuddy"
    description = "Fake provider for web tests"
    supports_browser_auth = True

    def __init__(self) -> None:
        self.checkin_calls = 0

    def detect_client_exe(self, config: ProviderConfig) -> Optional[str]:
        return None

    async def doctor(self, config: ProviderConfig) -> DoctorResult:
        return DoctorResult(
            provider_id=self.id,
            display_name=self.display_name,
            client_installed=False,
            auth_file_found=True,
            auth_readable=True,
            healthy=True,
            message="fake healthy",
        )

    async def checkin(
        self,
        config: ProviderConfig,
        biz_date: str,
        *,
        dry_run: bool = False,
        log: Optional[LogCallback] = None,
    ) -> CheckinResult:
        self.checkin_calls += 1
        return CheckinResult(
            provider_id=self.id,
            display_name=self.display_name,
            status=CheckinStatus.ALREADY_DONE,
            message="fake already done",
            biz_date=biz_date,
            dry_run=dry_run,
        )

    async def authorize_browser(self, config: ProviderConfig) -> tuple[bool, str]:
        return True, "ok_saved"


class FakeTraeProvider(FakeWebProvider):
    id = "trae"
    display_name = "Fake Trae"


class FixedBusinessDayWebProvider(FakeWebProvider):
    id = "qoder"
    display_name = "Fake Qoder"
    fixed_business_day_start = "10:00"


class BlockingWebProvider(FakeWebProvider):
    """Provider that keeps a run active until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    async def checkin(
        self,
        config: ProviderConfig,
        biz_date: str,
        *,
        dry_run: bool = False,
        log: Optional[LogCallback] = None,
    ) -> CheckinResult:
        self.started.set()
        await asyncio.to_thread(self.release.wait, 5)
        return await super().checkin(
            config, biz_date, dry_run=dry_run, log=log
        )


def _patch_schedule(monkeypatch) -> None:
    monkeypatch.setattr(
        "geteggs.web.app.get_schedule_status",
        lambda: {
            "supported": True,
            "installed": False,
            "task_name": "GetEggs-DailyCheckin",
            "message": "尚未安装系统计划任务。",
        },
    )


def test_web_auth_exchange_and_origin_guard(tmp_path: Path, monkeypatch) -> None:
    _patch_schedule(monkeypatch)
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    monkeypatch.delenv("GETEGGS_ALLOW_CUSTOM_CONFIG", raising=False)
    storage = Storage(tmp_path / "web.db")
    runner = CheckinRunner(storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock")
    provider = FakeWebProvider()
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=True,
        startup_token="test_startup_secret",
        providers=[provider],
    )
    client = TestClient(app)

    # Unauthenticated API access must fail.
    unauth = client.get("/api/overview")
    assert unauth.status_code == 401

    # Invalid startup token must fail.
    bad_auth = client.get("/auth?token=wrong", follow_redirects=False)
    assert bad_auth.status_code == 401

    # Valid startup token sets HttpOnly cookie and redirects to /.
    good_auth = client.get("/auth?token=test_startup_secret", follow_redirects=False)
    assert good_auth.status_code == 302
    assert "geteggs_session" in good_auth.cookies

    # Cross-origin browser request must be blocked.
    evil = client.get("/api/overview", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403

    # Authenticated localhost API succeeds.
    overview = client.get("/api/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["providers"][0]["provider_id"] == "workbuddy"
    assert body["providers"][0]["supports_browser_auth"] is True
    assert "supports_streak" not in body["providers"][0]

    # Notifier test endpoint succeeds.
    toast_resp = client.post("/api/notifiers/test")
    assert toast_resp.status_code == 200
    assert toast_resp.json()["ok"] is True

    # Provider settings update persists extra fields and jitter override.
    browser_state_target = str((tmp_path / "browser_state.json").as_posix())
    upd = client.put(
        "/api/providers/workbuddy/config",
        json={
            "enabled": True,
            "auth_file_path": "C:/custom/workbuddy-desktop.info",
            "browser_state_path": browser_state_target,
            "jitter_minutes": 5,
            "business_day_start": "10:00",
        },
    )
    assert upd.status_code == 200
    provider_extra = upd.json()["provider"]["extra"]
    assert provider_extra["auth_file_path"] == "C:/custom/workbuddy-desktop.info"
    assert provider_extra["browser_state_path"] == browser_state_target
    assert provider_extra["jitter_override"] is True
    assert "checkin_credits" not in upd.json()["provider"]
    assert upd.json()["provider"]["business_day_start"] == "10:00"
    assert "checkin_credits" not in client.get("/api/overview").json()["providers"][0]
    settings_provider = client.get("/api/settings").json()["providers"][0]
    assert "checkin_credits" not in settings_provider
    assert settings_provider["business_day_start"] == "10:00"
    assert "supports_streak" not in settings_provider

    reset_jitter = client.put(
        "/api/providers/workbuddy/config", json={"jitter_override": False}
    )
    assert reset_jitter.status_code == 200
    assert "jitter_override" not in reset_jitter.json()["provider"]["extra"]

    # Invalid inputs are rejected before touching the database.
    assert client.put("/api/settings", json={"schedule_time": "25:99"}).status_code == 422
    assert (
        client.put(
            "/api/settings", json={"run_events_retention_days": 0}
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/providers/workbuddy/config", json={"earliest_time": "9:00"}
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/providers/workbuddy/config", json={"business_day_start": "25:00"}
        ).status_code
        == 422
    )

    # Settings and history endpoints succeed.
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/history").status_code == 200
    assert client.get("/api/notifications").status_code == 200

    # Triggering dry-run creates a run_id and streams SSE done event.
    run_resp = client.post("/api/runs", json={"provider_id": "workbuddy", "dry_run": True})
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run_id"]
    with client.stream("GET", f"/api/runs/{run_id}/stream") as stream:
        assert stream.status_code == 200
        text = "".join(stream.iter_text())
        assert "event: done" in text

    runs = client.get("/api/runs").json()
    assert runs["runs"][0]["run_id"] == run_id
    assert runs["runs"][0]["status"] == "completed"
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["records"][0]["provider_id"] == "workbuddy"
    assert detail["events"]
    assert any("开始执行" in event["message"] for event in detail["events"])
    assert provider.checkin_calls == 1

    info = client.get("/api/maintenance").json()
    assert "counts" in info
    cleanup = client.post("/api/maintenance/cleanup", json={"vacuum": False})
    assert cleanup.status_code == 200
    cleanup_body = cleanup.json()
    assert cleanup_body["ok"] is True
    assert cleanup_body["info"]["last_cleanup_at"]
    assert cleanup_body["backup"]


def test_web_fixed_business_day_start_is_locked(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_schedule(monkeypatch)
    storage = Storage(tmp_path / "fixed_day.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[FixedBusinessDayWebProvider()],
    )
    client = TestClient(app)

    provider_settings = client.get("/api/settings").json()["providers"][0]
    assert provider_settings["fixed_business_day_start"] == "10:00"

    rejected = client.put(
        "/api/providers/qoder/config", json={"business_day_start": "09:00"}
    )
    assert rejected.status_code == 400
    assert "固定" in rejected.json()["detail"]

    accepted = client.put(
        "/api/providers/qoder/config", json={"business_day_start": "10:00"}
    )
    assert accepted.status_code == 200
    assert accepted.json()["provider"]["business_day_start"] == "10:00"


def test_web_auth_browser_endpoint_delegates_to_provider(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_schedule(monkeypatch)
    storage = Storage(tmp_path / "web2.db")
    runner = CheckinRunner(storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock")
    provider = FakeWebProvider()
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[provider],
    )
    client = TestClient(app)
    resp = client.post("/api/providers/workbuddy/auth-browser")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"

    status_payload = {}
    for _ in range(50):
        status_payload = client.get(
            "/api/providers/workbuddy/auth-browser/status"
        ).json()
        if status_payload.get("status") != "running":
            break
        time.sleep(0.02)
    assert status_payload["status"] == "success"
    assert status_payload["message"] == "ok_saved"


def test_web_overview_keeps_success_after_later_failure(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_schedule(monkeypatch)
    storage = Storage(tmp_path / "web3.db")
    biz_date = today_biz_date(storage.get_settings().timezone)
    storage.save_checkin_result(
        "seed",
        CheckinResult(
            provider_id="workbuddy",
            display_name="Fake WorkBuddy",
            status=CheckinStatus.SUCCESS,
            message="签到成功",
            biz_date=biz_date,
        ),
    )
    storage.save_checkin_result(
        "seed",
        CheckinResult(
            provider_id="workbuddy",
            display_name="Fake WorkBuddy",
            status=CheckinStatus.FAILED,
            message="network timeout",
            biz_date=biz_date,
        ),
    )
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[FakeWebProvider()],
    )
    client = TestClient(app)
    overview = client.get("/api/overview").json()

    assert "today_delta" not in overview
    assert "trend_30d" not in overview
    assert "total_balance" not in overview
    card = overview["providers"][0]
    assert card["today_record"]["status"] == "success"
    assert "balance" not in card["today_record"]
    assert "delta" not in card["today_record"]
    assert "最近一次重跑异常" in card["today_record"]["message"]


def test_web_overview_does_not_block_on_schedule(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[int] = []

    def fake_schedule() -> dict:
        calls.append(1)
        return {
            "supported": True,
            "installed": False,
            "task_name": "GetEggs-DailyCheckin",
            "message": "尚未安装系统计划任务。",
        }

    monkeypatch.setattr("geteggs.web.app.get_schedule_status", fake_schedule)
    storage = Storage(tmp_path / "overview.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[FakeWebProvider()],
    )
    client = TestClient(app)

    body = client.get("/api/overview").json()
    assert body["schedule"] is None
    assert body["schedule_stale"] is False
    assert calls == []

    schedule = client.get("/api/schedule").json()
    assert schedule["installed"] is False
    assert calls == [1]

    refreshed = client.get("/api/overview").json()
    assert refreshed["schedule"]["installed"] is False
    assert refreshed["schedule_stale"] is False


def test_web_qoder_pat_save_status_clear(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_schedule(monkeypatch)
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("QODERCN_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GETEGGS_QODER_PAT_FILE", raising=False)
    storage = Storage(tmp_path / "pat.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "pat.lock"
    )
    qoder = QoderProvider()
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[qoder],
    )
    client = TestClient(app)

    initial = client.get("/api/providers/qoder/pat/status")
    assert initial.status_code == 200
    assert initial.json()["configured"] is False
    assert initial.json()["source"] is None

    pat = "pt-web-secret-token-123456"
    saved = client.put("/api/providers/qoder/pat", json={"pat": f"  {pat}  "})
    assert saved.status_code == 200
    body = saved.json()
    assert body["configured"] is True
    assert body["format_ok"] is True
    assert "网页输入" in body["source"]
    assert pat not in saved.text

    stored = get_qoder_pat_path()
    assert stored.is_file()
    assert pat.encode("utf-8") not in stored.read_bytes()
    assert pat.encode("utf-8") not in (tmp_path / "pat.db").read_bytes()

    status = client.get("/api/providers/qoder/pat/status")
    assert status.status_code == 200
    assert status.json()["configured"] is True
    assert pat not in status.text

    token, source = qoder._resolve_pat(storage.get_provider_config("qoder"))
    assert token == pat
    assert "网页输入" in (source or "")

    invalid = client.put("/api/providers/qoder/pat", json={"pat": "   "})
    assert invalid.status_code == 400
    assert "PAT" in invalid.json()["detail"]

    cleared = client.delete("/api/providers/qoder/pat")
    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False
    assert not stored.exists()


def test_web_trae_allow_token_refresh_persists(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "trae-config.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[FakeTraeProvider()],
    )
    client = TestClient(app)

    response = client.put(
        "/api/providers/trae/config",
        json={"allow_token_refresh": False},
    )
    assert response.status_code == 200
    provider = response.json()["provider"]
    assert provider["extra"]["allow_token_refresh"] is False
    assert "checkin_credits" not in provider

    settings_provider = client.get("/api/settings").json()["providers"][0]
    assert settings_provider["extra"]["allow_token_refresh"] is False

    response = client.put(
        "/api/providers/trae/config", json={"allow_token_refresh": True}
    )
    assert response.status_code == 200
    assert response.json()["provider"]["extra"]["allow_token_refresh"] is True


def test_web_run_accepts_multiple_provider_ids(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "multi.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    first = FakeWebProvider()
    second = FakeTraeProvider()
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[first, second],
    )
    client = TestClient(app)

    empty = client.post("/api/runs", json={"provider_ids": [], "dry_run": True})
    assert empty.status_code == 400

    response = client.post(
        "/api/runs",
        json={"provider_ids": ["workbuddy", "trae"], "dry_run": True},
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    detail: dict = {}
    for _ in range(100):
        detail = client.get(f"/api/runs/{run_id}").json()
        if detail["run"]["status"] != "running":
            break
        time.sleep(0.02)

    assert detail["run"]["status"] == "completed"
    assert {record["provider_id"] for record in detail["records"]} == {
        "workbuddy",
        "trae",
    }
    assert first.checkin_calls == 1
    assert second.checkin_calls == 1


def test_web_shutdown_rejects_active_run(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "shutdown.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    provider = BlockingWebProvider()
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[provider],
    )
    client = TestClient(app)

    response = client.post(
        "/api/runs", json={"provider_id": "workbuddy", "dry_run": True}
    )
    assert response.status_code == 200
    assert provider.started.wait(2)

    blocked = client.post("/api/system/shutdown")
    assert blocked.status_code == 409
    assert "正在执行" in blocked.json()["detail"]

    provider.release.set()
    time.sleep(0.3)


def test_web_shutdown_rejects_external_run_lock(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "shutdown_lock.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[FakeWebProvider()],
    )
    client = TestClient(app)

    with acquire_run_lock(runner.lock_path):
        blocked = client.post("/api/system/shutdown")

    assert blocked.status_code == 409
    assert "计划任务" in blocked.json()["detail"]


def test_web_run_lock_busy_emits_run_busy_code(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "run_busy.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[FakeWebProvider()],
    )
    client = TestClient(app)

    with acquire_run_lock(runner.lock_path):
        # Real check-in respects the exclusive execution lock.
        response = client.post(
            "/api/runs", json={"provider_ids": ["workbuddy"], "dry_run": False}
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        with client.stream("GET", f"/api/runs/{run_id}/stream") as stream:
            body = "".join(stream.iter_text())

        # Read-only dry-run preflight does not block on the lock.
        dry_resp = client.post(
            "/api/runs", json={"provider_ids": ["workbuddy"], "dry_run": True}
        )
        assert dry_resp.status_code == 200
        dry_run_id = dry_resp.json()["run_id"]
        with client.stream("GET", f"/api/runs/{dry_run_id}/stream") as dry_stream:
            dry_body = "".join(dry_stream.iter_text())

    assert "另一个签到任务正在执行中" in body
    assert '"code": "run_busy"' in body
    assert "event: done" in dry_body
    assert '"code": "run_busy"' not in dry_body


def test_web_overview_uses_provider_business_date(tmp_path: Path) -> None:
    class FixedBusinessDayProvider(FakeWebProvider):
        def business_date(
            self,
            config: ProviderConfig,
            tz_name: str = "Asia/Shanghai",
            *,
            now: Optional[object] = None,
        ) -> str:
            return "2026-11-18"

    storage = Storage(tmp_path / "overview_business_day.db")
    storage.save_checkin_result(
        "seed-run",
        CheckinResult(
            provider_id="workbuddy",
            display_name="Fake WorkBuddy",
            status=CheckinStatus.SUCCESS,
            message="上一业务日签到成功",
            biz_date="2026-11-18",
        ),
    )
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[FixedBusinessDayProvider()],
    )
    client = TestClient(app)

    body = client.get("/api/overview").json()

    assert "today_delta" not in body
    assert body["providers"][0]["today_record"]["biz_date"] == "2026-11-18"
    assert body["providers"][0]["today_record"]["status"] == "success"


def test_web_static_assets_and_settings_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_schedule(monkeypatch)
    storage = Storage(tmp_path / "static_check.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[FakeWebProvider()],
    )
    client = TestClient(app)

    settings_resp = client.get("/api/settings")
    assert settings_resp.status_code == 200
    provider_cfg = settings_resp.json()["providers"][0]
    assert provider_cfg["description"] == "Fake provider for web tests"
    assert provider_cfg["supports_browser_auth"] is True

    html_resp = client.get("/")
    assert html_resp.status_code == 200
    assert 'id="overview-alert-strip"' in html_resp.text
    assert 'id="settings-save-bar"' in html_resp.text
    assert 'id="confirm-dialog"' in html_resp.text
    assert 'id="metric-progress-bar"' in html_resp.text
    assert 'id="metric-web-status"' in html_resp.text
    assert html_resp.text.index('id="btn-shutdown-web"') < html_resp.text.index('id="tab-history"')
    assert 'class="schedule-status-bar"' in html_resp.text
    assert 'id="metric-balance-list"' not in html_resp.text
    for removed_section in ("aux-ribbon", "aux-hero", "aux-dark-band", "aux-cobalt-card"):
        assert removed_section not in html_resp.text
    overview_js = client.get("/static/overview.js")
    assert overview_js.status_code == 200
    assert "formatDateTime," in overview_js.text
    assert "updateAuxiaTelemetry" not in overview_js.text
    assert "当前余额" not in overview_js.text
    assert "metric-balance-list" not in overview_js.text

    utils_js = client.get("/static/utils.js")
    assert utils_js.status_code == 200
    assert "animateNumber" not in utils_js.text

    styles_css = client.get("/static/styles.css")
    assert styles_css.status_code == 200
    assert ".trend-bar" not in styles_css.text
    assert ".aux-hero" not in styles_css.text
    assert ".aux-cobalt-card" not in styles_css.text

    for asset in (
        "/static/styles.css",
        "/static/app.js",
        "/static/utils.js",
        "/static/overview.js",
        "/static/history.js",
        "/static/settings.js",
    ):
        resp = client.get(asset)
        assert resp.status_code == 200, f"failed to fetch {asset}"
    assert client.get("/static/charts.js").status_code == 404


def test_web_security_headers_csrf_and_custom_config_toggle(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_schedule(monkeypatch)
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    monkeypatch.delenv("GETEGGS_ALLOW_CUSTOM_CONFIG", raising=False)
    storage = Storage(tmp_path / "sec.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=True,
        startup_token="tok123",
        providers=[FakeWebProvider()],
    )
    client = TestClient(app)

    # OpenAPI schema and static index.html must not bypass auth.
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/static/index.html").status_code == 401

    auth_resp = client.get("/auth?token=tok123", follow_redirects=False)
    assert auth_resp.status_code == 302
    assert auth_resp.headers.get("x-frame-options") == "DENY"
    assert auth_resp.headers.get("x-content-type-options") == "nosniff"
    assert auth_resp.headers.get("cache-control") == "no-store"
    assert "frame-ancestors 'none'" in (
        auth_resp.headers.get("content-security-policy") or ""
    )

    # Launcher splash page navigates from another loopback port to /auth.
    splash_nav = client.get(
        "/auth?token=tok123",
        follow_redirects=False,
        headers={
            "Host": "127.0.0.1:18765",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "navigate",
        },
    )
    assert splash_nav.status_code == 302

    splash_root = client.get(
        "/",
        headers={
            "Host": "127.0.0.1:18765",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "navigate",
        },
    )
    assert splash_root.status_code == 200

    # Same-site (cross-port) API access must still be blocked.
    cross_port_api = client.get(
        "/api/overview",
        headers={
            "Host": "127.0.0.1:18765",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
        },
    )
    assert cross_port_api.status_code == 403

    # Cross-port localhost CSRF and missing custom header must be blocked.
    cross_port = client.post(
        "/api/notifiers/test",
        headers={
            "Host": "127.0.0.1:18765",
            "Origin": "http://127.0.0.1:3000",
            "X-GetEggs-Request": "1",
        },
    )
    assert cross_port.status_code == 403

    missing_csrf_hdr = client.post(
        "/api/notifiers/test",
        headers={
            "Host": "127.0.0.1:18765",
            "Origin": "http://127.0.0.1:18765",
        },
    )
    assert missing_csrf_hdr.status_code == 403

    same_origin_ok = client.post(
        "/api/notifiers/test",
        headers={
            "Host": "127.0.0.1:18765",
            "Origin": "http://127.0.0.1:18765",
            "Sec-Fetch-Site": "same-origin",
            "X-GetEggs-Request": "1",
        },
    )
    assert same_origin_ok.status_code == 200

    # IPv6 loopback host must be parsed properly.
    ipv6_ok = client.get("/api/overview", headers={"Host": "[::1]:18765"})
    assert ipv6_ok.status_code == 200

    # Strict config validation rejects untrusted base_url, UNC paths, and out-of-dir browser state.
    assert (
        client.put(
            "/api/providers/workbuddy/config",
            json={"base_url": "https://evil.example"},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/providers/workbuddy/config",
            json={"base_url": "http://www.codebuddy.cn"},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/providers/workbuddy/config",
            json={"auth_file_path": r"\\attacker\share\info.json"},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/providers/workbuddy/config",
            json={"browser_state_path": "C:/Windows/Temp/outside.json"},
        ).status_code
        == 422
    )

    # Env toggle relaxes allowlist while still blocking UNC paths and control chars.
    monkeypatch.setenv("GETEGGS_ALLOW_CUSTOM_CONFIG", "1")
    relaxed_ok = client.put(
        "/api/providers/workbuddy/config",
        json={
            "base_url": "http://127.0.0.1:9999",
            "browser_state_path": "C:/custom/outside.json",
        },
    )
    assert relaxed_ok.status_code == 200
    assert (
        client.put(
            "/api/providers/workbuddy/config",
            json={"auth_file_path": r"\\attacker\share\still-blocked"},
        ).status_code
        == 422
    )


def test_web_shutdown_triggers_graceful_uvicorn_exit(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "graceful.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[FakeWebProvider()],
    )

    class FakeServer:
        should_exit = False

    fake_server = FakeServer()
    app.state.uvicorn_server = fake_server
    with TestClient(app) as client:
        resp = client.post("/api/system/shutdown")
        assert resp.status_code == 200
        # Once shutting down, new runs are rejected.
        blocked_run = client.post(
            "/api/runs", json={"provider_id": "workbuddy", "dry_run": True}
        )
        assert blocked_run.status_code == 503
        time.sleep(0.4)
        assert fake_server.should_exit is True


def test_web_sse_does_not_drop_terminal_event_during_history_yield() -> None:
    from geteggs.web.app import ActiveRunState

    state = ActiveRunState(run_id="race-1")
    state.publish("log", {"message": "step-1"})
    history, queue, was_finished = state.subscribe(0)
    assert was_finished is False
    assert queue is not None
    assert len(history) == 1

    # Simulate completion occurring while the SSE generator is still yielding history.
    state.publish("log", {"message": "step-2"})
    state.complete({"run_id": "race-1"})

    drained = []
    while True:
        item = queue.get_nowait()
        if item is None:
            break
        drained.append(item)
    assert [item["type"] for item in drained] == ["log", "done"]
    assert drained[0]["data"]["message"] == "step-2"


def test_web_history_month_validation_biz_date_and_provider_config_edges(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_schedule(monkeypatch)
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    storage = Storage(tmp_path / "p1_edges.db")
    for idx, day in enumerate(("2026-11-10", "2026-11-11")):
        storage.save_checkin_result(
            f"run-{idx}",
            CheckinResult(
                provider_id="workbuddy",
                display_name="Fake WorkBuddy",
                status=CheckinStatus.SUCCESS,
                message=f"ok-{idx}",
                biz_date=day,
            ),
        )
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    qoder = QoderProvider()
    qoder._token_cache[("https://openapi.qoder.com.cn", "digest")] = ("cached-tok", 9e9)

    class NoBrowserProvider(FakeTraeProvider):
        supports_browser_auth = False

    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=False,
        providers=[FakeWebProvider(), NoBrowserProvider(), qoder],
    )
    with TestClient(app) as client:
        # Invalid month wildcard must be rejected (422).
        assert client.get("/api/history?month=%25").status_code == 422

        filtered = client.get(
            "/api/history?month=2026-11&biz_date=2026-11-10&limit=10"
        )
        assert filtered.status_code == 200
        body = filtered.json()
        assert [r["biz_date"] for r in body["records"]] == ["2026-11-10"]
        assert body["has_more"] is False

        # jitter_override=False must take precedence even when jitter_minutes is also sent.
        upd = client.put(
            "/api/providers/workbuddy/config",
            json={"jitter_minutes": 10, "jitter_override": False},
        )
        assert upd.status_code == 200
        assert "jitter_override" not in upd.json()["provider"]["extra"]

        # Providers without browser auth support must be rejected upfront (400).
        unsupported = client.post("/api/providers/trae/auth-browser")
        assert unsupported.status_code == 400

        # Clearing Qoder PAT must evict in-memory exchanged token cache.
        cleared = client.delete("/api/providers/qoder/pat")
        assert cleared.status_code == 200
        assert qoder._token_cache == {}

        # Verify P1 frontend layout/interaction fixes remain intact.
        css_text = client.get("/static/styles.css").text
        assert "margin-inline: auto;" in css_text
        utils_text = client.get("/static/utils.js").text
        assert "X-GetEggs-Request" in utils_text
        assert "Array.isArray(payload.detail)" in utils_text
        history_text = client.get("/static/history.js").text
        assert "biz_date" in history_text
        assert "has_more" in history_text

        # Verify P2 WCAG accessibility & frontend hygiene fixes remain intact.
        html_text = client.get("/").text
        assert 'aria-controls="subtab-runs"' in html_text
        assert 'id="live-drawer-log" class="live-drawer-log" role="log" aria-live="polite"' in html_text
        assert "一次性令牌" not in html_text

        assert "visibility: hidden;" in css_text
        assert 'body[data-tab="settings"] .toast-banner' in css_text

        app_text = client.get("/static/app.js").text
        assert "window.__trendResizeTimer" not in app_text
        assert "appState.stopped" in app_text

        settings_text = client.get("/static/settings.js").text
        assert "PLANNED_TOOLS" not in settings_text
        assert 'role="switch"' in settings_text
        assert "267008" in settings_text
        assert "checkin_credits" not in settings_text
        assert "签到积分" not in settings_text
        assert "window.__bannerTimer" not in utils_text

        # Verify follow-up audit regression fixes & credit/streak removal in frontend assets.
        assert 'aria-describedby="metric-schedule metric-schedule-chip metric-schedule-sub"' in html_text
        assert "outline: none;" not in css_text
        overview_text = client.get("/static/overview.js").text
        assert "updateRunningCardPills();" in overview_text
        assert "appState.stopped = true" in overview_text
        assert "连续天数" not in overview_text
        assert "今日收益" not in overview_text
        assert "连签" not in html_text
        assert "连签" not in history_text
        assert "今日总收益" not in html_text
        assert "积分领取趋势" not in html_text
        assert "set-catchup-logon" not in html_text
        assert "interrupted" in history_text
        assert "skipCalendarRender" in history_text


def test_web_static_index_html_auth_guard_is_case_insensitive(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_schedule(monkeypatch)
    monkeypatch.setenv("GETEGGS_HOME", str(tmp_path))
    storage = Storage(tmp_path / "auth_case.db")
    runner = CheckinRunner(
        storage=storage, notifier=DummyNotifier(), lock_path=tmp_path / "run.lock"
    )
    app = create_app(
        storage=storage,
        runner=runner,
        auth_enabled=True,
        providers=[FakeWebProvider()],
    )
    with TestClient(app) as client:
        assert client.get("/static/index.html").status_code == 401
        assert client.get("/static/INDEX.HTML").status_code == 401
        assert client.get("/static/Index.Html").status_code == 401
