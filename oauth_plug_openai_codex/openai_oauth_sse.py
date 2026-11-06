# SPDX-FileCopyrightText: 2022-2099 Soulter
# SPDX-License-Identifier: AGPL-3.0-only

"""Bounded JSON Server-Sent Events parser adapted from AstrBot's OAuth SSE."""

import json
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

DEFAULT_MAX_EVENT_BYTES = 4 * 1024 * 1024


def iter_json_sse_text(text: str, *, max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES) -> Iterator[dict[str, Any]]:
    """Decode a buffered SSE response with the same framing as a live stream."""
    if max_event_bytes <= 0:
        raise ValueError("SSE 事件大小上限必须大于零。")
    data_lines: list[str] = []
    data_bytes = 0
    for line in [*re.split(r"\r\n|\r|\n", text.removeprefix("\ufeff")), ""]:
        if line == "":
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            data_lines = []
            data_bytes = 0
            if not payload.strip():
                continue
            if payload.strip() == "[DONE]":
                return
            try:
                event = json.loads(payload)
            except (json.JSONDecodeError, UnicodeError):
                raise ValueError("OpenAI OAuth SSE 事件包含无效 JSON。") from None
            if not isinstance(event, dict):
                raise ValueError("OpenAI OAuth SSE 事件必须是 JSON 对象。")
            yield event
            continue
        field, separator, value = line.partition(":")
        if field != "data":
            continue
        value = value.removeprefix(" ") if separator else ""
        data_bytes += len(value.encode("utf-8")) + bool(data_lines)
        if data_bytes > max_event_bytes:
            raise ValueError("OpenAI OAuth SSE 事件超过大小上限。")
        data_lines.append(value)


async def iter_json_sse_events(
    lines: AsyncIterator[str],
    *,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON objects from an asynchronous stream of SSE lines.

    Args:
        lines: SSE lines with or without trailing newline characters.
        max_event_bytes: Maximum UTF-8 byte size of one joined ``data`` event.

    Yields:
        Each decoded JSON object, in stream order.

    Raises:
        ValueError: If the size limit is invalid, an event is too large, or an
            event does not contain a valid JSON object.
    """
    if max_event_bytes <= 0:
        raise ValueError("SSE 事件大小上限必须大于零。")

    iterator = lines.__aiter__()
    data_lines: list[str] = []
    data_bytes = 0
    first_line = True

    while True:
        try:
            line = await anext(iterator)
            reached_eof = False
        except StopAsyncIteration:
            line = ""
            reached_eof = True

        if first_line:
            line = line.removeprefix("\ufeff")
            first_line = False
        line = line.removesuffix("\n").removesuffix("\r")

        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                data_bytes = 0

                if not payload.strip():
                    if reached_eof:
                        return
                    continue
                if payload.strip() == "[DONE]":
                    return

                try:
                    event = json.loads(payload)
                except (json.JSONDecodeError, UnicodeError):
                    raise ValueError("OpenAI OAuth SSE 事件包含无效 JSON。") from None
                if not isinstance(event, dict):
                    raise ValueError("OpenAI OAuth SSE 事件必须是 JSON 对象。")
                yield event

            if reached_eof:
                return
            continue

        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field != "data":
            continue

        encoded_size = len(value.encode("utf-8"))
        if data_lines:
            encoded_size += 1
        data_bytes += encoded_size
        if data_bytes > max_event_bytes:
            raise ValueError("OpenAI OAuth SSE 事件超过大小上限。")
        data_lines.append(value)
