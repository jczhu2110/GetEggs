"""Windows native Toast notification adapter."""

from __future__ import annotations

import os
import re
import subprocess
from typing import Optional
from urllib.parse import urlparse

from geteggs.notifiers.base import BaseNotifier

_ILLEGAL_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_TOAST_PS_SCRIPT = """
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($env:GETEGGS_TOAST_TITLE)) | Out-Null
$nodes.Item(1).AppendChild($xml.CreateTextNode($env:GETEGGS_TOAST_MSG)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('GetEggs')
$notifier.Show($toast)
""".strip()


def _sanitize_xml_text(text: str) -> str:
    """Remove characters illegal in XML 1.0."""
    return _ILLEGAL_XML_CHARS.sub("", text or "")


def _decode_ps_output(raw: bytes | str | None) -> str:
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


def _is_safe_action_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _send_via_powershell(title: str, message: str) -> tuple[bool, Optional[str]]:
    """Fallback Windows 10/11 Toast via PowerShell WinRT API."""
    env = os.environ.copy()
    env["GETEGGS_TOAST_TITLE"] = _sanitize_xml_text(title)
    env["GETEGGS_TOAST_MSG"] = _sanitize_xml_text(message)
    kwargs: dict[str, object] = {
        "capture_output": True,
        "timeout": 10,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                _TOAST_PS_SCRIPT,
            ],
            **kwargs,  # type: ignore[arg-type]
        )
        if completed.returncode == 0:
            return True, None
        stderr = _decode_ps_output(completed.stderr).strip()
        stdout = _decode_ps_output(completed.stdout).strip()
        return False, (stderr or stdout or "PowerShell toast failed")
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

            if action_label and action_url and _is_safe_action_url(action_url):
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
