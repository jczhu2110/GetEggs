"""Core check-in runner: jitter, idempotency, circuit breaker, locking, and notifications."""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from geteggs.core.config import get_lock_path, get_timezone, today_biz_date
from geteggs.core.lock import acquire_run_lock
from geteggs.core.models import (
    CheckinResult,
    CheckinStatus,
    CreditSnapshot,
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


class CheckinRunner:
    """Orchestrates check-in execution across providers with persistence and notifications."""

    def __init__(
        self,
        storage: Optional[Storage] = None,
        notifier: Optional[BaseNotifier] = None,
        lock_path: Optional[Path] = None,
    ) -> None:
        self.storage = storage or Storage()
        self.notifier = notifier or WindowsToastNotifier()
        self.lock_path = lock_path or get_lock_path()

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
        """Bound scheduled runtime by the sequential per-provider jitter budget."""
        total_jitter = sum(
            CheckinRunner._effective_jitter_minutes(cfg, settings)
            for cfg in configs
            if cfg.enabled
        )
        return max(25, min(300, 10 + total_jitter))

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
            lines = []
            for r in success_items:
                delta_str = f" +{r.snapshot.delta}" if r.snapshot and r.snapshot.delta > 0 else ""
                lines.append(f"{r.display_name} 签到成功{delta_str}")
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
        """Execute check-ins for selected providers under an exclusive file lock."""
        settings = self.storage.get_settings()
        biz_date = today_biz_date(settings.timezone)
        actual_run_id = run_id or uuid.uuid4().hex[:12]
        started_at = utc_now_iso()

        if providers_override is not None:
            targets = providers_override
        elif provider_ids:
            targets = [get_provider(pid) for pid in provider_ids]
        else:
            targets = list_providers()

        with acquire_run_lock(self.lock_path):
            self.storage.record_run_start(actual_run_id, trigger_type)
            results: list[CheckinResult] = []

            def emit(level: str, message: str, provider_id: Optional[str] = None) -> None:
                event = LogEvent(level=level, provider_id=provider_id, message=message)
                try:
                    self.storage.save_log_event(actual_run_id, event)
                except Exception:  # noqa: BLE001
                    pass
                if event_sink:
                    event_sink(event)

            try:
                for provider in targets:
                    provider_started = time.perf_counter()
                    try:
                        cfg = self.storage.get_provider_config(provider.id)
                        provider_biz_date = provider.business_date(
                            cfg, settings.timezone
                        )
                        if not cfg.enabled and not provider_ids:
                            emit(
                                "info",
                                f"跳过已禁用的插件 {provider.display_name}。",
                                provider.id,
                            )
                            continue

                        if (
                            missed_only
                            and trigger_type == TriggerType.MISSED_CATCHUP
                            and self._is_before_earliest(cfg.earliest_time, settings.timezone)
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
                                f"{provider.display_name} 本业务日（{provider_biz_date}）已有完成记录，跳过补跑。",
                                provider.id,
                            )
                            results.append(
                                CheckinResult(
                                    provider_id=provider.id,
                                    display_name=provider.display_name,
                                    status=CheckinStatus.ALREADY_DONE,
                                    message="本业务日已完成签到（本地账本缓存命中）。",
                                    biz_date=provider_biz_date,
                                    snapshot=CreditSnapshot(
                                        delta=0,
                                        streak_days=existing.get("streak_days"),
                                    ),
                                    dry_run=dry_run,
                                )
                            )
                            continue

                        if apply_jitter and not dry_run:
                            jitter_minutes = self._effective_jitter_minutes(cfg, settings)
                            if jitter_minutes > 0:
                                delay_sec = random.randint(0, jitter_minutes * 60)
                                if delay_sec > 0:
                                    emit(
                                        "info",
                                        f"{provider.display_name} 随机抖动等待 {delay_sec} 秒以错峰请求...",
                                        provider.id,
                                    )
                                    await asyncio.sleep(delay_sec)

                        # 抖动可能跨过业务日重置点，执行前按当前时间重新计算。
                        provider_biz_date = provider.business_date(
                            cfg, settings.timezone
                        )
                        emit(
                            "info",
                            f"开始执行 {provider.display_name} 签到...",
                            provider.id,
                        )

                        def _provider_log(level: str, msg: str, pid: str = provider.id) -> None:
                            emit(level, msg, pid)

                        result = await provider.checkin(
                            cfg,
                            provider_biz_date,
                            dry_run=dry_run,
                            log=_provider_log,
                        )
                        self.storage.save_checkin_result(actual_run_id, result)
                        results.append(result)

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
                    except Exception as exc:  # noqa: BLE001
                        duration_ms = int((time.perf_counter() - provider_started) * 1000)
                        display_name = getattr(provider, "display_name", provider.id)
                        result = CheckinResult(
                            provider_id=provider.id,
                            display_name=display_name,
                            status=CheckinStatus.FAILED,
                            message=f"{display_name} 适配器执行异常：{exc}",
                            biz_date=biz_date,
                            duration_ms=duration_ms,
                            dry_run=dry_run,
                            raw_error_type=type(exc).__name__,
                        )
                        self.storage.save_checkin_result(actual_run_id, result)
                        results.append(result)
                        emit("error", result.message, provider.id)

                self._notify_for_results(results, biz_date, notify=notify, dry_run=dry_run)
                self.storage.record_run_finish(actual_run_id, "completed")
                if not dry_run:
                    self._maybe_run_maintenance(settings, emit)
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
