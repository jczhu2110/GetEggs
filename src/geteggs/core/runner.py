"""Core check-in runner: jitter, idempotency, circuit breaker, locking, and notifications."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import random
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

from geteggs.core.config import get_lock_path, get_timezone, today_biz_date
from geteggs.core.lock import RunLockBusyError, acquire_run_lock
from geteggs.core.models import (
    CheckinResult,
    CheckinStatus,
    GlobalSettings,
    LogEvent,
    ProviderConfig,
    RunSummary,
    TriggerType,
    utc_now_iso,
)
from geteggs.core.storage import Storage
from geteggs.notifiers.base import BaseNotifier
from geteggs.notifiers.windows_toast import WindowsToastNotifier
from geteggs.providers.base import BaseProvider
from geteggs.providers.registry import get_provider, list_providers

EventSink = Callable[[LogEvent], None]
PROVIDER_TIMEOUT_SECONDS = 90.0


class CheckinRunner:
    """Orchestrates check-in execution across providers with persistence and notifications."""

    def __init__(
        self,
        storage: Optional[Storage] = None,
        notifier: Optional[BaseNotifier] = None,
        lock_path: Optional[Path] = None,
        provider_timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        self.storage = storage or Storage()
        self.notifier = notifier or WindowsToastNotifier()
        self.lock_path = lock_path or get_lock_path()
        self.provider_timeout_seconds = provider_timeout_seconds

    @staticmethod
    def _is_before_earliest(earliest_hhmm: str, tz_name: str) -> bool:
        if not earliest_hhmm or earliest_hhmm == "00:00":
            return False
        try:
            hour, minute = (int(x) for x in earliest_hhmm.split(":", 1))
        except ValueError:
            return False
        now = datetime.now(get_timezone(tz_name))
        return (now.hour, now.minute) < (hour, minute)

    @staticmethod
    def _effective_jitter_minutes(
        cfg: ProviderConfig, settings: GlobalSettings
    ) -> int:
        if cfg.extra.get("jitter_override"):
            return max(0, cfg.jitter_minutes)
        return max(0, settings.jitter_minutes)

    @staticmethod
    def estimate_task_timeout_minutes(
        settings: GlobalSettings, configs: list[ProviderConfig]
    ) -> int:
        """Bound scheduled runtime by the parallel max jitter window plus execution budget."""
        enabled_configs = [cfg for cfg in configs if cfg.enabled]
        max_jitter = max(
            (
                CheckinRunner._effective_jitter_minutes(cfg, settings)
                for cfg in enabled_configs
            ),
            default=0,
        )
        # Jitter offsets run on an overlapping timeline rather than summing serially.
        # Reserve ~2 minutes per enabled provider (90s timeout + overhead) plus buffer.
        exec_budget = max(5, len(enabled_configs) * 2 + 5)
        return max(15, min(1440, max_jitter + exec_budget))

    def _maybe_run_maintenance(
        self, settings: GlobalSettings, emit: Callable[[str, str], None]
    ) -> None:
        try:
            cleanup_due, vacuum_due = self.storage.maintenance_due(settings)
            if not cleanup_due:
                return
            result = self.storage.run_maintenance(
                settings, vacuum=vacuum_due, backup=True
            )
            deleted = result.get("deleted", {})
            changed = ", ".join(
                f"{key}={value}" for key, value in deleted.items() if value
            )
            message = "自动维护完成"
            if changed:
                message += f"：{changed}"
            if result.get("backup"):
                message += f"；备份：{result['backup']}"
            if result.get("vacuum_error"):
                message += f"；VACUUM 失败：{result['vacuum_error']}"
            emit("info", message)
        except Exception as exc:  # noqa: BLE001
            emit("warning", f"自动维护失败：{exc}")

    def _notify_for_results(
        self,
        results: list[CheckinResult],
        biz_date: str,
        *,
        notify: bool,
        dry_run: bool,
    ) -> None:
        if not notify or dry_run or not results:
            return

        settings = self.storage.get_settings()
        if not settings.notify_toast_enabled:
            return

        success_items = [r for r in results if r.status == CheckinStatus.SUCCESS]
        already_items = [
            r
            for r in results
            if r.status == CheckinStatus.ALREADY_DONE and r.biz_date == biz_date
        ]
        action_items = [r for r in results if r.status.requires_user_action]
        failed_items = [r for r in results if r.status == CheckinStatus.FAILED]

        # Action-required alerts are deduplicated once per provider/status/day so users aren't spammed.
        for item in action_items:
            dedup_key = f"alert:{item.provider_id}:{item.status.value}"
            if self.storage.was_notified_today(dedup_key, biz_date):
                continue
            title = f"{item.display_name} 需要手动处理"
            ok, err = self.notifier.send(title, item.message, level="warning")
            self.storage.save_notification(
                channel=self.notifier.channel_id,
                level="warning",
                title=title,
                message=item.message,
                success=ok,
                biz_date=biz_date,
                dedup_key=dedup_key,
                error_message=err,
            )

        def _send_once(
            dedup_key: str, title: str, body: str, level: str
        ) -> None:
            if self.storage.was_notified_today(dedup_key, biz_date):
                return
            ok, err = self.notifier.send(title, body, level=level)
            self.storage.save_notification(
                channel=self.notifier.channel_id,
                level=level,
                title=title,
                message=body,
                success=ok,
                biz_date=biz_date,
                dedup_key=dedup_key,
                error_message=err,
            )

        if success_items:
            lines = [f"{r.display_name} 签到成功" for r in success_items]
            # 业务日可能不同于批次日期（例如补领上一周期），纳入去重键避免
            # 吞掉同一天后续新业务日的成功通知。
            provider_key = ",".join(
                sorted({f"{r.provider_id}@{r.biz_date}" for r in success_items})
            )
            _send_once(
                f"daily_success:{biz_date}:{provider_key}",
                "GetEggs 今日签到完成",
                "；".join(lines),
                "info",
            )

        if failed_items:
            provider_key = ",".join(sorted({r.provider_id for r in failed_items}))
            _send_once(
                f"daily_failure:{biz_date}:{provider_key}",
                "GetEggs 签到异常",
                "；".join(f"{r.display_name}: {r.message}" for r in failed_items),
                "error",
            )

        if (
            not success_items
            and not failed_items
            and already_items
            and settings.notify_on_already_done
        ):
            provider_key = ",".join(sorted({r.provider_id for r in already_items}))
            _send_once(
                f"daily_already:{biz_date}:{provider_key}",
                "GetEggs 今日状态",
                "；".join(f"{r.display_name} 今日已签" for r in already_items),
                "info",
            )

    @asynccontextmanager
    async def _acquire_run_lock_async(
        self,
        *,
        wait_timeout: float = 0.0,
        poll_interval: float = 0.5,
    ) -> AsyncIterator[None]:
        """Acquire the run lock, optionally waiting briefly if another run is finishing."""
        deadline = time.monotonic() + max(0.0, wait_timeout)
        while True:
            ctx = acquire_run_lock(self.lock_path)
            try:
                ctx.__enter__()
            except RunLockBusyError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                await asyncio.sleep(min(poll_interval, remaining))
                continue
            try:
                yield
            finally:
                ctx.__exit__(None, None, None)
            return

    def _safe_mark_stale_runs(
        self, settings: GlobalSettings, *, exclude_run_id: Optional[str] = None
    ) -> None:
        try:
            self.storage.mark_stale_runs(settings, exclude_run_id=exclude_run_id)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _should_skip_before_earliest(
        cfg: ProviderConfig,
        settings: GlobalSettings,
        *,
        missed_only: bool,
        trigger_type: TriggerType,
    ) -> bool:
        return (
            missed_only
            and trigger_type in (TriggerType.SCHEDULED, TriggerType.MISSED_CATCHUP)
            and CheckinRunner._is_before_earliest(cfg.earliest_time, settings.timezone)
        )

    async def _execute_provider(
        self,
        provider: BaseProvider,
        cfg: ProviderConfig,
        settings: GlobalSettings,
        *,
        actual_run_id: str,
        missed_only: bool,
        dry_run: bool,
        emit: Callable[[str, str, Optional[str]], None],
    ) -> CheckinResult:
        provider_started = time.perf_counter()
        provider_biz_date = provider.business_date(cfg, settings.timezone)
        try:
            existing = self.storage.get_today_completed_record(
                provider.id, provider_biz_date
            )
            if existing and missed_only:
                emit(
                    "info",
                    f"{provider.display_name} 本业务日（{provider_biz_date}）已有完成记录，跳过执行。",
                    provider.id,
                )
                return CheckinResult(
                    provider_id=provider.id,
                    display_name=provider.display_name,
                    status=CheckinStatus.ALREADY_DONE,
                    message="本业务日已完成签到（本地账本缓存命中）。",
                    biz_date=provider_biz_date,
                    dry_run=dry_run,
                )

            emit(
                "info",
                f"开始执行 {provider.display_name} 签到...",
                provider.id,
            )

            def _provider_log(level: str, msg: str, pid: str = provider.id) -> None:
                emit(level, msg, pid)

            try:
                result = await asyncio.wait_for(
                    provider.checkin(
                        cfg,
                        provider_biz_date,
                        dry_run=dry_run,
                        log=_provider_log,
                    ),
                    timeout=self.provider_timeout_seconds,
                )
            except asyncio.TimeoutError:
                result = CheckinResult(
                    provider_id=provider.id,
                    display_name=provider.display_name,
                    status=CheckinStatus.FAILED,
                    message=(
                        f"{provider.display_name} 执行超时"
                        f"（超过 {self.provider_timeout_seconds:g} 秒），"
                        "已跳过本次签到。"
                    ),
                    biz_date=provider_biz_date,
                    duration_ms=int((time.perf_counter() - provider_started) * 1000),
                    dry_run=dry_run,
                    raw_error_type="ProviderTimeout",
                )
            self.storage.save_checkin_result(actual_run_id, result)

            lvl = "info" if result.status.is_done_for_today else "warning"
            emit(lvl, result.message, provider.id)

            if not dry_run and result.status.requires_user_action:
                streak_fail = self.storage.count_consecutive_failures(provider.id)
                if streak_fail >= 2:
                    emit(
                        "warning",
                        f"{provider.display_name} 已连续 {streak_fail} 次需要人工处理，建议打开客户端检查登录态。",
                        provider.id,
                    )
            return result
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.perf_counter() - provider_started) * 1000)
            display_name = getattr(provider, "display_name", provider.id)
            result = CheckinResult(
                provider_id=provider.id,
                display_name=display_name,
                status=CheckinStatus.FAILED,
                message=f"{display_name} 适配器执行异常：{exc}",
                biz_date=provider_biz_date,
                duration_ms=duration_ms,
                dry_run=dry_run,
                raw_error_type=type(exc).__name__,
            )
            try:
                self.storage.save_checkin_result(actual_run_id, result)
            except Exception:  # noqa: BLE001
                pass
            emit("error", result.message, provider.id)
            return result

    async def run(
        self,
        *,
        provider_ids: Optional[list[str]] = None,
        trigger_type: TriggerType = TriggerType.MANUAL_CLI,
        missed_only: bool = False,
        apply_jitter: bool = False,
        notify: bool = True,
        dry_run: bool = False,
        event_sink: Optional[EventSink] = None,
        run_id: Optional[str] = None,
        providers_override: Optional[list[BaseProvider]] = None,
    ) -> RunSummary:
        """Execute check-ins for selected providers with lock-free dry-run and lock-free jitter waits."""
        settings = self.storage.get_settings()
        actual_run_id = run_id or uuid.uuid4().hex[:12]
        started_at = utc_now_iso()

        if providers_override is not None:
            targets = providers_override
        elif provider_ids:
            targets = [get_provider(pid) for pid in provider_ids]
        else:
            targets = list_providers()

        results: list[CheckinResult] = []

        def emit(level: str, message: str, provider_id: Optional[str] = None) -> None:
            event = LogEvent(level=level, provider_id=provider_id, message=message)
            try:
                self.storage.save_log_event(actual_run_id, event)
            except Exception:  # noqa: BLE001
                pass
            if event_sink:
                event_sink(event)

        async def _run_batch_inline() -> None:
            self.storage.record_run_start(actual_run_id, trigger_type)
            try:
                for provider in targets:
                    try:
                        cfg = self.storage.get_provider_config(provider.id)
                    except Exception as exc:  # noqa: BLE001
                        display_name = getattr(provider, "display_name", provider.id)
                        result = CheckinResult(
                            provider_id=provider.id,
                            display_name=display_name,
                            status=CheckinStatus.FAILED,
                            message=f"{display_name} 读取配置异常：{exc}",
                            biz_date=today_biz_date(settings.timezone),
                            dry_run=dry_run,
                            raw_error_type=type(exc).__name__,
                        )
                        try:
                            self.storage.save_checkin_result(actual_run_id, result)
                        except Exception:  # noqa: BLE001
                            pass
                        results.append(result)
                        emit("error", result.message, provider.id)
                        continue

                    if not cfg.enabled and not provider_ids:
                        emit(
                            "info",
                            f"跳过已禁用的插件 {provider.display_name}。",
                            provider.id,
                        )
                        continue

                    if self._should_skip_before_earliest(
                        cfg,
                        settings,
                        missed_only=missed_only,
                        trigger_type=trigger_type,
                    ):
                        emit(
                            "info",
                            f"{provider.display_name} 尚未到达最早可执行时间 {cfg.earliest_time}，本次跳过。",
                            provider.id,
                        )
                        continue

                    result = await self._execute_provider(
                        provider,
                        cfg,
                        settings,
                        actual_run_id=actual_run_id,
                        missed_only=missed_only,
                        dry_run=dry_run,
                        emit=emit,
                    )
                    results.append(result)

                notify_biz_date = today_biz_date(settings.timezone)
                self._notify_for_results(
                    results, notify_biz_date, notify=notify, dry_run=dry_run
                )
                self.storage.record_run_finish(actual_run_id, "completed")
                if not dry_run:
                    self._maybe_run_maintenance(settings, emit)
            except asyncio.CancelledError:
                self.storage.record_run_finish(actual_run_id, "interrupted")
                raise
            except Exception:
                self.storage.record_run_finish(actual_run_id, "error")
                raise

        if dry_run:
            # Read-only preflight does not mutate remote state or run maintenance,
            # so it never blocks or gets blocked by the execution lock.
            await _run_batch_inline()
        elif not apply_jitter:
            with acquire_run_lock(self.lock_path):
                self._safe_mark_stale_runs(settings)
                await _run_batch_inline()
        else:
            # Scheduled jittered runs compute overlapping offsets and sleep WITHOUT
            # holding runner.lock so manual check-ins and UI actions stay responsive.
            self.storage.record_run_start(actual_run_id, trigger_type)
            try:
                scheduled_items: list[tuple[int, int, BaseProvider]] = []
                for idx, provider in enumerate(targets):
                    try:
                        cfg = self.storage.get_provider_config(provider.id)
                        provider_biz_date = provider.business_date(
                            cfg, settings.timezone
                        )
                    except Exception as exc:  # noqa: BLE001
                        display_name = getattr(provider, "display_name", provider.id)
                        result = CheckinResult(
                            provider_id=provider.id,
                            display_name=display_name,
                            status=CheckinStatus.FAILED,
                            message=f"{display_name} 读取配置异常：{exc}",
                            biz_date=today_biz_date(settings.timezone),
                            dry_run=False,
                            raw_error_type=type(exc).__name__,
                        )
                        try:
                            self.storage.save_checkin_result(actual_run_id, result)
                        except Exception:  # noqa: BLE001
                            pass
                        results.append(result)
                        emit("error", result.message, provider.id)
                        continue

                    if not cfg.enabled and not provider_ids:
                        emit(
                            "info",
                            f"跳过已禁用的插件 {provider.display_name}。",
                            provider.id,
                        )
                        continue

                    if self._should_skip_before_earliest(
                        cfg,
                        settings,
                        missed_only=missed_only,
                        trigger_type=trigger_type,
                    ):
                        emit(
                            "info",
                            f"{provider.display_name} 尚未到达最早可执行时间 {cfg.earliest_time}，本次跳过。",
                            provider.id,
                        )
                        continue

                    existing = self.storage.get_today_completed_record(
                        provider.id, provider_biz_date
                    )
                    if existing and missed_only:
                        emit(
                            "info",
                            f"{provider.display_name} 本业务日（{provider_biz_date}）已有完成记录，跳过执行。",
                            provider.id,
                        )
                        results.append(
                            CheckinResult(
                                provider_id=provider.id,
                                display_name=provider.display_name,
                                status=CheckinStatus.ALREADY_DONE,
                                message="本业务日已完成签到（本地账本缓存命中）。",
                                biz_date=provider_biz_date,
                                dry_run=False,
                            )
                        )
                        continue

                    jitter_minutes = self._effective_jitter_minutes(cfg, settings)
                    offset_sec = (
                        random.randint(0, jitter_minutes * 60)
                        if jitter_minutes > 0
                        else 0
                    )
                    scheduled_items.append((offset_sec, idx, provider))

                scheduled_items.sort(key=lambda item: (item[0], item[1]))
                for offset_sec, _idx, provider in scheduled_items:
                    if offset_sec > 0:
                        emit(
                            "info",
                            f"{provider.display_name} 计划错峰等待 {offset_sec} 秒（等待期间不阻塞手动签到）...",
                            provider.id,
                        )

                schedule_origin = time.monotonic()
                lock_wait = self.provider_timeout_seconds + 5.0
                for offset_sec, _idx, provider in scheduled_items:
                    remaining = offset_sec - (time.monotonic() - schedule_origin)
                    if remaining > 0:
                        await asyncio.sleep(remaining)

                    try:
                        async with self._acquire_run_lock_async(
                            wait_timeout=lock_wait
                        ):
                            self._safe_mark_stale_runs(
                                settings, exclude_run_id=actual_run_id
                            )
                            cfg = self.storage.get_provider_config(provider.id)
                            if not cfg.enabled and not provider_ids:
                                emit(
                                    "info",
                                    f"等待期间插件 {provider.display_name} 已被禁用，跳过执行。",
                                    provider.id,
                                )
                                continue
                            result = await self._execute_provider(
                                provider,
                                cfg,
                                settings,
                                actual_run_id=actual_run_id,
                                missed_only=missed_only,
                                dry_run=False,
                                emit=emit,
                            )
                            results.append(result)
                    except RunLockBusyError as exc:
                        display_name = getattr(provider, "display_name", provider.id)
                        fallback_date = today_biz_date(settings.timezone)
                        result = CheckinResult(
                            provider_id=provider.id,
                            display_name=display_name,
                            status=CheckinStatus.FAILED,
                            message=f"{display_name} 等待执行锁超时：{exc}",
                            biz_date=fallback_date,
                            dry_run=False,
                            raw_error_type="RunLockBusyError",
                        )
                        try:
                            self.storage.save_checkin_result(actual_run_id, result)
                        except Exception:  # noqa: BLE001
                            pass
                        results.append(result)
                        emit("warning", result.message, provider.id)

                notify_biz_date = today_biz_date(settings.timezone)
                self._notify_for_results(
                    results, notify_biz_date, notify=notify, dry_run=False
                )
                self.storage.record_run_finish(actual_run_id, "completed")
                try:
                    with acquire_run_lock(self.lock_path):
                        self._maybe_run_maintenance(settings, emit)
                except RunLockBusyError:
                    emit("info", "当前有其他签到任务正在执行，本次跳过自动维护。")
            except asyncio.CancelledError:
                self.storage.record_run_finish(actual_run_id, "interrupted")
                raise
            except Exception:
                self.storage.record_run_finish(actual_run_id, "error")
                raise

        return RunSummary(
            run_id=actual_run_id,
            trigger_type=trigger_type,
            started_at=started_at,
            finished_at=utc_now_iso(),
            results=results,
        )
