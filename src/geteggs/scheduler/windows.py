"""Windows Task Scheduler integration for silent daily + logon catch-up runs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from geteggs.core.config import get_data_dir

TASK_NAME = "GetEggs-DailyCheckin"


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


def _run_ps(script: str) -> tuple[int, str, str]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": 15,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        **kwargs,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _task_missing_fast() -> bool:
    """Return True when ``schtasks`` reports that the task is absent.

    Starting PowerShell merely to discover that no task exists costs close to a
    second. ``schtasks`` answers the same question much faster; any probe
    failure falls back to the full PowerShell query.
    """
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": 10,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME], **kwargs)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode != 0


def get_schedule_status() -> dict[str, Any]:
    """Query Windows Task Scheduler for the GetEggs task state."""
    if os.name != "nt":
        return {
            "supported": False,
            "installed": False,
            "task_name": TASK_NAME,
            "message": "仅 Windows 环境支持系统计划任务集成。",
        }

    if _task_missing_fast():
        return {
            "supported": True,
            "installed": False,
            "task_name": TASK_NAME,
            "message": "尚未安装系统计划任务。",
        }

    ps = f"""
$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if (-not $t) {{
    @{{ installed = $false }} | ConvertTo-Json -Compress
    exit 0
}}
$info = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}'
@{{
    installed = $true
    state = [string]$t.State
    last_run_time = if ($info.LastRunTime.Year -gt 1900) {{ $info.LastRunTime.ToString('o') }} else {{ $null }}
    next_run_time = if ($info.NextRunTime.Year -gt 1900) {{ $info.NextRunTime.ToString('o') }} else {{ $null }}
    last_task_result = $info.LastTaskResult
}} | ConvertTo-Json -Compress
""".strip()

    code, out, err = _run_ps(ps)
    if code != 0:
        return {
            "supported": True,
            "installed": False,
            "task_name": TASK_NAME,
            "message": f"查询计划任务失败：{err or out}",
        }
    try:
        data = json.loads(out) if out else {}
    except ValueError:
        data = {}

    installed = bool(data.get("installed"))
    return {
        "supported": True,
        "installed": installed,
        "task_name": TASK_NAME,
        "state": data.get("state"),
        "last_run_time": data.get("last_run_time"),
        "next_run_time": data.get("next_run_time"),
        "last_task_result": data.get("last_task_result"),
        "message": "计划任务已安装并就绪。" if installed else "尚未安装系统计划任务。",
    }


def install_schedule(
    schedule_time: str = "10:15",
    *,
    catchup_on_logon: bool = True,
    working_dir: Optional[Path] = None,
) -> tuple[bool, str]:
    """Register daily + logon catch-up scheduled task in Windows Task Scheduler."""
    if os.name != "nt":
        return False, "当前非 Windows 系统，无法注册 Windows 计划任务。"

    hhmm = _validate_hhmm(schedule_time)
    py_exe = _resolve_python_executable().replace("'", "''")
    work_dir = str((working_dir or get_data_dir()).resolve()).replace("'", "''")

    imported, probe_error = _probe_package_import(_resolve_probe_executable(), work_dir)
    if not imported:
        return (
            False,
            "计划任务使用的 Python 环境无法导入 geteggs，"
            f"请先在项目环境中执行 `uv sync` 后重试：{probe_error}",
        )

    triggers_block = f"$triggers = @((New-ScheduledTaskTrigger -Daily -At '{hhmm}'))"
    if catchup_on_logon:
        triggers_block += "\n$triggers += (New-ScheduledTaskTrigger -AtLogOn)"

    ps = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute '{py_exe}' -Argument '-m geteggs run --missed-only --jitter' -WorkingDirectory '{work_dir}'
{triggers_block}
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 25) `
    -MultipleInstances IgnoreNew
$userId = if ($env:USERDOMAIN) {{ "$env:USERDOMAIN\\$env:USERNAME" }} else {{ $env:USERNAME }}
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Out-Null
Write-Output 'OK'
""".strip()

    code, out, err = _run_ps(ps)
    if code == 0:
        return True, f"已成功注册 Windows 计划任务 {TASK_NAME}（每日 {hhmm} + 登录漏签补跑）。"
    return False, f"注册计划任务失败：{err or out}"


def uninstall_schedule() -> tuple[bool, str]:
    """Remove the GetEggs scheduled task from Windows Task Scheduler."""
    if os.name != "nt":
        return False, "当前非 Windows 系统。"

    ps = f"""
$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($t) {{
    Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false
}}
Write-Output 'OK'
""".strip()
    code, out, err = _run_ps(ps)
    if code == 0:
        return True, f"已移除计划任务 {TASK_NAME}。"
    return False, f"移除计划任务失败：{err or out}"
