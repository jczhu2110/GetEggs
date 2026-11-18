"""Base notifier contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class BaseNotifier(ABC):
    """Contract for notification channels."""

    channel_id: str

    @abstractmethod
    def send(
        self,
        title: str,
        message: str,
        *,
        level: str = "info",
        action_label: Optional[str] = None,
        action_url: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """Deliver a notification; return (success, error_message)."""
