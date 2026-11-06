"""Compatibility shims for AstrBot provider APIs that changed across releases."""

from __future__ import annotations

import inspect
import uuid
from contextlib import nullcontext
from collections.abc import Awaitable, Callable, Collection
from contextvars import ContextVar
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from astrbot.core.provider.entities import TokenUsage

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
    ledger = current_attempt_ledger.get()
    factory = request_factory
    if isinstance(ledger, _FallbackUsageAttemptLedger):
        async def observed_factory():
            ledger.begin_attempt()
            try:
                return await request_factory()
            except BaseException as error:
                ledger.note_failure(error)
                raise
        factory = observed_factory
    return await retry_impl(provider_label, factory, **kwargs)


async def record_provider_usage(provider, db, **kwargs) -> bool:
    """Record through the new core ledger when that API is available."""
    try:
        from astrbot.core.provider.usage_recorder import record_usage
    except ImportError:
        return False
    parameters = inspect.signature(record_usage).parameters
    if kwargs.get("replace_snapshot") and not {
        "replace_snapshot", "usage_snapshot_fields"
    }.issubset(parameters):
        # An insert-only recorder cannot safely emulate terminal upserts.
        return False
    for optional in ("_context", "replace_snapshot", "usage_snapshot_fields"):
        if optional not in parameters:
            kwargs.pop(optional, None)
    await record_usage(provider, db, **kwargs)
    return True


def _valid_token_usage(usage) -> bool:
    if usage is None:
        return False
    from astrbot.core.provider.entities import TokenUsage

    return isinstance(usage, TokenUsage) and all(
        type(value) is int and 0 <= value <= 2**63 - 1
        for value in (usage.input_other, usage.input_cached, usage.output)
    )


def _fallback_token_usage(*, input_other=0, input_cached=0, output=0, is_partial=False):
    from astrbot.core.provider.entities import TokenUsage

    try:
        return TokenUsage(input_other=input_other, input_cached=input_cached, output=output, is_partial=is_partial)
    except TypeError:
        usage = TokenUsage(input_other=input_other, input_cached=input_cached, output=output)
        try:
            usage.is_partial = is_partial
        except (AttributeError, TypeError):
            pass
        return usage


def _add_fallback_usage(left, right):
    return _fallback_token_usage(
        input_other=left.input_other + right.input_other,
        input_cached=left.input_cached + right.input_cached,
        output=left.output + right.output,
        is_partial=getattr(left, "is_partial", False) or getattr(right, "is_partial", False),
    )


class _FallbackUsageAttemptLedger:
    """Keep failed-attempt usage separate from the final prompt's token usage."""

    def __init__(self, provider_key: int | None = None) -> None:
        """Initialize an independent logical call ledger.

        Args:
            provider_key: Owning provider identity for nested-call isolation.
        """
        self.provider_key = provider_key
        self.usage: TokenUsage | None = None
        self.unknown = False
        self.errors: set[str] = set()
        self.observed_usage: TokenUsage | None = None
        self.snapshot_complete = False
        self.snapshot_terminal_received = False
        self.snapshot_response_id: str | None = None
        self.snapshot_reported_fields: frozenset[str] | None = None
        self.snapshot_last_usage: TokenUsage | None = None
        self.closed = False

    def begin_attempt(self) -> None:
        """Clear the current snapshot before a distinct backend attempt starts."""
        self.observed_usage = None
        self.snapshot_complete = False
        self.snapshot_terminal_received = False
        self.snapshot_response_id = None
        self.snapshot_reported_fields = None
        self.snapshot_last_usage = None

    def note_failure(self, error: BaseException, *, new_attempt: bool = False) -> None:
        """Observe an exception once, even when several retry layers see it.

        Args:
            error: Failure of one actual or possibly submitted attempt.
            new_attempt: Start a new identity even if a caller reused an exception.
        """
        from .context_budget import ContextBudgetExceeded

        if isinstance(error, ContextBudgetExceeded):
            return
        if getattr(error, "_astrbot_usage_recorded", False):
            return
        identity = getattr(error, "_astrbot_usage_attempt_id", None)
        if (
            new_attempt
            or not isinstance(identity, str)
            or not identity
            or len(identity) > 128
        ):
            identity = str(uuid.uuid4())
            try:
                setattr(error, "_astrbot_usage_attempt_id", identity)
            except (AttributeError, TypeError):
                identity = f"exception:{id(error)}"
        if identity in self.errors:
            return
        self.errors.add(identity)
        usage = getattr(error, "_astrbot_token_usage", None)
        if not _valid_token_usage(usage):
            usage = self.observed_usage
        elif (
            getattr(usage, "is_partial", False)
            and self.observed_usage is not None
            and (
                self.snapshot_complete
                or (
                    self.snapshot_reported_fields is not None
                    and self.snapshot_last_usage is not None
                    and (usage.input_other, usage.input_cached, usage.output)
                    == (
                        self.snapshot_last_usage.input_other,
                        self.snapshot_last_usage.input_cached,
                        self.snapshot_last_usage.output,
                    )
                )
            )
        ):
            usage = self.observed_usage
        self.observed_usage = None
        self.snapshot_complete = False
        self.snapshot_response_id = None
        self.snapshot_reported_fields = None
        self.snapshot_last_usage = None
        if not _valid_token_usage(usage):
            self.unknown = True
        else:
            self.usage = _add_fallback_usage(self.usage or _fallback_token_usage(), usage)

    def combined(
        self, usage: TokenUsage | None, error: BaseException | None = None
    ) -> TokenUsage | None:
        """Return ledger totals without changing the response's usage object.

        Args:
            usage: Final attempt's reported usage, when available.
            error: Final exception, to avoid counting its observed usage twice.

        Returns:
            Known total marked partial for unknown attempts, or None if all unknown.
        """
        if not _valid_token_usage(usage) or (
            error is not None and getattr(error, "_astrbot_usage_recorded", False)
        ):
            usage = None
        elif (
            getattr(usage, "is_partial", False)
            and self.observed_usage is not None
            and (
                self.snapshot_complete
                or (
                    self.snapshot_reported_fields is not None
                    and self.snapshot_last_usage is not None
                    and (usage.input_other, usage.input_cached, usage.output)
                    == (
                        self.snapshot_last_usage.input_other,
                        self.snapshot_last_usage.input_cached,
                        self.snapshot_last_usage.output,
                    )
                )
            )
        ):
            usage = self.observed_usage
        identity = getattr(error, "_astrbot_usage_attempt_id", None)
        already_observed = isinstance(identity, str) and identity in self.errors
        if usage is None and not already_observed:
            usage = self.observed_usage
        total = self.usage
        if not already_observed and usage is not None:
            total = _add_fallback_usage(total or _fallback_token_usage(), usage)
        if total is None:
            return None
        return _fallback_token_usage(
            input_other=total.input_other,
            input_cached=total.input_cached,
            output=total.output,
            is_partial=getattr(total, "is_partial", False)
            or self.unknown
            or (usage is None and not already_observed),
        )


try:
    from astrbot.core.provider import usage_recorder as _usage_recorder
except ImportError:
    _usage_recorder = None


def _fallback_note_failed_attempt(error, *, new_attempt=False):
    ledger = current_attempt_ledger.get()
    if ledger is not None and not ledger.closed:
        ledger.note_failure(error, new_attempt=new_attempt)


def _fallback_observe_usage_snapshot(
    usage: TokenUsage | None,
    *,
    response_id: str | None = None,
    complete: bool = False,
    reported_fields: Collection[str] | None = None,
) -> None:
    """Publish a request-local snapshot without recording or summing a new call.

    Args:
        usage: Host TokenUsage from actual backend fields, never an estimate.
        response_id: Optional upstream identity for the current response snapshot.
        complete: Whether this snapshot came from a terminal backend event.
        reported_fields: Optional input/output direction presence for partial updates.

    This is a no-op outside an instrumented request or after its cleanup. A
    producer task inherits the mutable request sink when created under the
    provider call. Producers must publish before their close operation returns.
    Repeated snapshots replace, never add. Different response IDs alone do not
    imply extra billable calls; retries use the independent attempt ledger.
    """
    ledger = current_attempt_ledger.get()
    if ledger is None or ledger.closed or not _valid_token_usage(usage):
        return
    fields = (
        frozenset(
            value
            for value in reported_fields
            if isinstance(value, str) and value in {"input", "output"}
        )
        if isinstance(reported_fields, Collection)
        and not isinstance(reported_fields, (str, bytes))
        else None
    )
    if fields == frozenset():
        return
    identity = (
        response_id
        if isinstance(response_id, str) and len(response_id) <= 256
        else None
    )
    same_response = identity == ledger.snapshot_response_id
    fully_reported = (
        bool(complete)
        and not getattr(usage, "is_partial", False)
        and (fields is None or fields == {"input", "output"})
    )
    if same_response and ledger.snapshot_complete and not fully_reported:
        return
    ledger.snapshot_last_usage = _fallback_token_usage(
        input_other=usage.input_other,
        input_cached=usage.input_cached,
        output=usage.output,
        is_partial=getattr(usage, "is_partial", False),
    )
    previous = ledger.observed_usage
    if (
        not fully_reported
        and same_response
        and fields is not None
        and previous is not None
    ):
        ledger.observed_usage = _fallback_token_usage(
            input_other=usage.input_other
            if "input" in fields
            else previous.input_other,
            input_cached=usage.input_cached
            if "input" in fields
            else previous.input_cached,
            output=usage.output if "output" in fields else previous.output,
            is_partial=True,
        )
        ledger.snapshot_reported_fields = (
            ledger.snapshot_reported_fields or frozenset()
        ) | fields
    else:
        ledger.observed_usage = _fallback_token_usage(
            input_other=usage.input_other,
            input_cached=usage.input_cached,
            output=usage.output,
            is_partial=not fully_reported,
        )
        ledger.snapshot_reported_fields = fields or (
            frozenset({"input", "output"}) if fully_reported else None
        )
    ledger.snapshot_complete = fully_reported
    ledger.snapshot_terminal_received = (
        ledger.snapshot_terminal_received if same_response else False
    ) or bool(complete)
    ledger.snapshot_response_id = identity


UsageAttemptLedger = getattr(
    _usage_recorder, "UsageAttemptLedger", _FallbackUsageAttemptLedger
)
current_attempt_ledger = getattr(
    _usage_recorder, "current_attempt_ledger",
    ContextVar("oauth_legacy_attempt_ledger", default=None),
)
note_failed_attempt = getattr(_usage_recorder, "note_failed_attempt", _fallback_note_failed_attempt)
observe_usage_snapshot = getattr(_usage_recorder, "observe_usage_snapshot", _fallback_observe_usage_snapshot)
observe_attempt_usage = getattr(
    _usage_recorder, "observe_attempt_usage", observe_usage_snapshot
)


def usage_scope(**metadata):
    scope = getattr(_usage_recorder, "usage_scope", None)
    fields = getattr(capture_usage_context(), "__dataclass_fields__", None)
    if fields is not None:
        metadata = {key: value for key, value in metadata.items() if key in fields}
    return scope(**metadata) if scope is not None else nullcontext()


def capture_usage_context():
    context = getattr(_usage_recorder, "_current_usage_context", None)
    return context.get() if context is not None else None
