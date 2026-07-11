from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

try:
    from astrbot.core.provider.sources.request_retry import (
        retry_provider_request as _core_retry_provider_request,
    )
except ImportError:
    _core_retry_provider_request = None

T = TypeVar("T")


async def retry_provider_request(
    provider_label: str,
    request_factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int | None = None,
) -> T:
    if _core_retry_provider_request is None:
        return await request_factory()
    return await _core_retry_provider_request(
        provider_label,
        request_factory,
        max_attempts=max_attempts,
    )
