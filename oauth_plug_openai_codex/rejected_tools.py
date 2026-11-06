"""Keep invalid model calls separate from executable tool arguments."""

import json
import math
from typing import Any


REJECTION_CODES = frozenset(
    {"missing_arguments", "invalid_json", "non_object_arguments", "non_json_values"}
)
REJECTION_NOTICE = "部分工具调用因参数无效而未执行。"
LEGACY_REJECTION_NOTICE = (
    "部分工具调用因参数无效而未执行；当前宿主不会自动修正这些调用。"
)


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item)
            for key, item in value.items()
        )
    return False


def parse_tool_arguments(arguments: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return executable arguments or a fixed code, never a wrapper payload."""
    if arguments is None:
        return None, "missing_arguments"
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (ValueError, TypeError, RecursionError):
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "non_object_arguments"
    try:
        if not _is_json_value(parsed):
            return None, "non_json_values"
    except RecursionError:
        return None, "non_json_values"
    return parsed, None


def rejected_tool_call(name: Any, call_id: Any, code: str) -> dict[str, str]:
    """Bound identifiers and codes before exposing rejection metadata."""
    def identifier(value: Any) -> str:
        if not isinstance(value, str) or not 0 < len(value) <= 128:
            return ""
        if not all(
            char.isascii() and (char.isalnum() or char in "_.-") for char in value
        ):
            return ""
        return value

    return {
        "name": identifier(name),
        "call_id": identifier(call_id),
        "code": code if code in REJECTION_CODES else "invalid_json",
    }


def apply_tool_rejections(
    response: Any,
    rejected: list[dict[str, str]],
    output_text: str,
    *,
    has_valid_calls: bool,
) -> str:
    """Attach safe metadata without requiring a newer response constructor.

    Legacy runners do not process this metadata. Their response gets an explicit
    notice, with no provider-side retries. In mixed responses the legacy runner
    can replace this text after executing valid calls; callers needing guaranteed
    correction should use a host runner supporting rejected_tool_calls.
    """
    if not rejected:
        return output_text
    native = any(
        "rejected_tool_calls" in getattr(cls, "__dataclass_fields__", {})
        or "rejected_tool_calls" in getattr(cls, "__annotations__", {})
        for cls in type(response).__mro__
    )
    safe = [
        rejected_tool_call(item.get("name"), item.get("call_id"), item.get("code", ""))
        for item in rejected[:32]
    ]
    try:
        setattr(response, "rejected_tool_calls", safe)
    except (AttributeError, TypeError, ValueError):
        native = False
    if not native:
        return (
            f"{output_text}\n\n{LEGACY_REJECTION_NOTICE}"
            if output_text else LEGACY_REJECTION_NOTICE
        )
    if not output_text and not has_valid_calls:
        return REJECTION_NOTICE
    return output_text
