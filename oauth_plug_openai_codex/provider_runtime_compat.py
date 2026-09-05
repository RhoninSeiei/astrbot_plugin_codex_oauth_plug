"""Compatibility shims for AstrBot provider APIs that changed across releases."""

import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TypeVar

from .retry_compat import retry_provider_request as _legacy_retry_provider_request

T = TypeVar("T")

try:
    from astrbot.core.provider.sources.request_retry import (
        ProviderRequestError,
        retry_provider_request as _core_retry_provider_request,
    )
except ImportError:
    _core_retry_provider_request = None

    class ProviderRequestError(RuntimeError):
        """Provider failure carrying an HTTP status for retry classifiers."""

        def __init__(self, message: str, *, status_code: int | None = None) -> None:
            super().__init__(message)
            self.status_code = status_code


try:
    from astrbot.core.provider.sources.request_retry import provider_oauth_web_search
except ImportError:
    provider_oauth_web_search: ContextVar[str] = ContextVar(
        "provider_oauth_web_search",
        default="inherit",
    )


try:
    from astrbot.core.provider.provider import provider_stats_managed_by_agent
except ImportError:
    provider_stats_managed_by_agent: ContextVar[bool] = ContextVar(
        "provider_stats_managed_by_agent",
        default=False,
    )


try:
    from astrbot.core import db_helper
except ImportError:
    db_helper = None


async def retry_provider_request(
    provider_label: str,
    request_factory: Callable[[], Awaitable[T]],
    *,
    retry_rate_limits: bool = True,
    max_attempts: int | None = None,
) -> T:
    """Call the newest retry API while remaining usable on older AstrBot cores."""
    retry_impl = _core_retry_provider_request or _legacy_retry_provider_request
    parameters = inspect.signature(retry_impl).parameters
    kwargs = {"max_attempts": max_attempts}
    if "retry_rate_limits" in parameters:
        kwargs["retry_rate_limits"] = retry_rate_limits
    return await retry_impl(provider_label, request_factory, **kwargs)
