"""Base provider protocol and shared sanitization/network helpers."""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Optional

from geteggs.core.config import business_date as resolve_business_date
from geteggs.core.models import (
    CheckinResult,
    DoctorResult,
    ProviderConfig,
)

LogCallback = Callable[[str, str], None]


def mask_secret(value: Optional[str], visible_prefix: int = 3, visible_suffix: int = 3) -> Optional[str]:
    """Return a masked identifier safe for UI and log output."""
    if not value:
        return None
    if len(value) <= visible_prefix + visible_suffix:
        return "*" * len(value)
    return f"{value[:visible_prefix]}***{value[-visible_suffix:]}"


def extract_first(data: dict[str, Any], *keys: str) -> Any:
    """Return the first non-None value matching any candidate key in `data` or `data['data']`."""
    sources = [data]
    nested = data.get("data")
    if isinstance(nested, dict):
        sources.insert(0, nested)
    for src in sources:
        for key in keys:
            if key in src and src[key] is not None:
                return src[key]
    return None


class BaseProvider(ABC):
    """Contract every AI agent check-in adapter must implement."""

    id: str
    display_name: str
    description: str
    supports_browser_auth: bool = False
    supports_streak: bool = True
    fixed_business_day_start: Optional[str] = None

    @abstractmethod
    def detect_client_exe(self, config: ProviderConfig) -> Optional[str]:
        """Locate the local desktop executable path if installed."""

    @abstractmethod
    async def doctor(self, config: ProviderConfig) -> DoctorResult:
        """Inspect local client installation and session state (read-only)."""

    @abstractmethod
    async def checkin(
        self,
        config: ProviderConfig,
        biz_date: str,
        *,
        dry_run: bool = False,
        log: Optional[LogCallback] = None,
    ) -> CheckinResult:
        """Perform an idempotent check-in for `biz_date`."""

    def business_date(
        self,
        config: ProviderConfig,
        tz_name: str = "Asia/Shanghai",
        *,
        now: Optional[datetime] = None,
    ) -> str:
        """Return the current business date using the provider's daily reset time."""
        return resolve_business_date(config.business_day_start, tz_name, now)

    def launch_client(self, config: ProviderConfig) -> tuple[bool, str]:
        """Launch the official desktop client for manual login or interaction."""
        exe = self.detect_client_exe(config)
        if not exe:
            return False, f"未在本机找到 {self.display_name} 客户端可执行文件，请在设置中手动指定路径。"
        try:
            kwargs: dict[str, Any] = {}
            if os.name == "nt":
                kwargs["creationflags"] = (
                    getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                )
            subprocess.Popen([exe], **kwargs)
            return True, f"已启动 {self.display_name} 客户端：{exe}"
        except OSError as exc:
            return False, f"启动 {self.display_name} 失败：{exc}"

    async def authorize_browser(self, config: ProviderConfig) -> tuple[bool, str]:
        """Run optional interactive browser sign-in flow to save local session state."""
        return False, f"{self.display_name} 暂不支持受控浏览器授权。"

    @staticmethod
    def build_http_client(
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = 15.0,
        cookies: Optional[dict[str, str]] = None,
    ) -> httpx.AsyncClient:
        """Create a configured async HTTP client."""
        import httpx

        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            transport=transport,
            cookies=cookies,
            headers={"Accept": "application/json"},
        )
