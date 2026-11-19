"""FastAPI local Web UI server with SSE real-time check-in logs."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from geteggs import __version__
from geteggs.core.config import get_qoder_pat_path, today_biz_date
from geteggs.core.lock import RunLockBusyError, acquire_run_lock
from geteggs.core.models import (
    MAX_CHECKIN_CREDITS,
    GlobalSettings,
    LogEvent,
    TriggerType,
    validate_hhmm,
)
from geteggs.core.runner import CheckinRunner
from geteggs.core.secure_store import save_secure_json
from geteggs.core.storage import Storage
from geteggs.notifiers.windows_toast import WindowsToastNotifier
from geteggs.providers.base import BaseProvider
from geteggs.providers.registry import list_providers
from geteggs.scheduler.windows import (
    get_schedule_status,
    install_schedule,
    uninstall_schedule,
)
from geteggs.web.auth import COOKIE_NAME, SessionAuthManager, is_local_origin

STATIC_DIR = Path(__file__).parent / "static"


class TriggerRunRequest(BaseModel):
    provider_id: Optional[str] = None
    provider_ids: Optional[list[str]] = None
    dry_run: bool = False


class ProviderConfigUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    earliest_time: Optional[str] = None
    business_day_start: Optional[str] = None
    jitter_minutes: Optional[int] = None
    checkin_credits: Optional[int] = None
    jitter_override: Optional[bool] = None
    auth_file_path: Optional[str] = None
    client_exe_path: Optional[str] = None
    browser_state_path: Optional[str] = None
    base_url: Optional[str] = None

    @field_validator("earliest_time")
    @classmethod
    def _validate_earliest_time(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return validate_hhmm(value)

    @field_validator("business_day_start")
    @classmethod
    def _validate_business_day_start(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return validate_hhmm(value)

    @field_validator("jitter_minutes")
    @classmethod
    def _validate_jitter_minutes(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        if not 0 <= value <= 120:
            raise ValueError("Provider 随机抖动窗口必须在 0-120 分钟之间。")
        return value

    @field_validator("checkin_credits")
    @classmethod
    def _validate_checkin_credits(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        if not 0 <= value <= MAX_CHECKIN_CREDITS:
            raise ValueError(f"签到积分必须在 0-{MAX_CHECKIN_CREDITS} 之间。")
        return value


class MaintenanceCleanupRequest(BaseModel):
    vacuum: bool = False


class QoderPatRequest(BaseModel):
    pat: str


@dataclass
class ActiveRunState:
    run_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[asyncio.Queue[Optional[dict[str, Any]]]] = field(default_factory=list)
    finished: bool = False
    summary: Optional[dict[str, Any]] = None
    seq: int = 0
    created_at: float = field(default_factory=time.monotonic)
    task: Optional[asyncio.Task[None]] = None

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        item = {"id": self.seq, "type": event_type, "data": payload}
        self.events.append(item)
        for q in list(self.subscribers):
            q.put_nowait(item)

    def complete(self, summary: Optional[dict[str, Any]], error: Optional[str] = None) -> None:
        self.finished = True
        self.summary = summary
        if error:
            self.publish("error", {"message": error})
        else:
            self.publish("done", summary or {})
        for q in list(self.subscribers):
            q.put_nowait(None)

    def subscribe(
        self, last_event_id: int = 0
    ) -> tuple[list[dict[str, Any]], asyncio.Queue[Optional[dict[str, Any]]]]:
        q: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue()
        self.subscribers.append(q)
        history = [item for item in self.events if item["id"] > last_event_id]
        return history, q

    def unsubscribe(self, q: asyncio.Queue[Optional[dict[str, Any]]]) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)


@dataclass
class AuthTaskState:
    provider_id: str
    status: str = "running"
    message: str = "正在等待浏览器登录..."
    started_at: float = field(default_factory=time.monotonic)
    finished_at: Optional[float] = None
    task: Optional[asyncio.Task[None]] = None


def create_app(
    *,
    storage: Optional[Storage] = None,
    runner: Optional[CheckinRunner] = None,
    auth_enabled: bool = True,
    startup_token: Optional[str] = None,
    providers: Optional[list[BaseProvider]] = None,
) -> FastAPI:
    """Create the FastAPI application for GetEggs local Web UI."""
    app_storage = storage or Storage()
    app_runner = runner or CheckinRunner(storage=app_storage, notifier=WindowsToastNotifier())
    auth_mgr = SessionAuthManager(enabled=auth_enabled, startup_token=startup_token)
    active_runs: dict[str, ActiveRunState] = {}
    auth_tasks: dict[str, AuthTaskState] = {}
    app_providers = list(providers) if providers is not None else list_providers()
    schedule_cache: dict[str, Any] = {"value": None, "expires_at": 0.0}

    def resolve_provider(provider_id: str) -> BaseProvider:
        for provider in app_providers:
            if provider.id == provider_id:
                return provider
        raise KeyError(f"未知的插件 ID: {provider_id}")

    def resolve_qoder_provider() -> BaseProvider:
        try:
            provider = resolve_provider("qoder")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not hasattr(provider, "pat_status"):
            raise HTTPException(status_code=404, detail="Qoder 插件不支持 PAT 配置。")
        return provider

    def prune_active_runs() -> None:
        finished = [state for state in active_runs.values() if state.finished]
        if len(finished) <= 20:
            return
        for state in sorted(finished, key=lambda item: item.created_at)[:-20]:
            active_runs.pop(state.run_id, None)

    async def schedule_status_cached() -> dict[str, Any]:
        now = time.monotonic()
        if schedule_cache["value"] is None or now >= schedule_cache["expires_at"]:
            schedule_cache["value"] = await run_in_threadpool(get_schedule_status)
            schedule_cache["expires_at"] = now + 45.0
        return schedule_cache["value"]

    def runner_lock_busy() -> bool:
        """Return True when a check-in run (including scheduled ones) holds the lock."""
        try:
            with acquire_run_lock(app_runner.lock_path):
                return False
        except RunLockBusyError:
            return True

    app = FastAPI(
        title="GetEggs Local Console",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.state.auth_manager = auth_mgr
    app.state.storage = app_storage
    app.state.runner = app_runner
    app.state.providers = app_providers
    app.state.auth_tasks = auth_tasks

    @app.middleware("http")
    async def local_origin_guard(request: Request, call_next: Any) -> Any:
        if not is_local_origin(request):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "仅允许本机来源访问 GetEggs 控制台。"},
            )
        return await call_next(request)

    def require_auth(request: Request) -> None:
        auth_mgr.verify_request(request)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/auth")
    async def exchange_auth(token: Optional[str] = Query(default=None)) -> RedirectResponse:
        if not auth_mgr.verify_startup_token(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="启动鉴权令牌无效，请重新运行 `geteggs ui` 打开控制台。",
            )
        resp = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
        resp.set_cookie(
            key=COOKIE_NAME,
            value=auth_mgr.session_value,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return resp

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        if auth_mgr.enabled:
            try:
                auth_mgr.verify_request(request)
            except HTTPException:
                return HTMLResponse(
                    content=(
                        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                        "<body style='font-family:system-ui;background:#f4f6f8;color:#111827;padding:48px;'>"
                        "<h2>GetEggs 本地会话未激活</h2>"
                        "<p>为保护本地接口安全，请通过终端运行 <code>geteggs ui</code> 自动打开带启动令牌的链接。</p>"
                        "</body></html>"
                    ),
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    @app.get("/api/overview", dependencies=[Depends(require_auth)])
    async def api_overview() -> dict[str, Any]:
        settings = app_storage.get_settings()
        biz_date = today_biz_date(settings.timezone)
        cards: list[dict[str, Any]] = []
        today_delta = 0
        # Never block the first paint on a Task Scheduler query. The UI fetches
        # /api/schedule asynchronously when no cached value is available yet.
        schedule_info = schedule_cache["value"]
        schedule_stale = bool(
            schedule_info is not None
            and time.monotonic() >= schedule_cache["expires_at"]
        )

        for provider in app_providers:
            cfg = app_storage.get_provider_config(provider.id)
            # Align the card with the provider's own business day (Qoder resets
            # at 10:00), so early-morning runs show the previous business day.
            provider_biz_date = provider.business_date(cfg, settings.timezone)
            today_rec = app_storage.get_today_summary(provider.id, provider_biz_date)
            latest_rec = app_storage.get_latest_snapshot_record(
                provider.id
            ) or app_storage.get_latest_record(provider.id)
            if today_rec and today_rec.get("delta"):
                today_delta += int(today_rec["delta"])

            cards.append(
                {
                    "provider_id": provider.id,
                    "display_name": provider.display_name,
                    "description": provider.description,
                    "enabled": cfg.enabled,
                    "earliest_time": cfg.earliest_time,
                    "jitter_minutes": cfg.jitter_minutes,
                    "extra": cfg.extra,
                    "client_installed": provider.detect_client_exe(cfg) is not None,
                    "supports_browser_auth": provider.supports_browser_auth,
                    "supports_streak": provider.supports_streak,
                    "fixed_business_day_start": provider.fixed_business_day_start,
                    "today_record": today_rec,
                    "latest_record": latest_rec,
                }
            )

        return {
            "version": __version__,
            "biz_date": biz_date,
            "today_delta": today_delta,
            "providers": cards,
            "trend_30d": app_storage.get_trend_30_days(settings.timezone),
            "schedule": schedule_info,
            "schedule_stale": schedule_stale,
        }

    @app.post("/api/doctor", dependencies=[Depends(require_auth)])
    async def api_doctor(provider_id: Optional[str] = Query(default=None)) -> dict[str, Any]:
        if provider_id:
            try:
                providers = [resolve_provider(provider_id)]
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        else:
            providers = app_providers
        results = []
        for p in providers:
            cfg = app_storage.get_provider_config(p.id)
            res = await p.doctor(cfg)
            results.append(res.model_dump())
        return {"results": results}

    @app.post("/api/runs", dependencies=[Depends(require_auth)])
    async def api_start_run(req: TriggerRunRequest) -> dict[str, Any]:
        requested_ids = req.provider_ids
        if requested_ids is None and req.provider_id:
            requested_ids = [req.provider_id]
        if requested_ids is not None and not requested_ids:
            raise HTTPException(
                status_code=400, detail="请至少选择一个需要执行签到的工具。"
            )

        targets = app_providers
        if requested_ids:
            targets = []
            seen: set[str] = set()
            for provider_id in requested_ids:
                if provider_id in seen:
                    continue
                seen.add(provider_id)
                try:
                    targets.append(resolve_provider(provider_id))
                except KeyError as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc

        run_id = uuid.uuid4().hex[:12]
        state = ActiveRunState(run_id=run_id)
        active_runs[run_id] = state

        def on_event(ev: LogEvent) -> None:
            state.publish("log", ev.model_dump())

        async def _execute() -> None:
            try:
                summary = await app_runner.run(
                    provider_ids=[p.id for p in targets] if requested_ids else None,
                    providers_override=targets,
                    trigger_type=TriggerType.MANUAL_WEB,
                    missed_only=False,
                    apply_jitter=False,
                    notify=not req.dry_run,
                    dry_run=req.dry_run,
                    event_sink=on_event,
                    run_id=run_id,
                )
                state.complete(summary.model_dump())
            except RunLockBusyError as exc:
                state.complete(None, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                state.complete(None, error=f"执行异常：{exc}")
            finally:
                prune_active_runs()

        state.task = asyncio.create_task(_execute())
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}/stream", dependencies=[Depends(require_auth)])
    async def api_stream_run(run_id: str, request: Request) -> StreamingResponse:
        state = active_runs.get(run_id)
        if not state:
            raise HTTPException(status_code=404, detail="未找到对应的执行批次。")

        try:
            last_event_id = int(request.headers.get("last-event-id") or 0)
        except ValueError:
            last_event_id = 0
        history_events, queue = state.subscribe(last_event_id)

        async def event_generator():
            try:
                for item in history_events:
                    yield (
                        f"id: {item['id']}\nevent: {item['type']}\n"
                        f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                    )
                if state.finished:
                    return
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if item is None:
                        break
                    yield (
                        f"id: {item['id']}\nevent: {item['type']}\n"
                        f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                    )
            finally:
                state.unsubscribe(queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/api/runs", dependencies=[Depends(require_auth)])
    async def api_list_runs(
        limit: int = Query(default=30, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return {
            "runs": app_storage.get_runs(limit=limit, offset=offset),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def api_run_detail(run_id: str) -> dict[str, Any]:
        detail = app_storage.get_run_detail(run_id)
        if not detail:
            raise HTTPException(status_code=404, detail="未找到对应的执行批次。")
        return detail

    @app.get("/api/history", dependencies=[Depends(require_auth)])
    async def api_history(
        provider_id: Optional[str] = Query(default=None),
        month: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return app_storage.get_history(
            limit=limit, provider_id=provider_id, month=month, offset=offset
        )

    @app.get("/api/notifications", dependencies=[Depends(require_auth)])
    async def api_notifications(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return {
            "notifications": app_storage.get_notifications(limit=limit, offset=offset),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/maintenance", dependencies=[Depends(require_auth)])
    async def api_maintenance_info() -> dict[str, Any]:
        return await run_in_threadpool(app_storage.get_maintenance_info)

    @app.post("/api/maintenance/cleanup", dependencies=[Depends(require_auth)])
    async def api_maintenance_cleanup(
        req: MaintenanceCleanupRequest,
    ) -> dict[str, Any]:
        if any(not state.finished for state in active_runs.values()):
            raise HTTPException(
                status_code=409,
                detail="已有签到任务正在执行，请稍后再清理。",
            )
        settings = app_storage.get_settings()

        def _do() -> dict[str, Any]:
            with acquire_run_lock(app_runner.lock_path):
                return app_storage.run_maintenance(
                    settings, vacuum=req.vacuum, backup=True
                )

        try:
            result = await run_in_threadpool(_do)
        except RunLockBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"数据清理失败：{exc}"
            ) from exc
        return {"ok": True, **result}

    @app.get("/api/settings", dependencies=[Depends(require_auth)])
    async def api_get_settings() -> dict[str, Any]:
        providers = []
        for p in app_providers:
            cfg = app_storage.get_provider_config(p.id)
            providers.append(
                {
                    "provider_id": p.id,
                    "display_name": p.display_name,
                    "description": p.description,
                    "supports_browser_auth": p.supports_browser_auth,
                    "supports_streak": p.supports_streak,
                    "fixed_business_day_start": p.fixed_business_day_start,
                    **cfg.model_dump(),
                }
            )
        return {
            "global": app_storage.get_settings().model_dump(),
            "providers": providers,
            "schedule": await schedule_status_cached(),
        }

    @app.put("/api/settings", dependencies=[Depends(require_auth)])
    async def api_put_settings(payload: GlobalSettings) -> dict[str, Any]:
        saved = app_storage.save_settings(payload)
        return {"global": saved.model_dump()}

    @app.put("/api/providers/{provider_id}/config", dependencies=[Depends(require_auth)])
    async def api_update_provider_config(
        provider_id: str, req: ProviderConfigUpdateRequest
    ) -> dict[str, Any]:
        try:
            provider = resolve_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        cfg = app_storage.get_provider_config(provider_id)
        if req.enabled is not None:
            cfg.enabled = req.enabled
        if req.earliest_time is not None:
            cfg.earliest_time = req.earliest_time.strip()
        if req.business_day_start is not None:
            requested_start = req.business_day_start.strip()
            if (
                provider.fixed_business_day_start
                and requested_start != provider.fixed_business_day_start
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{provider.display_name} 的业务日起始时间固定为 "
                        f"{provider.fixed_business_day_start}，不可修改。"
                    ),
                )
            cfg.business_day_start = requested_start
        if req.checkin_credits is not None:
            cfg.checkin_credits = req.checkin_credits

        extra = dict(cfg.extra)
        if req.jitter_override is False:
            extra.pop("jitter_override", None)
        if req.jitter_minutes is not None:
            cfg.jitter_minutes = req.jitter_minutes
            extra["jitter_override"] = True
        elif req.jitter_override is True:
            extra["jitter_override"] = True
        for field_name in (
            "auth_file_path",
            "client_exe_path",
            "browser_state_path",
            "base_url",
        ):
            val = getattr(req, field_name)
            if val is not None:
                cleaned = val.strip()
                if cleaned:
                    extra[field_name] = cleaned
                else:
                    extra.pop(field_name, None)
        cfg.extra = extra

        saved = app_storage.save_provider_config(cfg)
        return {"provider": saved.model_dump()}

    @app.get(
        "/api/providers/qoder/pat/status", dependencies=[Depends(require_auth)]
    )
    async def api_qoder_pat_status() -> dict[str, Any]:
        provider = resolve_qoder_provider()
        cfg = app_storage.get_provider_config("qoder")
        return {"ok": True, **provider.pat_status(cfg)}

    @app.put("/api/providers/qoder/pat", dependencies=[Depends(require_auth)])
    async def api_save_qoder_pat(req: QoderPatRequest) -> dict[str, Any]:
        provider = resolve_qoder_provider()
        cleaned = (req.pat or "").strip()
        if not cleaned or len(cleaned) < 8 or len(cleaned) > 8192:
            raise HTTPException(
                status_code=400, detail="PAT 不能为空，且长度需在 8-8192 字符之间。"
            )
        if any(ch.isspace() for ch in cleaned):
            raise HTTPException(status_code=400, detail="PAT 不能包含空白字符。")
        try:
            await run_in_threadpool(
                save_secure_json, get_qoder_pat_path(), {"pat": cleaned}
            )
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"保存 PAT 失败：{exc}"
            ) from exc
        cfg = app_storage.get_provider_config("qoder")
        return {"ok": True, **provider.pat_status(cfg)}

    @app.delete("/api/providers/qoder/pat", dependencies=[Depends(require_auth)])
    async def api_clear_qoder_pat() -> dict[str, Any]:
        provider = resolve_qoder_provider()
        path = get_qoder_pat_path()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"清除 PAT 失败：{exc}"
            ) from exc
        cfg = app_storage.get_provider_config("qoder")
        return {"ok": True, **provider.pat_status(cfg)}

    @app.post("/api/providers/{provider_id}/launch-client", dependencies=[Depends(require_auth)])
    async def api_launch_client(provider_id: str) -> dict[str, Any]:
        try:
            provider = resolve_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        cfg = app_storage.get_provider_config(provider_id)
        ok, msg = provider.launch_client(cfg)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"ok": True, "message": msg}

    @app.post("/api/providers/{provider_id}/auth-browser", dependencies=[Depends(require_auth)])
    async def api_auth_browser(provider_id: str) -> dict[str, Any]:
        try:
            provider = resolve_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        existing = auth_tasks.get(provider_id)
        if existing and existing.status == "running":
            return {
                "ok": True,
                "status": "running",
                "message": existing.message,
            }

        cfg = app_storage.get_provider_config(provider_id)
        state = AuthTaskState(provider_id=provider_id)
        auth_tasks[provider_id] = state

        async def _run() -> None:
            try:
                ok, msg = await provider.authorize_browser(cfg)
                state.status = "success" if ok else "error"
                state.message = msg
            except Exception as exc:  # noqa: BLE001
                state.status = "error"
                state.message = f"浏览器授权异常：{exc}"
            finally:
                state.finished_at = time.monotonic()

        state.task = asyncio.create_task(_run())
        return {
            "ok": True,
            "status": "running",
            "message": "已打开 Microsoft Edge，请在弹出窗口中完成登录。",
        }

    @app.get(
        "/api/providers/{provider_id}/auth-browser/status",
        dependencies=[Depends(require_auth)],
    )
    async def api_auth_browser_status(provider_id: str) -> dict[str, Any]:
        try:
            resolve_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        state = auth_tasks.get(provider_id)
        if not state:
            return {
                "provider_id": provider_id,
                "status": "idle",
                "message": "尚未开始浏览器授权。",
            }
        return {
            "provider_id": provider_id,
            "status": state.status,
            "message": state.message,
        }

    @app.post("/api/notifiers/test", dependencies=[Depends(require_auth)])
    async def api_test_notifier() -> dict[str, Any]:
        ok, err = app_runner.notifier.send(
            "GetEggs 通知测试",
            "Windows 系统弹窗通知已连通，后续签到结果将通过此方式提醒。",
            level="info",
        )
        app_storage.save_notification(
            channel=app_runner.notifier.channel_id,
            level="info",
            title="GetEggs 通知测试",
            message="手动触发弹窗测试",
            success=ok,
            error_message=err,
        )
        if not ok:
            raise HTTPException(status_code=500, detail=err or "发送弹窗通知失败。")
        return {"ok": True, "message": "已发送 Windows 系统弹窗，请留意右下角通知中心。"}

    @app.get("/api/schedule", dependencies=[Depends(require_auth)])
    async def api_schedule_status() -> dict[str, Any]:
        return await schedule_status_cached()

    @app.post("/api/schedule/install", dependencies=[Depends(require_auth)])
    async def api_schedule_install() -> dict[str, Any]:
        settings = app_storage.get_settings()
        configs = [app_storage.get_provider_config(p.id) for p in app_providers]
        timeout_minutes = CheckinRunner.estimate_task_timeout_minutes(
            settings, configs
        )
        try:
            ok, msg = await run_in_threadpool(
                install_schedule,
                settings.schedule_time,
                catchup_on_logon=settings.auto_catchup_on_logon,
                timeout_minutes=timeout_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=500, detail=msg)
        schedule_cache["value"] = None
        return {
            "ok": True,
            "message": msg,
            "schedule": await schedule_status_cached(),
        }

    @app.delete("/api/schedule", dependencies=[Depends(require_auth)])
    async def api_schedule_uninstall() -> dict[str, Any]:
        ok, msg = await run_in_threadpool(uninstall_schedule)
        if not ok:
            raise HTTPException(status_code=500, detail=msg)
        schedule_cache["value"] = None
        return {
            "ok": True,
            "message": msg,
            "schedule": await schedule_status_cached(),
        }

    @app.post("/api/system/shutdown", dependencies=[Depends(require_auth)])
    async def api_shutdown() -> dict[str, Any]:
        busy = any(not state.finished for state in active_runs.values())
        if not busy:
            busy = await run_in_threadpool(runner_lock_busy)
        if busy:
            raise HTTPException(
                status_code=409,
                detail="已有签到任务正在执行（含计划任务），请等待完成后再关闭 Web 服务。",
            )
        cleanup = getattr(app.state, "ui_state_cleanup", None)
        if callable(cleanup):
            cleanup()

        async def _stop() -> None:
            await asyncio.sleep(0.3)
            os.kill(os.getpid(), signal.SIGINT)

        app.state.shutdown_task = asyncio.create_task(_stop())
        return {"ok": True, "message": "正在关闭本地 Web 服务，定时计划任务不受影响。"}

    return app
