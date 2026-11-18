"""Windows native Toast notification adapter."""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from geteggs.notifiers.base import BaseNotifier


def _escape_ps_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _send_via_powershell(title: str, message: str) -> tuple[bool, Optional[str]]:
    """Fallback Windows 10/11 Toast via PowerShell WinRT API."""
    safe_title = _escape_ps_xml(title)
    safe_msg = _escape_ps_xml(message)
    ps_script = f"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{safe_title}</text><text>{safe_msg}</text></binding></visual></toast>')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('GetEggs')
$notifier.Show($toast)
""".strip()

    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        if completed.returncode == 0:
            return True, None
        return False, (completed.stderr or completed.stdout or "PowerShell toast failed").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


class WindowsToastNotifier(BaseNotifier):
    """Send native Windows 10/11 Toast notifications."""

    channel_id = "windows_toast"
    app_id = "GetEggs 自动签到助手"

    def send(
        self,
        title: str,
        message: str,
        *,
        level: str = "info",
        action_label: Optional[str] = None,
        action_url: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        if os.name != "nt":
            return False, "当前非 Windows 系统，跳过 Windows 弹窗通知。"

        try:
            from winotify import Notification, audio  # type: ignore[import-untyped]

            toast = Notification(
                app_id=self.app_id,
                title=title,
                msg=message,
                duration="long" if level in ("warning", "error") else "short",
            )
            if level in ("warning", "error"):
                toast.set_audio(audio.Reminder, loop=False)
            else:
                toast.set_audio(audio.Default, loop=False)

            if action_label and action_url:
                toast.add_actions(label=action_label, launch=action_url)

            toast.show()
            return True, None
        except ImportError:
            return _send_via_powershell(title, message)
        except Exception as exc:  # noqa: BLE001
            ok, ps_err = _send_via_powershell(title, message)
            if ok:
                return True, None
            return False, f"winotify 失败 ({exc}); PowerShell 回退失败 ({ps_err})"
