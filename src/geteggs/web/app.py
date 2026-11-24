"""FastAPI local Web UI server with SSE real-time check-in logs."""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import signal
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from geteggs import __version__
from geteggs.core.config import (
    get_qoder_pat_path,
    today_biz_date,
    validate_local_file_path,
    validate_provider_base_url,
)
from geteggs.core.lock import RunLockBusyError, acquire_run_lock, is_lock_busy
from geteggs.core.models import (
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
    jitter_override: Optional[bool] = None
    auth_file_path: Optional[str] = None
    client_exe_path: Optional[str] = None
    browser_state_path: Optional[str] = None
    base_url: Optional[str] = None
    allow_token_refresh: Optional[bool] = None

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


class MaintenanceCleanupRequest(BaseModel):
    vacuum: bool = False


class QoderPatRequest(BaseModel):
    pat: str


@dataclass
class ActiveRunState:
    run_id: str
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=2000))
    subscribers: list[asyncio.Queue[Optional[dict[str, Any]]]] = field(default_factory=list)
    finished: bool = False
    summary: Optional[dict[str, Any]] = None
    seq: int = 0
    created_at: float = field(default_factory=time.monotonic)
    task: Optional[asyncio.Task[None]] = None

    @staticmethod
    def _enqueue(
        q: asyncio.Queue[Optional[dict[str, Any]]],
        item: Optional[dict[str, Any]],
    ) -> None:
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        item = {"id": self.seq, "type": event_type, "data": payload}
        self.events.append(item)
        for q in list(self.subscribers):
            self._enqueue(q, item)

    def complete(
        self,
        summary: Optional[dict[str, Any]],
        error: Optional[str] = None,
        code: Optional[str] = None,
    ) -> None:
        self.finished = True
        self.summary = summary
        if error:
            payload: dict[str, Any] = {"message": error}
            if code:
                payload["code"] = code
            self.publish("error", payload)
        else:
            self.publish("done", summary or {})
        for q in list(self.subscribers):
            self._enqueue(q, None)

    def subscribe(
        self, last_event_id: int = 0
    ) -> tuple[list[dict[str, Any]], Optional[asyncio.Queue[Optional[dict[str, Any]]]], bool]:
        history = [item for item in self.events if item["id"] > last_event_id]
        if self.finished:
            return history, None, True
        q: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue(maxsize=256)
        self.subscribers.append(q)
        return history, q, False

    def unsubscribe(
        self, q: Optional[asyncio.Queue[Optional[dict[str, Any]]]]
    ) -> None:
        if q is not None and q in self.subscribers:
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
    schedule_cache: dict[str, Any] = {"value": None, "expires_at": 0.0, "gen": 0}
    schedule_lock = asyncio.Lock()
    config_lock = asyncio.Lock()
    shutdown_lock = asyncio.Lock()
    shutting_down = False
    index_html_cache: Optional[str] = None

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
        cached_val = schedule_cache["value"]
        if cached_val is not None and now < schedule_cache["expires_at"]:
            return cached_val
        async with schedule_lock:
            now = time.monotonic()
            cached_val = schedule_cache["value"]
            if cached_val is not None and now < schedule_cache["expires_at"]:
                return cached_val
            gen_before = schedule_cache["gen"]
            result = await run_in_threadpool(get_schedule_status)
            if schedule_cache["gen"] == gen_before:
                ttl = 3.0 if result.get("query_failed") else 45.0
                schedule_cache["value"] = result
                schedule_cache["expires_at"] = time.monotonic() + ttl
            return result

    def runner_lock_busy() -> bool:
        """Return True when a check-in run (including scheduled ones) holds the lock."""
        return is_lock_busy(app_runner.lock_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            for run_state in list(active_runs.values()):
                for q in list(run_state.subscribers):
                    ActiveRunState._enqueue(q, None)
            pending: list[asyncio.Task[None]] = []
            for run_state in list(active_runs.values()):
                if run_state.task is not None and not run_state.task.done():
                    run_state.task.cancel()
                    pending.append(run_state.task)
            for auth_state in list(auth_tasks.values()):
                if auth_state.task is not None and not auth_state.task.done():
                    auth_state.task.cancel()
                    pending.append(auth_state.task)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(
        title="GetEggs Local Console",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.auth_manager = auth_mgr
    app.state.storage = app_storage
    app.state.runner = app_runner
    app.state.providers = app_providers
    app.state.auth_tasks = auth_tasks

    _CSP_POLICY = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )

    @app.middleware("http")
    async def local_origin_guard(request: Request, call_next: Any) -> Any:
        if not is_local_origin(request):
            resp: Any = JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "仅允许本机来源访问 GetEggs 控制台。"},
            )
        elif auth_mgr.enabled and posixpath.normpath(request.url.path).lower() == "/static/index.html":
            try:
                auth_mgr.verify_request(request)
            except HTTPException as exc:
                resp = JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                )
            else:
                resp = await call_next(request)
        else:
            resp = await call_next(request)

        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy", _CSP_POLICY)
        if request.url.path == "/auth" or request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

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
        nonlocal index_html_cache
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
        if index_html_cache is None:
            index_html_cache = await run_in_threadpool(
                (STATIC_DIR / "index.html").read_text, encoding="utf-8"
            )
        return HTMLResponse(content=index_html_cache)

    @app.get("/api/overview", dependencies=[Depends(require_auth)])
    async def api_overview() -> dict[str, Any]:
        # Never block the first paint on a Task Scheduler query. The UI fetches
        # /api/schedule asynchronously when no cached value is available yet.
        schedule_info = schedule_cache["value"]
        schedule_stale = bool(
            schedule_info is not None
            and time.monotonic() >= schedule_cache["expires_at"]
        )

        def _build_overview() -> dict[str, Any]:
            settings = app_storage.get_settings()
            biz_date = today_biz_date(settings.timezone)
            cards: list[dict[str, Any]] = []

            for provider in app_providers:
                cfg = app_storage.get_provider_config(provider.id)
                # Align the card with the provider's own business day (Qoder resets
                # at 10:00), so early-morning runs show the previous business day.
                provider_biz_date = provider.business_date(cfg, settings.timezone)
                today_rec = app_storage.get_today_summary(provider.id, provider_biz_date)
                latest_rec = app_storage.get_latest_record(provider.id)

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
                        "fixed_business_day_start": provider.fixed_business_day_start,
                        "today_record": today_rec,
                        "latest_record": latest_rec,
                    }
                )

            return {
                "version": __version__,
                "biz_date": biz_date,
                "providers": cards,
                "schedule": schedule_info,
                "schedule_stale": schedule_stale,
            }

        return await run_in_threadpool(_build_overview)

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
        if shutting_down:
            raise HTTPException(
                status_code=503, detail="本地 Web 服务正在关闭，无法启动新的签到任务。"
            )
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
                state.complete(None, error=str(exc), code="run_busy")
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
        history_events, queue, was_finished = state.subscribe(last_event_id)

        async def event_generator():
            try:
                for item in history_events:
                    yield (
                        f"id: {item['id']}\nevent: {item['type']}\n"
                        f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                    )
                if was_finished or queue is None:
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

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/runs", dependencies=[Depends(require_auth)])
    async def api_list_runs(
        limit: int = Query(default=30, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        runs = await run_in_threadpool(app_storage.get_runs, limit=limit, offset=offset)
        return {
            "runs": runs,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def api_run_detail(run_id: str) -> dict[str, Any]:
        detail = await run_in_threadpool(app_storage.get_run_detail, run_id)
        if not detail:
            raise HTTPException(status_code=404, detail="未找到对应的执行批次。")
        return detail

    @app.get("/api/history", dependencies=[Depends(require_auth)])
    async def api_history(
        provider_id: Optional[str] = Query(default=None),
        month: Optional[str] = Query(
            default=None, pattern=r"^\d{4}-(?:0[1-9]|1[0-2])$"
        ),
        biz_date: Optional[str] = Query(
            default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return await run_in_threadpool(
            app_storage.get_history,
            limit=limit,
            provider_id=provider_id,
            month=month,
            offset=offset,
            biz_date=biz_date,
        )

    @app.get("/api/notifications", dependencies=[Depends(require_auth)])
    async def api_notifications(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        items = await run_in_threadpool(
            app_storage.get_notifications, limit=limit, offset=offset
        )
        return {
            "notifications": items,
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
        if shutting_down:
            raise HTTPException(
                status_code=503, detail="本地 Web 服务正在关闭，无法执行数据清理。"
            )
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
        def _load() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            providers = []
            for p in app_providers:
                cfg = app_storage.get_provider_config(p.id)
                providers.append(
                    {
                        "provider_id": p.id,
                        "display_name": p.display_name,
                        "description": p.description,
                        "supports_browser_auth": p.supports_browser_auth,
                        "fixed_business_day_start": p.fixed_business_day_start,
                        **cfg.model_dump(),
                    }
                )
            return app_storage.get_settings().model_dump(), providers

        global_cfg, providers = await run_in_threadpool(_load)
        return {
            "global": global_cfg,
            "providers": providers,
            "schedule": await schedule_status_cached(),
        }

    @app.put("/api/settings", dependencies=[Depends(require_auth)])
    async def api_put_settings(payload: GlobalSettings) -> dict[str, Any]:
        saved = await run_in_threadpool(app_storage.save_settings, payload)
        return {"global": saved.model_dump()}

    @app.put("/api/providers/{provider_id}/config", dependencies=[Depends(require_auth)])
    async def api_update_provider_config(
        provider_id: str, req: ProviderConfigUpdateRequest
    ) -> dict[str, Any]:
        try:
            provider = resolve_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        async with config_lock:
            cfg = await run_in_threadpool(app_storage.get_provider_config, provider_id)
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

            extra = dict(cfg.extra)
            if req.jitter_minutes is not None:
                cfg.jitter_minutes = req.jitter_minutes
            if req.jitter_override is False:
                extra.pop("jitter_override", None)
            elif req.jitter_override is True or req.jitter_minutes is not None:
                extra["jitter_override"] = True
            path_specs: dict[str, tuple[str, dict[str, bool]]] = {
                "auth_file_path": ("登录态文件路径", {}),
                "client_exe_path": ("客户端可执行文件路径", {"require_exe_on_windows": True}),
                "browser_state_path": (
                    "浏览器会话文件路径",
                    {"restrict_to_data_dir": True, "require_json": True},
                ),
            }
            for field_name, (label, opts) in path_specs.items():
                val = getattr(req, field_name)
                if val is not None:
                    try:
                        cleaned = validate_local_file_path(val, field_label=label, **opts)
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail=str(exc)) from exc
                    if cleaned:
                        extra[field_name] = cleaned
                    else:
                        extra.pop(field_name, None)

            if req.base_url is not None:
                try:
                    cleaned_url = validate_provider_base_url(provider_id, req.base_url)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                if cleaned_url:
                    extra["base_url"] = cleaned_url
                else:
                    extra.pop("base_url", None)
            if req.allow_token_refresh is not None:
                extra["allow_token_refresh"] = bool(req.allow_token_refresh)
            cfg.extra = extra

            saved = await run_in_threadpool(app_storage.save_provider_config, cfg)
            return {"provider": saved.model_dump()}

    @app.get(
        "/api/providers/qoder/pat/status", dependencies=[Depends(require_auth)]
    )
    async def api_qoder_pat_status() -> dict[str, Any]:
        provider = resolve_qoder_provider()

        def _status() -> dict[str, Any]:
            cfg = app_storage.get_provider_config("qoder")
            return {"ok": True, **provider.pat_status(cfg)}

        return await run_in_threadpool(_status)

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
        if hasattr(provider, "clear_token_cache"):
            provider.clear_token_cache()

        def _status() -> dict[str, Any]:
            cfg = app_storage.get_provider_config("qoder")
            return {"ok": True, **provider.pat_status(cfg)}

        return await run_in_threadpool(_status)

    @app.delete("/api/providers/qoder/pat", dependencies=[Depends(require_auth)])
    async def api_clear_qoder_pat() -> dict[str, Any]:
        provider = resolve_qoder_provider()
        path = get_qoder_pat_path()
        try:
            await run_in_threadpool(path.unlink, True)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"清除 PAT 失败：{exc}"
            ) from exc
        if hasattr(provider, "clear_token_cache"):
            provider.clear_token_cache()

        def _status() -> dict[str, Any]:
            cfg = app_storage.get_provider_config("qoder")
            return {"ok": True, **provider.pat_status(cfg)}

        return await run_in_threadpool(_status)

    @app.post("/api/providers/{provider_id}/launch-client", dependencies=[Depends(require_auth)])
    async def api_launch_client(provider_id: str) -> dict[str, Any]:
        try:
            provider = resolve_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        def _launch() -> tuple[bool, str]:
            cfg = app_storage.get_provider_config(provider_id)
            return provider.launch_client(cfg)

        ok, msg = await run_in_threadpool(_launch)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"ok": True, "message": msg}

    @app.post("/api/providers/{provider_id}/auth-browser", dependencies=[Depends(require_auth)])
    async def api_auth_browser(provider_id: str) -> dict[str, Any]:
        if shutting_down:
            raise HTTPException(
                status_code=503, detail="本地 Web 服务正在关闭，无法启动浏览器授权。"
            )
        try:
            provider = resolve_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not provider.supports_browser_auth:
            raise HTTPException(
                status_code=400,
                detail=f"{provider.display_name} 暂不支持受控浏览器授权。",
            )

        existing = auth_tasks.get(provider_id)
        if existing and existing.status == "running":
            return {
                "ok": True,
                "status": "running",
                "message": existing.message,
            }

        cfg = await run_in_threadpool(app_storage.get_provider_config, provider_id)
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
        def _send() -> tuple[bool, Optional[str]]:
            ok, err = app_runner.notifier.send(
                "GetEggs 通知测试",
                "Windows 系统弹窗通知已连通，后续签到结果将通过此方式提醒。",
                level="info",
            )
            tz = app_storage.get_settings().timezone
            app_storage.save_notification(
                channel=app_runner.notifier.channel_id,
                level="info",
                title="GetEggs 通知测试",
                message="手动触发弹窗测试",
                success=ok,
                biz_date=today_biz_date(tz),
                error_message=err,
            )
            return ok, err

        ok, err = await run_in_threadpool(_send)
        if not ok:
            raise HTTPException(status_code=500, detail=err or "发送弹窗通知失败。")
        return {"ok": True, "message": "已发送 Windows 系统弹窗，请留意右下角通知中心。"}

    @app.get("/api/schedule", dependencies=[Depends(require_auth)])
    async def api_schedule_status() -> dict[str, Any]:
        return await schedule_status_cached()

    @app.post("/api/schedule/install", dependencies=[Depends(require_auth)])
    async def api_schedule_install() -> dict[str, Any]:
        async with schedule_lock:
            def _load_schedule_args() -> tuple[str, bool, int]:
                settings = app_storage.get_settings()
                configs = [app_storage.get_provider_config(p.id) for p in app_providers]
                return (
                    settings.schedule_time,
                    settings.auto_catchup_on_logon,
                    CheckinRunner.estimate_task_timeout_minutes(settings, configs),
                )

            schedule_time, catchup_on_logon, timeout_minutes = await run_in_threadpool(
                _load_schedule_args
            )
            try:
                ok, msg = await run_in_threadpool(
                    install_schedule,
                    schedule_time,
                    catchup_on_logon=catchup_on_logon,
                    timeout_minutes=timeout_minutes,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not ok:
                raise HTTPException(status_code=500, detail=msg)
            schedule_cache["gen"] += 1
            schedule_cache["value"] = None
            schedule_cache["expires_at"] = 0.0
        return {
            "ok": True,
            "message": msg,
            "schedule": await schedule_status_cached(),
        }

    @app.delete("/api/schedule", dependencies=[Depends(require_auth)])
    async def api_schedule_uninstall() -> dict[str, Any]:
        async with schedule_lock:
            ok, msg = await run_in_threadpool(uninstall_schedule)
            if not ok:
                raise HTTPException(status_code=500, detail=msg)
            schedule_cache["gen"] += 1
            schedule_cache["value"] = None
            schedule_cache["expires_at"] = 0.0
        return {
            "ok": True,
            "message": msg,
            "schedule": await schedule_status_cached(),
        }

    @app.post("/api/system/shutdown", dependencies=[Depends(require_auth)])
    async def api_shutdown() -> dict[str, Any]:
        nonlocal shutting_down
        async with shutdown_lock:
            if shutting_down:
                return {"ok": True, "message": "本地 Web 服务正在关闭中。"}
            busy = any(not state.finished for state in active_runs.values())
            if not busy:
                busy = any(state.status == "running" for state in auth_tasks.values())
            if not busy:
                busy = await run_in_threadpool(runner_lock_busy)
            if not busy:
                busy = any(not state.finished for state in active_runs.values()) or any(
                    state.status == "running" for state in auth_tasks.values()
                )
            if busy:
                raise HTTPException(
                    status_code=409,
                    detail="已有签到或授权任务正在执行（含计划任务），请等待完成后再关闭 Web 服务。",
                )
            shutting_down = True
            cleanup = getattr(app.state, "ui_state_cleanup", None)
            if callable(cleanup):
                cleanup()

        async def _stop() -> None:
            await asyncio.sleep(0.3)
            for run_state in list(active_runs.values()):
                for q in list(run_state.subscribers):
                    ActiveRunState._enqueue(q, None)
            server = getattr(app.state, "uvicorn_server", None)
            if server is not None:
                server.should_exit = True
                return
            if os.name != "nt":
                os.kill(os.getpid(), signal.SIGTERM)

        app.state.shutdown_task = asyncio.create_task(_stop())
        return {"ok": True, "message": "正在关闭本地 Web 服务，定时计划任务不受影响。"}

    return app
