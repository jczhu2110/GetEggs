"""Windows Task Scheduler integration for silent daily check-in runs."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from geteggs.core.config import get_data_dir

TASK_NAME = "GetEggs-DailyCheckin"


def _ps_b64_expr(value: str) -> str:
    """Encode an arbitrary string into a safe PowerShell UTF-8 expression.

    Base64 contains only ASCII [A-Za-z0-9+/=], preventing ASCII or Unicode
    single-quote breakouts and ANSI code-page mangling.
    """
    b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64}'))"


def _current_windows_user() -> Optional[str]:
    username = (os.environ.get("USERNAME") or "").strip()
    if not username:
        return None
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    return f"{domain}\\{username}" if domain else username


def _is_access_denied_text(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered or marker in text
        for marker in (
            "access is denied",
            "access denied",
            "unauthorizedaccess",
            "0x80070005",
            "拒绝访问",
            "权限",
        )
    )


def _resolve_python_executable() -> str:
    """Prefer pythonw.exe to avoid flashing a console window during scheduled runs."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(exe)


def _resolve_probe_executable() -> str:
    exe = Path(_resolve_python_executable())
    if exe.name.lower() == "pythonw.exe":
        python = exe.with_name("python.exe")
        if python.is_file():
            return str(python)
    return str(exe)


def _probe_package_import(exe: str, cwd: str) -> tuple[bool, str]:
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "capture_output": True,
        "text": True,
        "timeout": 20,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.run([exe, "-c", "import geteggs"], **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout or "导入 geteggs 失败").strip()


def _validate_hhmm(time_str: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_str.strip()):
        raise ValueError("时间格式必须为 HH:MM（24 小时制，例如 09:00）。")
    return time_str.strip()


def _run_ps(script: str, timeout: float = 15) -> tuple[int, str, str]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        **kwargs,
    )
    return (
        proc.returncode,
        _decode_output(proc.stdout).strip(),
        _decode_output(proc.stderr).strip(),
    )


_MISSING_MARKERS = (
    "cannot find the file",
    "does not exist",
    "no such",
    "找不到",
    "不存在",
)


def _decode_output(raw: Any) -> str:
    """Decode console output that may be UTF-8 or the Chinese ANSI code page."""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _task_missing_fast() -> Optional[bool]:
    """Return True/False when the fast probe is conclusive, None when unknown.

    Starting PowerShell merely to discover that no task exists costs close to a
    second. ``schtasks`` answers the same question much faster, but a non-zero
    exit code alone is ambiguous (missing task, access denied, corrupted task
    store), so only recognized "not found" output counts as a definitive miss.
    """
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "timeout": 10,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME], **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return False
    output = (
        f"{_decode_output(getattr(proc, 'stdout', b''))}\n"
        f"{_decode_output(getattr(proc, 'stderr', b''))}"
    ).lower()
    if any(marker in output for marker in _MISSING_MARKERS):
        return True
    return None


def get_schedule_status() -> dict[str, Any]:
    """Query Windows Task Scheduler for the GetEggs task state."""
    if os.name != "nt":
        return {
            "supported": False,
            "installed": False,
            "task_name": TASK_NAME,
            "message": "仅 Windows 环境支持系统计划任务集成。",
        }

    if _task_missing_fast() is True:
        return {
            "supported": True,
            "installed": False,
            "elevated": _is_elevated(),
            "task_name": TASK_NAME,
            "message": "尚未安装系统计划任务。",
        }

    ps = f"""
$ErrorActionPreference = 'Stop'
try {{
    $t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction Stop
}} catch {{
    $errorId = [string]$_.FullyQualifiedErrorId
    $errorText = [string]$_.Exception.Message
    if ($errorId -match 'NotFound|ObjectNotFound' -or $errorText -match 'No MSFT_ScheduledTask objects found|0x80070002') {{
        @{{ installed = $false }} | ConvertTo-Json -Compress
    }} else {{
        @{{ error = $errorText }} | ConvertTo-Json -Compress
    }}
    exit 0
}}
$info = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}'
@{{
    installed = $true
    state = [string]$t.State
    last_run_time = if ($info.LastRunTime.Year -gt 1999) {{ $info.LastRunTime.ToString('o') }} else {{ $null }}
    next_run_time = if ($info.NextRunTime.Year -gt 1999) {{ $info.NextRunTime.ToString('o') }} else {{ $null }}
    last_task_result = $info.LastTaskResult
}} | ConvertTo-Json -Compress
""".strip()

    try:
        code, out, err = _run_ps(ps)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "supported": True,
            "installed": False,
            "query_failed": True,
            "elevated": _is_elevated(),
            "task_name": TASK_NAME,
            "message": f"查询计划任务失败：{exc}",
        }
    if code != 0:
        return {
            "supported": True,
            "installed": False,
            "query_failed": True,
            "elevated": _is_elevated(),
            "task_name": TASK_NAME,
            "message": f"查询计划任务失败：{err or out}",
        }
    try:
        data = json.loads(out) if out else None
    except ValueError:
        data = None

    if not isinstance(data, dict):
        return {
            "supported": True,
            "installed": False,
            "query_failed": True,
            "elevated": _is_elevated(),
            "task_name": TASK_NAME,
            "message": "查询计划任务失败：返回结果无法解析为 JSON。",
        }

    if data.get("error"):
        return {
            "supported": True,
            "installed": False,
            "query_failed": True,
            "elevated": _is_elevated(),
            "task_name": TASK_NAME,
            "message": f"查询计划任务失败：{data['error']}",
        }

    installed = bool(data.get("installed"))
    state = data.get("state")
    last_task_result = data.get("last_task_result")
    if not installed:
        message = "尚未安装系统计划任务。"
    elif state and str(state).lower() not in ("ready", "running", "queued"):
        message = f"计划任务已安装，但当前状态为 {state}。"
    elif last_task_result == 1:
        message = "计划任务已安装并已运行，但上次存在未完成签到的工具（退出码 1），请查看历史记录。"
    elif last_task_result == 2:
        message = "计划任务已安装，上次因已有签到任务运行中而跳过（退出码 2）。"
    elif last_task_result not in (None, 0, 267008, 267009, 267011):
        message = f"计划任务已安装，但上次执行返回异常代码 {last_task_result}。"
    else:
        message = "计划任务已安装并就绪。"
    return {
        "supported": True,
        "installed": installed,
        "elevated": _is_elevated(),
        "task_name": TASK_NAME,
        "state": state,
        "last_run_time": data.get("last_run_time"),
        "next_run_time": data.get("next_run_time"),
        "last_task_result": last_task_result,
        "message": message,
    }


def _is_elevated() -> bool:
    """Return True when the current Windows token is elevated."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _install_schedule_elevated(
    python_exe: str,
    work_dir: str,
    *,
    schedule_time: str,
    timeout_minutes: int,
    target_user: Optional[str] = None,
) -> tuple[bool, str]:
    """Re-run the install command through UAC when the current token is filtered."""
    fd, result_name = tempfile.mkstemp(
        prefix="geteggs-elevated-install-", suffix=".json"
    )
    os.close(fd)
    result_path = Path(result_name)
    pfd, params_name = tempfile.mkstemp(
        prefix="geteggs-elevated-params-", suffix=".json"
    )
    os.close(pfd)
    params_path = Path(params_name)
    params_path.write_text(
        json.dumps(
            {
                "schedule_time": schedule_time,
                "working_dir": work_dir,
                "timeout_minutes": int(timeout_minutes),
                "target_user": target_user,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ps = f"""
$ErrorActionPreference = 'Stop'
$pyExe = {_ps_b64_expr(python_exe)}
$workDir = {_ps_b64_expr(work_dir)}
$resultFile = {_ps_b64_expr(str(result_path))}
$paramsFile = {_ps_b64_expr(str(params_path))}
$argList = @('-m', 'geteggs', 'schedule', 'install', '--no-elevate', '--result-file', ('"' + $resultFile + '"'), '--params-file', ('"' + $paramsFile + '"'))
$p = Start-Process -FilePath $pyExe -ArgumentList $argList -WorkingDirectory $workDir -Verb RunAs -Wait -PassThru -WindowStyle Hidden
exit $p.ExitCode
""".strip()

    try:
        try:
            code, out, err = _run_ps(ps, timeout=120)
        except subprocess.TimeoutExpired:
            # The elevated child cannot be killed from here; it may still have
            # completed registration after the timeout.
            if get_schedule_status().get("installed"):
                return True, "管理员授权等待超时，但检测到计划任务已安装成功。"
            return False, "等待管理员授权超时，计划任务未安装。"
        payload: dict[str, Any] = {}
        try:
            if result_path.is_file() and result_path.stat().st_size > 0:
                parsed = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    payload = parsed
        except (OSError, ValueError):
            payload = {}
        if payload:
            return bool(payload.get("ok")), str(payload.get("message") or "")
        if code == 0:
            return True, "已通过管理员权限注册计划任务。"
        text = f"{err}\n{out}".strip()
        lowered = text.lower()
        if "取消" in text or "cancelled" in lowered or "0x800704c7" in lowered:
            return False, "已取消管理员授权，计划任务未安装。"
        return False, f"提权安装计划任务失败：{text or '未知错误'}"
    finally:
        result_path.unlink(missing_ok=True)
        params_path.unlink(missing_ok=True)


def install_schedule(
    schedule_time: str = "10:15",
    *,
    working_dir: Optional[Path] = None,
    timeout_minutes: int = 25,
    elevate: bool = True,
    target_user: Optional[str] = None,
) -> tuple[bool, str]:
    """Register daily silent scheduled task in Windows Task Scheduler."""
    if os.name != "nt":
        return False, "当前非 Windows 系统，无法注册 Windows 计划任务。"

    hhmm = _validate_hhmm(schedule_time)
    timeout = max(1, min(1440, int(timeout_minutes)))
    python_exe = _resolve_python_executable()
    probe_exe = _resolve_probe_executable()
    work_dir_raw = str((working_dir or get_data_dir()).resolve())
    effective_user = (target_user or _current_windows_user() or "").strip() or None

    imported, probe_error = _probe_package_import(probe_exe, work_dir_raw)
    if not imported:
        return (
            False,
            "计划任务使用的 Python 环境无法导入 geteggs，"
            f"请先在项目环境中执行 `uv sync` 后重试：{probe_error}",
        )

    if elevate and not _is_elevated():
        try:
            return _install_schedule_elevated(
                probe_exe,
                work_dir_raw,
                schedule_time=hhmm,
                timeout_minutes=timeout,
                target_user=effective_user,
            )
        except OSError as exc:
            return False, f"请求管理员权限失败：{exc}"

    user_expr = (
        _ps_b64_expr(effective_user)
        if effective_user
        else 'if ($env:USERDOMAIN) { "$env:USERDOMAIN\\$env:USERNAME" } else { $env:USERNAME }'
    )

    ps = f"""
$ErrorActionPreference = 'Stop'
$pyExe = {_ps_b64_expr(python_exe)}
$workDir = {_ps_b64_expr(work_dir_raw)}
$scheduleAt = {_ps_b64_expr(hhmm)}
$taskName = {_ps_b64_expr(TASK_NAME)}
$executeCmd = if ($pyExe -match '\\s') {{ '"' + $pyExe + '"' }} else {{ $pyExe }}
$userId = {user_expr}
$action = New-ScheduledTaskAction -Execute $executeCmd -Argument '-m geteggs run --scheduled --missed-only --jitter' -WorkingDirectory $workDir
$triggers = @((New-ScheduledTaskTrigger -Daily -At $scheduleAt))
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes {timeout}) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Out-Null
Write-Output 'OK'
""".strip()

    try:
        code, out, err = _run_ps(ps)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"注册计划任务失败：{exc}"
    if code == 0:
        return (
            True,
            f"已成功注册 Windows 计划任务 {TASK_NAME}"
            f"（每日 {hhmm}，最长执行 {timeout} 分钟）。",
        )
    return False, f"注册计划任务失败：{err or out}"


def _uninstall_schedule_elevated(python_exe: str, work_dir: str) -> tuple[bool, str]:
    """Re-run the uninstall command through UAC when direct removal is denied."""
    fd, result_name = tempfile.mkstemp(
        prefix="geteggs-elevated-uninstall-", suffix=".json"
    )
    os.close(fd)
    result_path = Path(result_name)

    ps = f"""
$ErrorActionPreference = 'Stop'
$pyExe = {_ps_b64_expr(python_exe)}
$workDir = {_ps_b64_expr(work_dir)}
$resultFile = {_ps_b64_expr(str(result_path))}
$argList = @('-m', 'geteggs', 'schedule', 'uninstall', '--no-elevate', '--result-file', ('"' + $resultFile + '"'))
$p = Start-Process -FilePath $pyExe -ArgumentList $argList -WorkingDirectory $workDir -Verb RunAs -Wait -PassThru -WindowStyle Hidden
exit $p.ExitCode
""".strip()

    try:
        try:
            code, out, err = _run_ps(ps, timeout=120)
        except subprocess.TimeoutExpired:
            status = get_schedule_status()
            if not status.get("installed") and not status.get("query_failed"):
                return True, f"管理员授权等待超时，但检测到计划任务 {TASK_NAME} 已移除。"
            return False, "等待管理员授权超时，计划任务未移除。"
        payload: dict[str, Any] = {}
        try:
            if result_path.is_file() and result_path.stat().st_size > 0:
                parsed = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    payload = parsed
        except (OSError, ValueError):
            payload = {}
        if payload:
            return bool(payload.get("ok")), str(payload.get("message") or "")
        if code == 0:
            return True, f"已通过管理员权限移除计划任务 {TASK_NAME}。"
        text = f"{err}\n{out}".strip()
        lowered = text.lower()
        if "取消" in text or "cancelled" in lowered or "0x800704c7" in lowered:
            return False, "已取消管理员授权，计划任务未移除。"
        return False, f"提权移除计划任务失败：{text or '未知错误'}"
    finally:
        result_path.unlink(missing_ok=True)


def uninstall_schedule(*, elevate: bool = True) -> tuple[bool, str]:
    """Remove the GetEggs scheduled task from Windows Task Scheduler."""
    if os.name != "nt":
        return False, "当前非 Windows 系统。"

    ps = f"""
$ErrorActionPreference = 'Stop'
$taskName = {_ps_b64_expr(TASK_NAME)}
$t = $null
try {{
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
}} catch {{
    $errorId = [string]$_.FullyQualifiedErrorId
    $errorText = [string]$_.Exception.Message
    if (-not ($errorId -match 'NotFound|ObjectNotFound' -or $errorText -match 'No MSFT_ScheduledTask objects found|0x80070002')) {{
        throw
    }}
}}
if ($t) {{
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
}}
Write-Output 'OK'
""".strip()
    try:
        code, out, err = _run_ps(ps)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"移除计划任务失败：{exc}"
    if code == 0:
        return True, f"已移除计划任务 {TASK_NAME}。"
    combined = f"{err}\n{out}".strip()
    if elevate and not _is_elevated() and _is_access_denied_text(combined):
        try:
            return _uninstall_schedule_elevated(
                _resolve_probe_executable(), str(get_data_dir().resolve())
            )
        except OSError as exc:
            return False, f"请求管理员权限失败：{exc}"
    return False, f"移除计划任务失败：{err or out}"
