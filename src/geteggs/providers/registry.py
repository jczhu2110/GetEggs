"""Provider registry."""

from __future__ import annotations

from geteggs.providers.base import BaseProvider
from geteggs.providers.qoder import QoderProvider
from geteggs.providers.workbuddy import WorkBuddyProvider

_PROVIDERS: dict[str, BaseProvider] = {
    WorkBuddyProvider.id: WorkBuddyProvider(),
    QoderProvider.id: QoderProvider(),
}


def list_providers() -> list[BaseProvider]:
    """Return all registered provider adapters in display order."""
    return list(_PROVIDERS.values())


def get_provider(provider_id: str) -> BaseProvider:
    """Lookup a provider adapter by its unique ID."""
    if provider_id not in _PROVIDERS:
        raise KeyError(f"未知的插件 ID: {provider_id}")
    return _PROVIDERS[provider_id]
