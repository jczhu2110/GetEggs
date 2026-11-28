"""Typer CLI entrypoint for GetEggs."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.table import Table

from geteggs import __version__, launcher
from geteggs.core.config import (
    get_lock_path,
    get_ui_lock_path,
    get_ui_state_path,
    today_biz_date,
)
from geteggs.core.lock import RunLockBusyError, acquire_run_lock
from geteggs.core.models import LogEvent, TriggerType, utc_now_iso
from geteggs.core.runner import CheckinRunner
from geteggs.core.storage import Storage
from geteggs.notifiers.windows_toast import WindowsToastNotifier
from geteggs.providers.registry import get_provider, list_providers
from geteggs.scheduler.windows import (
    get_schedule_status,
    install_schedule,
    uninstall_schedule,
)
from geteggs.web.app import create_app

app = typer.Typer(
    name="geteggs",
    help="Local-first AI Agent 自动签到管理工具",
    no_args_is_help=True,
)
schedule_app = typer.Typer(help="管理 Windows 系统定时计划任务")
app.add_typer(schedule_app, name="schedule")

console = Console()


def _pick_port(preferred: int) -> int:
    """Return a bindable port without connecting to closed loopback ports."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name == "nt":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                s.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@app.command()
def version() -> None:
    """显示当前版本号。"""
    console.print(f"GetEggs v{__version__}")


@app.command()
def doctor(
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="仅检测指定插件 ID"),
) -> None:
    """只读检查本机客户端安装状态与本地登录态健康度。"""
    storage = Storage()
    try:
        targets = [get_provider(provider)] if provider else list_providers()
    except KeyError as exc:
        console.print(f"[red]{rich_escape(str(exc.args[0] if exc.args else exc))}[/red]")
        raise typer.Exit(code=1)

    table = Table(title="GetEggs 本地环境体检")
    table.add_column("工具", style="bold")
    table.add_column("客户端")
    table.add_column("登录态文件")
    table.add_column("账号")
    table.add_column("状态")
    table.add_column("说明")

    async def _run() -> list:
        res_list = []
        for p in targets:
            cfg = storage.get_provider_config(p.id)
            res_list.append(await p.doctor(cfg))
        return res_list

    results = asyncio.run(_run())
    for r in results:
        table.add_row(
            r.display_name,
            "已安装" if r.client_installed else "未检测到",
            "已找到" if r.auth_file_found else "未找到",
            r.account_label or "-",
            "[green]就绪[/green]" if r.healthy else "[red]异常[/red]",
            r.message,
        )
    console.print(table)


@app.command()
def status() -> None:
    """查看今日各工具签到状态与最近记录。"""
    storage = Storage()
    settings = storage.get_settings()
    biz_date = today_biz_date(settings.timezone)

    table = Table(title=f"GetEggs 今日状态 ({biz_date}，按工具业务日)")
    table.add_column("工具", style="bold")
    table.add_column("启用")
    table.add_column("今日状态")
    table.add_column("说明")

    for p in list_providers():
        cfg = storage.get_provider_config(p.id)
        provider_biz_date = p.business_date(cfg, settings.timezone)
        today_rec = storage.get_today_summary(p.id, provider_biz_date)
        latest = storage.get_latest_record(p.id)
        table.add_row(
            p.display_name,
            "是" if cfg.enabled else "否",
            today_rec["status"] if today_rec else "pending",
            (today_rec or latest or {}).get("message", "尚无记录"),
        )
    console.print(table)


@app.command()
def maintenance(
    vacuum: bool = typer.Option(False, "--vacuum", help="清理后执行 VACUUM"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅显示维护信息，不清理"),
) -> None:
    """清理过期的执行日志、批次、通知和签到流水。"""
    storage = Storage()
    if dry_run:
        console.print_json(data=storage.get_maintenance_info())
        return

    settings = storage.get_settings()
    try:
        with acquire_run_lock(get_lock_path()):
            result = storage.run_maintenance(settings, vacuum=vacuum, backup=True)
    except RunLockBusyError:
        console.print("[yellow]已有签到任务在运行，请稍后再维护。[/yellow]")
        raise typer.Exit(code=2)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]维护失败：{exc}[/red]")
        raise typer.Exit(code=1)
    console.print_json(data=result)
    console.print("[green]维护完成。[/green]")


@app.command()
def run(
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="仅执行指定插件 ID"),
    scheduled: bool = typer.Option(False, "--scheduled", help="标记本次为计划任务触发"),
    missed_only: bool = typer.Option(False, "--missed-only", help="跳过本业务日已有成功记录的工具"),
    jitter: bool = typer.Option(False, "--jitter", help="执行前启用随机延迟抖动（适合定时任务）"),
    no_notify: bool = typer.Option(False, "--no-notify", help="本次不发送系统弹窗通知"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预检模式：只校验登录态与状态，不提交签到"),
) -> None:
    """执行一次签到流程。"""
    runner = CheckinRunner()
    if scheduled:
        trigger = TriggerType.SCHEDULED
    elif missed_only:
        trigger = TriggerType.MISSED_CATCHUP
    else:
        trigger = TriggerType.MANUAL_CLI

    hidden_scheduled = scheduled and os.environ.get("GETEGGS_HIDDEN") == "1"

    def _print_event(ev: LogEvent) -> None:
        if hidden_scheduled and ev.level not in ("warning", "error"):
            return
        style = {"warning": "yellow", "error": "red"}.get(ev.level, "cyan")
        prefix = f"[{ev.provider_id}] " if ev.provider_id else ""
        console.print(f"[{style}]{rich_escape(prefix + ev.message)}[/{style}]")

    try:
        summary = asyncio.run(
            runner.run(
                provider_ids=[provider] if provider else None,
                trigger_type=trigger,
                missed_only=missed_only,
                apply_jitter=jitter,
                notify=not no_notify,
                dry_run=dry_run,
                event_sink=_print_event,
            )
        )
    except KeyError as exc:
        console.print(f"[red]{rich_escape(str(exc.args[0] if exc.args else exc))}[/red]")
        raise typer.Exit(code=1)
    except RunLockBusyError as exc:
        console.print(f"[yellow]{rich_escape(str(exc))}[/yellow]")
        raise typer.Exit(code=2)

    has_error = any(not r.status.is_done_for_today for r in summary.results)
    if has_error:
        raise typer.Exit(code=1)


@app.command()
def auth(
    provider: str = typer.Argument("workbuddy", help="需要打开受控浏览器完成登录授权的插件 ID"),
) -> None:
    """打开 Edge 受控浏览器完成一次网页登录授权，保存本地会话供后续静默签到使用。"""
    try:
        p = get_provider(provider)
    except KeyError as exc:
        console.print(f"[red]{rich_escape(str(exc.args[0] if exc.args else exc))}[/red]")
        raise typer.Exit(code=1)
    if not p.supports_browser_auth:
        console.print(f"[yellow]{p.display_name} 暂不支持受控浏览器授权。[/yellow]")
        raise typer.Exit(code=1)

    storage = Storage()
    cfg = storage.get_provider_config(p.id)
    console.print(f"[cyan]正在打开 Microsoft Edge 进行 {p.display_name} 登录授权，请在弹出的窗口中完成登录...[/cyan]")
    ok, msg = asyncio.run(p.authorize_browser(cfg))
    if ok:
        console.print(f"[green]{msg}[/green]")
    else:
        console.print(f"[red]{msg}[/red]")
        raise typer.Exit(code=1)


@app.command("notify-test")
def notify_test() -> None:
    """发送一条测试用 Windows 系统弹窗通知。"""
    notifier = WindowsToastNotifier()
    ok, err = notifier.send(
        "GetEggs 通知测试",
        "如果你看到这条消息，说明 Windows 弹窗通知渠道已正常工作。",
        level="info",
    )
    if ok:
        console.print("[green]测试弹窗已触发，请查看系统右下角通知。[/green]")
    else:
        console.print(f"[red]发送失败：{err}[/red]")
        raise typer.Exit(code=1)


_read_ui_state = launcher.read_ui_state


def _open_browser_when_ready(url: str, port: int) -> None:
    """Open the browser once the local server starts accepting connections."""
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if launcher.port_open(port):
            if not launcher.open_url(url):
                launcher.notify_failure(
                    "GetEggs 已启动，但浏览器打开失败，请检查默认浏览器设置。"
                )
            return
        time.sleep(0.05)
    launcher.notify_failure("GetEggs Web 服务已启动，但等待端口就绪超时。")


def _run_ui(
    port: int,
    no_browser: bool,
    no_auth: bool,
    quiet: bool = False,
    open_when_ready: bool = False,
) -> None:
    chosen_port = _pick_port(port)
    web_app = create_app(auth_enabled=not no_auth)
    auth_mgr = web_app.state.auth_manager

    if no_auth:
        url = f"http://127.0.0.1:{chosen_port}/"
    else:
        url = f"http://127.0.0.1:{chosen_port}/auth?token={auth_mgr.startup_token}"

    state_path = get_ui_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_payload = json.dumps(
        {
            "pid": os.getpid(),
            "port": chosen_port,
            "url": url,
            "started_at": utc_now_iso(),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        dir=state_path.parent, prefix=".ui-state.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(state_payload)
            handle.flush()
        os.replace(tmp_path, state_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    if quiet:
        console.print(f"[dim]GetEggs 本地控制台已启动（端口 {chosen_port}）。[/dim]")
    else:
        console.print(f"[green]GetEggs 本地控制台已启动：[/green]{url}")
        console.print("[dim]仅监听 127.0.0.1。按 Ctrl+C 可关闭服务（不影响已注册的计划任务）。[/dim]")

    def _cleanup_state() -> None:
        try:
            state_path.unlink()
        except OSError:
            pass

    web_app.state.ui_state_cleanup = _cleanup_state
    browser_timer: Optional[threading.Timer] = None
    try:
        if open_when_ready:
            threading.Thread(
                target=_open_browser_when_ready,
                args=(url, chosen_port),
                daemon=True,
            ).start()
        elif not no_browser:
            browser_timer = threading.Timer(0.6, lambda: webbrowser.open(url))
            browser_timer.daemon = True
            browser_timer.start()
        config = uvicorn.Config(
            web_app, host="127.0.0.1", port=chosen_port, log_level="warning"
        )
        server = uvicorn.Server(config)
        web_app.state.uvicorn_server = server
        server.run()
    finally:
        if browser_timer is not None:
            browser_timer.cancel()
        _cleanup_state()


@app.command()
def ui(
    port: int = typer.Option(18765, "--port", help="本地监听端口（仅绑定 127.0.0.1）"),
    no_browser: bool = typer.Option(False, "--no-browser", help="启动服务但不自动打开浏览器"),
    no_auth: bool = typer.Option(False, "--no-auth", help="关闭本地会话令牌（仅建议调试时使用）"),
    quiet: bool = typer.Option(False, "--quiet", help="静默启动（由 open 命令使用）"),
    open_when_ready: bool = typer.Option(
        False, "--open-when-ready", help="服务就绪后再打开浏览器（由启动器使用）"
    ),
) -> None:
    """启动本地 Web 控制台并打开浏览器。"""
    try:
        with acquire_run_lock(get_ui_lock_path()):
            _run_ui(port, no_browser, no_auth, quiet, open_when_ready)
    except RunLockBusyError:
        state = _read_ui_state()
        existing_url = state.get("url")
        if isinstance(existing_url, str) and existing_url and not quiet:
            console.print(f"[yellow]GetEggs Web 控制台已在运行：[/yellow]{existing_url}")
        elif quiet:
            console.print("[yellow]GetEggs Web 控制台已在运行。[/yellow]")
        else:
            console.print("[yellow]GetEggs Web 控制台已在运行，请使用已有终端中的 URL。[/yellow]")
        raise typer.Exit(code=0)


@app.command("open")
def open_console() -> None:
    """打开 GetEggs Web 控制台；未运行时在后台启动，不显示终端窗口。"""
    launcher.main()


@schedule_app.command("status")
def schedule_status_cmd() -> None:
    """查看 Windows 计划任务状态。"""
    info = get_schedule_status()
    console.print_json(data=info)


@schedule_app.command("install")
def schedule_install_cmd(
    time: Optional[str] = typer.Option(None, "--time", "-t", help="每日触发时间 HH:MM（默认读取配置）"),
    no_elevate: bool = typer.Option(
        False, "--no-elevate", hidden=True, help="内部使用：跳过管理员提权包装"
    ),
    result_file: Optional[Path] = typer.Option(
        None, "--result-file", hidden=True, help="内部使用：写入安装结果 JSON"
    ),
    params_file: Optional[Path] = typer.Option(
        None, "--params-file", hidden=True, help="内部使用：从 JSON 读取提权安装参数"
    ),
) -> None:
    """安装或更新 Windows 每日定时签到计划任务。"""
    params: dict[str, object] = {}
    if params_file is not None:
        try:
            loaded = json.loads(params_file.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not loaded:
                raise ValueError("提权安装参数文件不是有效的非空 JSON 对象。")
            params = loaded
        except (OSError, ValueError) as exc:
            msg = f"读取提权安装参数失败：{exc}"
            if result_file:
                try:
                    result_file.parent.mkdir(parents=True, exist_ok=True)
                    result_file.write_text(
                        json.dumps({"ok": False, "message": msg}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            console.print(f"[red]{rich_escape(msg)}[/red]")
            raise typer.Exit(1)

    if params:
        target_time = str(params.get("schedule_time") or time or "10:15")
        raw_work_dir = params.get("working_dir")
        working_dir = Path(str(raw_work_dir)) if raw_work_dir else None
        timeout_minutes = int(params.get("timeout_minutes") or 25)
        raw_user = params.get("target_user")
        target_user = str(raw_user).strip() if raw_user else None
    else:
        storage = Storage()
        settings = storage.get_settings()
        target_time = time or settings.schedule_time
        if time:
            try:
                settings = settings.model_copy(update={"schedule_time": target_time})
                settings = type(settings).model_validate(settings.model_dump())
            except ValueError as exc:
                console.print(f"[red]定时时间格式无效：{rich_escape(str(exc))}[/red]")
                raise typer.Exit(1)
            storage.save_settings(settings)
        configs = [storage.get_provider_config(p.id) for p in list_providers()]
        working_dir = None
        timeout_minutes = CheckinRunner.estimate_task_timeout_minutes(
            settings, configs
        )
        target_user = None

    try:
        ok, msg = install_schedule(
            target_time,
            working_dir=working_dir,
            timeout_minutes=timeout_minutes,
            elevate=not no_elevate,
            target_user=target_user,
        )
    except Exception as exc:  # noqa: BLE001
        ok, msg = False, f"注册计划任务失败：{exc}"

    if result_file:
        try:
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.write_text(
                json.dumps({"ok": ok, "message": msg}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    if ok:
        console.print(f"[green]{msg}[/green]")
    else:
        console.print(f"[red]{msg}[/red]")
        raise typer.Exit(code=1)


@schedule_app.command("uninstall")
def schedule_uninstall_cmd(
    no_elevate: bool = typer.Option(
        False, "--no-elevate", hidden=True, help="内部使用：跳过管理员提权包装"
    ),
    result_file: Optional[Path] = typer.Option(
        None, "--result-file", hidden=True, help="内部使用：写入卸载结果 JSON"
    ),
) -> None:
    """卸载 Windows 计划任务。"""
    try:
        ok, msg = uninstall_schedule(elevate=not no_elevate)
    except Exception as exc:  # noqa: BLE001
        ok, msg = False, f"移除计划任务失败：{exc}"

    if result_file:
        try:
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.write_text(
                json.dumps({"ok": ok, "message": msg}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    if ok:
        console.print(f"[green]{msg}[/green]")
    else:
        console.print(f"[red]{msg}[/red]")
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
