# SPDX-FileCopyrightText: 2022-2099 Soulter
# SPDX-License-Identifier: AGPL-3.0-only

"""Independent Codex OAuth provider adapted from AstrBot production source."""

import asyncio
import copy
import json
import math
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from astrbot import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse, TokenUsage
from .oauth import refresh_access_token
from .openai_oauth_audio import OpenAIOAuthAudioMixin
from .openai_oauth_shared_state import OpenAIOAuthSharedState
from .openai_oauth_sse import iter_json_sse_events, iter_json_sse_text
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from .provider_runtime_compat import (
    ProviderRequestError,
    db_helper,
    provider_oauth_web_search,
    provider_stats_managed_by_agent,
    record_provider_usage,
    UsageAttemptLedger,
    current_attempt_ledger,
    note_failed_attempt,
    observe_attempt_usage,
    retry_provider_request,
)

from .context_budget import resolve_context_budget, trim_context_to_budget
from .headers import build_codex_backend_headers
from .image_media import decode_result, reference_image_url
from .rejected_tools import apply_tool_rejections, parse_tool_arguments, rejected_tool_call
from .service import OAUTH_PLACEHOLDER_KEY, get_service

oauth_provider_stat_kind: ContextVar[str] = ContextVar(
    "oauth_provider_stat_kind",
    default="text",
)
IMAGE_WEBSOCKET_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
IMAGE_WEBSOCKET_MAX_TRANSCRIPT_BYTES = 128 * 1024 * 1024
IMAGE_WEBSOCKET_MAX_EVENTS = 16384
OAUTH_IMAGE_MODEL_ALIASES = frozenset(
    {"gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst",
     "gpt-image-2.6-flare", "gpt-image-2.6-sunburst"}
)


@asynccontextmanager
async def _usage_bound_closing(stream, ledger):
    try:
        yield stream
    finally:
        token = current_attempt_ledger.set(ledger)
        try:
            await stream.aclose()
        finally:
            current_attempt_ledger.reset(token)


class OpenAIOAuthImageStreamError(RuntimeError):
    """Sanitized image transport failure with a safe retry indicator."""

    def __init__(
        self,
        reason_code: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"Codex image WebSocket request ended: {reason_code}.")
        self.reason_code = reason_code
        self.status_code = status_code
        self.retryable = retryable


@dataclass
class OAuthPlugImageResult:
    path: str
    mime_type: str = "image/png"
    revised_prompt: str = ""
    raw: dict[str, Any] | None = None
    frame_count: int = 1
    is_animated: bool = False


class ProviderOAuthPlugOpenAICodex(OpenAIOAuthAudioMixin, ProviderOpenAIOfficial):
    capabilities = {
        "chat": True,
        "stream": True,
        "vision_input": True,
        "function_call": True,
        "reasoning": True,
        "image_generate": True,
        "image_edit": True,
        "image_websocket": True,
    }
    model_capabilities = {
        "gpt-6.1-terra": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": ("none", "low", "medium", "high", "xhigh", "max"),
        },
        "gpt-6.1-sol": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": (
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            ),
        },
        "gpt-6-astra": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": (
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            ),
        },
        "gpt-6-sol": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": (
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            ),
        },
        "gpt-6-luna": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": (
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            ),
        },
        "gpt-5.6-sol": {
            "default_reasoning_effort": "low",
            "supported_reasoning_efforts": (
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            ),
        },
        "gpt-5.6-terra": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": (
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            ),
        },
        "gpt-5.6-luna": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": (
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            ),
        },
        "gpt-5.5": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": (
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
            ),
        },
        "gpt-5.4": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": (
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
            ),
        },
        "gpt-5.4-mini": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": (
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
            ),
        },
        "gpt-5.3-codex-spark": {
            "default_reasoning_effort": "high",
            "supported_reasoning_efforts": (
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
            ),
        },
        "gpt-5.3-codex": {
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": (
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
            ),
        },
    }

    def __init__(self, provider_config, provider_settings) -> None:
        service = get_service()
        self._oauth_service = service
        provider_config = (
            service.build_provider_config(provider_config)
            if service is not None
            else dict(provider_config)
        )
        patched_config = dict(provider_config)
        patched_config.pop("oauth_shared_state", None)
        patched_config["key"] = [OAUTH_PLACEHOLDER_KEY]
        self._oauth_context_config = copy.deepcopy({
            key: provider_config[key] for key in (
                "max_context_tokens", "max_tokens", "max_completion_tokens",
                "max_output_tokens", "custom_extra_body",
            ) if key in provider_config
        })
        super().__init__(patched_config, provider_settings)
        self.provider_config = dict(provider_config)
        shared_state = self.provider_config.pop("oauth_shared_state", None)
        if isinstance(shared_state, OpenAIOAuthSharedState):
            self._oauth_shared_state = shared_state
        else:
            source_id = str(
                self.provider_config.get("provider_source_id")
                or self.provider_config.get("id")
                or "openai_oauth"
            )
            self._oauth_shared_state = OpenAIOAuthSharedState(
                source_id,
                self.provider_config,
            )
        self.provider_config["key"] = [OAUTH_PLACEHOLDER_KEY]
        self.api_keys = [OAUTH_PLACEHOLDER_KEY]
        self.chosen_api_key = ""
        self.account_id = (
            self.provider_config.get("oauth_account_id")
            or self.provider_config.get("account_id")
            or ""
        ).strip()
        self.base_url = (
            self.provider_config.get("api_base")
            or "https://chatgpt.com/backend-api/codex"
        ).rstrip("/")
        self._oauth_refresh_lock = self._oauth_shared_state.refresh_lock
        self._oauth_refresh_skew_seconds = int(
            self.provider_config.get("oauth_refresh_skew_seconds") or 300
        )
        self._sync_oauth_credentials_from_shared()
        if service is not None:
            service.register_provider(self)

    async def get_model_context_metadata(self, model: str) -> dict[str, int]:
        from .model_catalog import get_model_context_metadata

        return await get_model_context_metadata(self, model)

    async def get_models(self):
        from .model_catalog import discover_models

        if getattr(self, "_oauth_media_closed", False):
            raise RuntimeError("OAuth provider is closed")
        service = getattr(self, "_oauth_service", None) or get_service()
        if service is not None:
            fallback = service.get_models()
        else:
            configured_model = str(self.provider_config.get("model") or "").strip()
            fallback = [configured_model] if configured_model else list(self.model_capabilities)
        if self.provider_config.get("oauth_auto_models", True) is False:
            return fallback
        return await discover_models(self, fallback)

    async def _prepare_chat_payload(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Preserve per-request reasoning controls for Codex Responses.

        Args:
            *args: Positional arguments forwarded to the OpenAI payload builder.
            **kwargs: Keyword arguments forwarded to the OpenAI payload builder.

        Returns:
            The prepared request payload and normalized message context.
        """
        payloads, context_query = await super()._prepare_chat_payload(
            *args,
            **kwargs,
        )
        for key in ("reasoning_effort", "reasoning", "response_format"):
            if kwargs.get(key) is not None:
                payloads[key] = kwargs[key]
        if kwargs.get("oauth_web_search") is not None:
            payloads["_oauth_web_search"] = kwargs["oauth_web_search"]
        if kwargs.get("retry_rate_limits") is not None:
            payloads["_oauth_retry_rate_limits"] = kwargs["retry_rate_limits"]
        return payloads, context_query

    def _parse_oauth_expires_at(self) -> datetime | None:
        value = (self.provider_config.get("oauth_expires_at") or "").strip()
        if not value:
            return None
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            return None

    def _oauth_expiring_soon(self) -> bool:
        self._sync_oauth_credentials_from_shared()
        expires_at = self._parse_oauth_expires_at()
        if expires_at is None:
            return False
        refresh_at = datetime.now(timezone.utc) + timedelta(
            seconds=self._oauth_refresh_skew_seconds
        )
        return expires_at <= refresh_at

    def _sync_oauth_credentials_from_shared(self) -> None:
        if not hasattr(self, "_oauth_shared_state"):
            source_id = str(
                self.provider_config.get("provider_source_id")
                or self.provider_config.get("id")
                or "oauth_plug_openai_codex"
            )
            self._oauth_shared_state = OpenAIOAuthSharedState(
                source_id,
                self.provider_config,
            )
            self._oauth_refresh_lock = self._oauth_shared_state.refresh_lock
        self.provider_config.update(self._oauth_shared_state.snapshot())
        self.account_id = str(
            self.provider_config.get("oauth_account_id")
            or self.provider_config.get("account_id")
            or ""
        ).strip()
        self.api_keys = [OAUTH_PLACEHOLDER_KEY]
        self.chosen_api_key = ""
        self.client.api_key = OAUTH_PLACEHOLDER_KEY

    def _apply_oauth_token_to_runtime(self, token: dict[str, Any]) -> None:
        access_token = str(token.get("access_token") or "").strip()
        refresh_token = str(token.get("refresh_token") or "").strip()
        patch: dict[str, Any] = {}
        if access_token:
            patch["oauth_access_token"] = access_token
        if refresh_token:
            patch["oauth_refresh_token"] = refresh_token
        patch["oauth_expires_at"] = str(token.get("expires_at") or "")
        patch["oauth_account_email"] = str(
            token.get("email") or ""
        ) or self.provider_config.get("oauth_account_email", "")
        patch["oauth_account_id"] = str(
            token.get("account_id") or ""
        ) or self.provider_config.get("oauth_account_id", "")
        self._oauth_shared_state.apply(patch)
        self._sync_oauth_credentials_from_shared()

    async def _refresh_oauth_token(self) -> bool:
        self._sync_oauth_credentials_from_shared()
        refresh_version, credentials = self._oauth_shared_state.versioned_snapshot()
        refresh_token_value = str(credentials.get("oauth_refresh_token") or "").strip()
        if not refresh_token_value:
            return False

        service = getattr(self, "_oauth_service", None)
        if service is not None:
            token = await service.refresh(refresh_version)
        else:
            token = await refresh_access_token(
                refresh_token_value,
                self.provider_config.get("proxy", ""),
            )
        if self._oauth_shared_state.version != refresh_version:
            self._sync_oauth_credentials_from_shared()
            return True
        self._apply_oauth_token_to_runtime(token)

        if service is not None:
            await service.persist_token(token)
        return True

    async def _ensure_fresh_oauth_token(self) -> None:
        self._sync_oauth_credentials_from_shared()
        if not self._oauth_expiring_soon():
            return
        if getattr(self, "_oauth_service", None) is not None:
            await self._refresh_oauth_token()
            return
        async with self._oauth_refresh_lock:
            self._sync_oauth_credentials_from_shared()
            if not self._oauth_expiring_soon():
                return
            await self._refresh_oauth_token()

    async def _refresh_after_auth_failure(self, attempted_version: int) -> bool:
        if getattr(self, "_oauth_service", None) is not None:
            self._sync_oauth_credentials_from_shared()
            if self._oauth_shared_state.version != attempted_version:
                return True
            return await self._refresh_oauth_token()
        async with self._oauth_refresh_lock:
            self._sync_oauth_credentials_from_shared()
            if self._oauth_shared_state.version != attempted_version:
                return True
            return await self._refresh_oauth_token()

    def _build_backend_headers(self) -> dict[str, str]:
        headers, _version = self._build_backend_headers_with_version()
        return headers

    def _build_backend_headers_with_version(self) -> tuple[dict[str, str], int]:
        self._sync_oauth_credentials_from_shared()
        attempted_version, credentials = self._oauth_shared_state.versioned_snapshot()
        self.provider_config.update(credentials)
        access_token = str(credentials.get("oauth_access_token") or "").strip()
        account_id = (
            str(credentials.get("oauth_account_id") or "") or self.account_id
        ).strip()
        if not access_token:
            raise Exception("当前 OAuth Source 尚未绑定 access token")
        if not account_id:
            raise Exception(
                "当前 OAuth Source 缺少 chatgpt_account_id，请重新绑定或导入完整 JSON 凭据"
            )

        custom_headers = self.provider_config.get("custom_headers")
        headers = build_codex_backend_headers(
            access_token,
            account_id,
            custom_headers=custom_headers if isinstance(custom_headers, dict) else None,
        )
        return headers, attempted_version

    async def _request_backend_once(
        self,
        payload: dict[str, Any],
    ) -> tuple[int, str, int]:
        self._sync_oauth_credentials_from_shared()
        headers = self._build_backend_headers()
        attempted_version = self._oauth_shared_state.version

        async with self._open_oauth_http_client(follow_redirects=True) as client:
            response = await client.post(
                f"{self.base_url}/responses",
                headers=headers,
                json=payload,
            )
            raw_text = await response.aread()
        text = raw_text.decode("utf-8", errors="replace")
        return response.status_code, text, attempted_version

    async def _request_backend(self, payload: dict[str, Any]) -> dict[str, Any]:
        if getattr(self, "_oauth_media_closed", False):
            raise RuntimeError("OAuth provider is closed")
        await self._ensure_fresh_oauth_token()
        status_code, text, attempted_version = await self._request_backend_once(payload)

        if status_code in {401, 403}:
            auth_error = self._http_response_error(status_code, text)
            refreshed = await self._refresh_after_auth_failure(attempted_version)
            if refreshed:
                note_failed_attempt(auth_error)
                ledger = current_attempt_ledger.get()
                if ledger is not None:
                    ledger.begin_attempt()
                (
                    status_code,
                    text,
                    _attempted_version,
                ) = await self._request_backend_once(payload)

        if status_code < 200 or status_code >= 300:
            raise self._http_response_error(status_code, text)
        return self._parse_backend_response(text)


    @asynccontextmanager
    async def _open_oauth_http_client(
        self,
        *,
        follow_redirects: bool,
        timeout: float | None = None,
    ):
        if getattr(self, "_oauth_media_closed", False):
            raise RuntimeError("OAuth provider is closed")
        if not hasattr(self, "_oauth_stream_clients"):
            self._oauth_stream_clients = set()
        client = httpx.AsyncClient(
            proxy=self.provider_config.get("proxy") or None,
            timeout=self.timeout if timeout is None else timeout,
            follow_redirects=follow_redirects,
        )
        self._oauth_stream_clients.add(client)
        try:
            async with client:
                yield client
        finally:
            self._oauth_stream_clients.discard(client)

    @asynccontextmanager
    async def _open_oauth_stream_client(self):
        async with self._open_oauth_http_client(follow_redirects=False) as client:
            yield client

    async def _stream_backend_events(
        self,
        payload: dict[str, Any],
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield decoded SSE events and refresh OAuth only before any output."""
        if getattr(self, "_oauth_media_closed", False):
            raise RuntimeError("OAuth provider is closed")
        await self._ensure_fresh_oauth_token()
        auth_retry_available = True
        while True:
            headers, attempted_version = self._build_backend_headers_with_version()
            retry_after_auth = False
            async with self._open_oauth_stream_client() as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/responses",
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code == 401 and auth_retry_available:
                        raw_body = await response.aread()
                        text = raw_body.decode("utf-8", errors="replace")
                        auth_error = self._http_response_error(
                            response.status_code, text
                        )
                        refreshed = await self._refresh_after_auth_failure(
                            attempted_version
                        )
                        if refreshed:
                            note_failed_attempt(auth_error)
                            ledger = current_attempt_ledger.get()
                            if ledger is not None:
                                ledger.begin_attempt()
                            auth_retry_available = False
                            retry_after_auth = True
                        else:
                            raise auth_error
                    elif response.status_code < 200 or response.status_code >= 300:
                        raw_body = await response.aread()
                        text = raw_body.decode("utf-8", errors="replace")
                        raise self._http_response_error(response.status_code, text)
                    else:
                        try:
                            async for event in iter_json_sse_events(
                                response.aiter_lines()
                            ):
                                event_payload = event.get("response")
                                event_payload = (
                                    event_payload
                                    if isinstance(event_payload, dict)
                                    else event
                                )
                                self._observe_backend_usage(
                                    event_payload,
                                    response_id=event.get("response_id"),
                                    complete=event.get("type")
                                    in {
                                        "response.completed",
                                        "response.failed",
                                        "response.incomplete",
                                        "response.error",
                                        "error",
                                    },
                                )
                                yield event
                                if event.get("type") in {
                                    "response.completed",
                                    "response.error",
                                    "response.failed",
                                    "response.incomplete",
                                    "error",
                                }:
                                    return
                        except ValueError:
                            logger.warning(
                                "Codex backend SSE rejected 1 invalid event."
                            )
                            raise ValueError(
                                "Codex backend SSE event is invalid."
                            ) from None
            if retry_after_auth:
                continue
            return


    async def _request_image_backend_once(
        self,
        payload: dict[str, Any],
        request_timeout: float | None = None,
    ) -> tuple[int, str, int]:
        self._sync_oauth_credentials_from_shared()
        headers = self._build_backend_headers()
        attempted_version = self._oauth_shared_state.version

        text_parts: list[str] = []
        async with self._open_oauth_http_client(
            follow_redirects=True,
            timeout=request_timeout,
        ) as client:
            async with client.stream(
                "POST", f"{self.base_url}/responses", headers=headers, json=payload,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    text_parts.append((await response.aread()).decode("utf-8", errors="replace"))
                else:
                    async def captured_lines():
                        total_bytes = 0
                        async for line in response.aiter_lines():
                            total_bytes += len(line.encode("utf-8")) + 1
                            if total_bytes > IMAGE_WEBSOCKET_MAX_TRANSCRIPT_BYTES:
                                raise ValueError("Codex image response exceeds size limit")
                            text_parts.append(line)
                            yield line

                    try:
                        async with aclosing(iter_json_sse_events(
                            captured_lines(), max_event_bytes=IMAGE_WEBSOCKET_MAX_MESSAGE_BYTES,
                        )) as events:
                            async for event in events:
                                event_payload = event.get("response")
                                event_payload = event_payload if isinstance(event_payload, dict) else event
                                self._observe_backend_usage(
                                    event_payload, response_id=event.get("response_id"),
                                    complete=event.get("type") in {"response.completed", "response.failed", "response.incomplete", "response.error", "error"},
                                )
                                if event.get("type") in {
                                    "response.completed", "response.error", "response.failed",
                                    "response.incomplete", "error",
                                }:
                                    break
                    except ValueError:
                        logger.warning("Codex image SSE rejected 1 invalid event.")
                        raise ValueError("Codex image SSE event is invalid.") from None

        return response.status_code, "\n".join(text_parts), attempted_version

    async def _request_image_backend(
        self,
        payload: dict[str, Any],
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        if getattr(self, "_oauth_media_closed", False):
            raise RuntimeError("OAuth provider is closed")
        await self._ensure_fresh_oauth_token()
        first_result = await self._request_image_backend_once(payload, request_timeout)
        if len(first_result) == 2:
            status_code, text = first_result
            self._sync_oauth_credentials_from_shared()
            attempted_version = self._oauth_shared_state.version
        else:
            status_code, text, attempted_version = first_result

        if status_code in {401, 403}:
            auth_error = self._http_response_error(status_code, text)
            refreshed = await self._refresh_after_auth_failure(attempted_version)
            if refreshed:
                note_failed_attempt(auth_error)
                ledger = current_attempt_ledger.get()
                if ledger is not None:
                    ledger.begin_attempt()
                retry_result = await self._request_image_backend_once(
                    payload,
                    request_timeout,
                )
                status_code, text = retry_result[:2]

        if status_code < 200 or status_code >= 300:
            raise self._http_response_error(status_code, text)
        return self._parse_backend_response(text)

    async def _request_image_backend_websocket(
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Submit one image request with a total deadline and no ambiguous replay."""
        import aiohttp

        deadline_seconds = float(self.timeout if timeout is None else timeout)
        if (
            isinstance(timeout, bool)
            or not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0
        ):
            raise ValueError("timeout 必须是有限正浮点数。")
        base = urlsplit(self.base_url)
        if (
            base.scheme not in {"https", "http"}
            or not base.netloc
            or base.query
            or base.fragment
        ):
            raise ValueError("图片 WebSocket 后端地址无效。")
        scheme = "wss" if base.scheme == "https" else "ws"
        url = f"{scheme}://{base.netloc}{base.path.rstrip('/')}/responses"
        submitted = False

        async def reject_redirect(_session, _context, params):
            raise OpenAIOAuthImageStreamError(
                "request_rejected", status_code=params.response.status
            )

        try:
            async with asyncio.timeout(deadline_seconds):
                if getattr(self, "_oauth_media_closed", False):
                    raise OpenAIOAuthImageStreamError("provider_closed")
                await self._ensure_fresh_oauth_token()
                if not hasattr(self, "_oauth_stream_clients"):
                    self._oauth_stream_clients = set()
                for auth_attempt in range(2):
                    if getattr(self, "_oauth_media_closed", False):
                        raise OpenAIOAuthImageStreamError("provider_closed")
                    headers, attempted_version = (
                        self._build_backend_headers_with_version()
                    )
                    trace = aiohttp.TraceConfig()
                    trace.on_request_redirect.append(reject_redirect)
                    resources = AsyncExitStack()
                    self._oauth_stream_clients.add(resources)
                    try:
                        async with resources:
                            session = await resources.enter_async_context(
                                aiohttp.ClientSession(
                                    timeout=aiohttp.ClientTimeout(total=None),
                                    trace_configs=[trace],
                                )
                            )
                            if getattr(self, "_oauth_media_closed", False):
                                raise OpenAIOAuthImageStreamError("provider_closed")
                            websocket = await resources.enter_async_context(
                                session.ws_connect(
                                    url,
                                    headers=headers,
                                    proxy=self.provider_config.get("proxy") or None,
                                    heartbeat=20,
                                    max_msg_size=IMAGE_WEBSOCKET_MAX_MESSAGE_BYTES,
                                )
                            )
                            if getattr(self, "_oauth_media_closed", False):
                                raise OpenAIOAuthImageStreamError("provider_closed")
                            request = {
                                key: value
                                for key, value in payload.items()
                                if key not in {"stream", "background"}
                            }
                            submitted = True
                            await websocket.send_json(
                                {"type": "response.create", **request}
                            )
                            lines: list[str] = []
                            received_bytes = 0
                            async for message in websocket:
                                if message.type != aiohttp.WSMsgType.TEXT:
                                    raise OpenAIOAuthImageStreamError("outcome_unknown")
                                received_bytes += len(message.data.encode("utf-8"))
                                if (
                                    received_bytes
                                    > IMAGE_WEBSOCKET_MAX_TRANSCRIPT_BYTES
                                    or len(lines) >= IMAGE_WEBSOCKET_MAX_EVENTS
                                ):
                                    raise OpenAIOAuthImageStreamError("outcome_unknown")
                                event = json.loads(message.data)
                                if not isinstance(event, dict):
                                    raise OpenAIOAuthImageStreamError("outcome_unknown")
                                lines.append("data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                                event_type = event.get("type")
                                event_payload = event.get("response")
                                self._observe_backend_usage(
                                    event_payload if isinstance(event_payload, dict) else event,
                                    response_id=event.get("response_id"),
                                    complete=event_type in {"response.completed", "response.failed", "response.incomplete", "response.error", "error"},
                                )
                                if event_type in {
                                    "error",
                                    "response.error",
                                    "response.failed",
                                    "response.incomplete",
                                }:
                                    status = self._extract_stream_error_status_code(
                                        event
                                    )
                                    reason = (
                                        "upstream_unavailable"
                                        if status and status >= 500
                                        else "request_rejected"
                                    )
                                    if status == 429:
                                        reason = "rate_limited"
                                    error = OpenAIOAuthImageStreamError(reason, status_code=status)
                                    response_data = event.get("response", event)
                                    if isinstance(response_data, dict):
                                        usage = self._extract_response_usage(response_data.get("usage"))
                                        if usage is not None:
                                            setattr(error, "_astrbot_token_usage", usage)
                                    raise error
                                if event_type == "response.completed":
                                    return self._parse_backend_response(
                                        "\n\n".join(lines)
                                    )
                            raise OpenAIOAuthImageStreamError("outcome_unknown")
                    except aiohttp.WSServerHandshakeError as error:
                        if error.status == 401 and auth_attempt == 0:
                            if await self._refresh_after_auth_failure(
                                attempted_version
                            ):
                                continue
                        reason = (
                            "rate_limited"
                            if error.status == 429
                            else "request_rejected"
                        )
                        raise OpenAIOAuthImageStreamError(
                            reason,
                            status_code=error.status,
                            retryable=error.status == 429 or error.status >= 500,
                        ) from None
                    finally:
                        self._oauth_stream_clients.discard(resources)
        except asyncio.CancelledError:
            raise
        except OpenAIOAuthImageStreamError:
            raise
        except TimeoutError:
            raise OpenAIOAuthImageStreamError(
                "outcome_unknown" if submitted else "timeout",
                retryable=not submitted,
            ) from None
        except (aiohttp.ClientError, OSError):
            raise OpenAIOAuthImageStreamError(
                "outcome_unknown" if submitted else "connection_failed",
                retryable=not submitted,
            ) from None
        except Exception:
            raise OpenAIOAuthImageStreamError(
                "outcome_unknown" if submitted else "provider_failed",
            ) from None
        raise OpenAIOAuthImageStreamError("request_rejected")

    @staticmethod
    def _compact_backend_error(data: Any) -> str:
        # Backend messages, IDs and arbitrary codes can contain prompts or tokens.
        allowed = {
            "rate_limit_exceeded", "rate_limit_error", "insufficient_quota",
            "credit_balance_exhausted", "invalid_api_key", "invalid_request_error",
            "authentication_error", "permission_denied", "server_error",
            "context_length_exceeded", "model_not_found",
        }
        if not isinstance(data, dict):
            return "backend_error"
        response = data.get("response")
        payload = response if isinstance(response, dict) else data
        error = payload.get("error")
        error = error if isinstance(error, dict) else {}
        for value in (error.get("code"), payload.get("code"), error.get("type")):
            if isinstance(value, str) and value in allowed:
                return value
        return "backend_error"

    def _log_backend_failure(self, data: Any, status_code: int | None = None) -> None:
        if not isinstance(status_code, int) or not 100 <= status_code <= 599:
            status_code = None
        logger.warning(
            "Codex backend failure: reason=%s status=%s",
            self._compact_backend_error(data),
            status_code,
        )

    def _backend_response_error(self, data: Any) -> Exception:
        status_code = self._extract_stream_error_status_code(data) if isinstance(data, dict) else None
        self._log_backend_failure(data, status_code)
        message = "Codex backend response failed: " + self._compact_backend_error(data)
        error = ProviderRequestError(message, status_code=status_code) if status_code is not None else RuntimeError(message)
        payload = data.get("response", data) if isinstance(data, dict) else {}
        if isinstance(payload, dict):
            self._observe_backend_usage(payload, complete=True, response_id=data.get("response_id"))
            usage = self._extract_response_usage(payload.get("usage"))
            if usage is not None:
                setattr(error, "_astrbot_token_usage", usage)
        return error

    def _buffered_sse_events(self, text: str):
        observed_usage = None
        try:
            for event in iter_json_sse_text(text, max_event_bytes=IMAGE_WEBSOCKET_MAX_MESSAGE_BYTES):
                payload = event.get("response")
                payload = payload if isinstance(payload, dict) else event
                complete = event.get("type") in {"response.completed", "response.failed", "response.incomplete", "response.error", "error"}
                usage = self._extract_response_usage(payload.get("usage"))
                if usage is not None:
                    observed_usage = usage if complete else self._partial_usage(usage)
                self._observe_backend_usage(payload, complete=complete, response_id=event.get("response_id"))
                yield event
        except ValueError:
            logger.warning("Codex backend SSE rejected 1 invalid event.")
            error = ValueError("Codex backend SSE event is invalid.")
            if observed_usage is not None:
                error._astrbot_token_usage = observed_usage
            raise error from None

    def _format_backend_error(self, status_code: int, text: str) -> str:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
        self._log_backend_failure(data, status_code)
        summary = self._compact_backend_error(data)
        return f"Codex backend request failed: status={status_code}, reason={summary}"

    def _parse_backend_response(self, text: str) -> dict[str, Any]:
        completed_response: dict[str, Any] | None = None
        error_payload: dict[str, Any] | None = None
        output_text_parts: list[str] = []
        output_text_done: str | None = None
        output_items: list[dict[str, Any]] = []
        output_item_ids: set[str] = set()
        stripped = text.lstrip("\ufeff").strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
            except ValueError:
                raise ValueError("Codex backend response contains invalid JSON") from None
            events = [data]
        else:
            events = self._buffered_sse_events(text)
        for event in events:
            response = event.get("response", event)
            if isinstance(response, dict):
                status = response.get("status")
                if status in ("failed", "incomplete", "cancelled"):
                    raise self._backend_response_error(event)
            event_type = event.get("type")
            if event_type in {
                "error",
                "response.error",
                "response.failed",
                "response.incomplete",
            }:
                error_payload = event
            elif event_type == "response.output_text.delta":
                delta = event.get("delta")
                if delta:
                    output_text_parts.append(str(delta))
            elif event_type == "response.output_text.done":
                text_value = event.get("text")
                if text_value is not None:
                    output_text_done = str(text_value)
            elif event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    item_id = str(item.get("id") or "")
                    dedupe_key = item_id or f"index:{len(output_items)}"
                    if dedupe_key not in output_item_ids:
                        output_item_ids.add(dedupe_key)
                        output_items.append(item)
            if event_type == "response.completed":
                response = event.get("response")
                if isinstance(response, dict):
                    completed_response = response
                else:
                    raise ValueError("Codex completed event has no response object")
                completed_response = dict(completed_response)
                completed_response.setdefault("status", "completed")
        merged_output_text = (
            output_text_done
            if output_text_done is not None
            else "".join(output_text_parts)
        )
        if error_payload:
            raise self._backend_response_error(error_payload)
        if completed_response is not None:
            if not completed_response.get("output") and output_items:
                completed_response["output"] = output_items
            if merged_output_text and not completed_response.get("output_text"):
                completed_response["output_text"] = merged_output_text
            return completed_response
        if stripped.startswith("{") and isinstance(data, dict):
            return data
        raise Exception(
            "Codex backend response did not contain response.completed event"
        )

    def _convert_message_content(self, raw_content: Any) -> str | list[dict[str, Any]]:
        if isinstance(raw_content, str):
            return raw_content
        if isinstance(raw_content, dict):
            raw_content = [raw_content]
        if not isinstance(raw_content, list):
            return str(raw_content) if raw_content is not None else ""

        content_parts: list[dict[str, Any]] = []
        for part in raw_content:
            if not isinstance(part, dict):
                raise ValueError(
                    f"OAuth_plug 不支持非对象消息内容：{type(part).__name__}。"
                )
            part_type = part.get("type")
            if part_type == "text":
                content_parts.append(
                    {
                        "type": "input_text",
                        "text": str(part.get("text") or ""),
                    }
                )
            elif part_type == "image_url":
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                if image_url:
                    content_parts.append(
                        {
                            "type": "input_image",
                            "image_url": str(image_url),
                        }
                    )
            else:
                unsupported_type = str(part_type or "unknown")
                raise ValueError(
                    "OAuth_plug 尚未适配消息内容类型 "
                    f"{unsupported_type}，不能静默丢弃。"
                )
        if not content_parts:
            return ""
        if len(content_parts) == 1 and content_parts[0]["type"] == "input_text":
            return content_parts[0]["text"]
        return content_parts

    def _stringify_tool_output(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)

    def _extract_instructions(self, message: dict[str, Any]) -> str:
        content = self._convert_message_content(message.get("content"))
        if isinstance(content, str):
            return content.strip()
        parts: list[str] = []
        for item in content:
            if item.get("type") == "input_text" and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(part for part in parts if part).strip()

    def _convert_messages_to_backend_input(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        instructions_parts: list[str] = []
        response_items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            if role in {"system", "developer"}:
                instruction = self._extract_instructions(message)
                if instruction:
                    instructions_parts.append(instruction)
                continue

            content = message.get("content")
            if role == "tool":
                call_id = str(message.get("tool_call_id") or "").strip()
                if not call_id:
                    logger.warning("检测到缺少 tool_call_id 的工具回传，已忽略。")
                    continue
                response_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": self._stringify_tool_output(content),
                    }
                )
                continue

            tool_calls = message.get("tool_calls") or []
            normalized_role = role if role in {"user", "assistant"} else "user"
            if content not in (None, "", []):
                response_items.append(
                    {
                        "type": "message",
                        "role": normalized_role,
                        "content": self._convert_message_content(content),
                    }
                )

            if role == "assistant" and isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if isinstance(tool_call, str):
                        tool_call = json.loads(tool_call)
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function") or {}
                    name = str(function.get("name") or "").strip()
                    arguments = function.get("arguments") or "{}"
                    call_id = str(tool_call.get("id") or "").strip()
                    if not name or not call_id:
                        continue
                    if not isinstance(arguments, str):
                        arguments = json.dumps(
                            arguments, ensure_ascii=False, default=str
                        )
                    response_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": arguments,
                        }
                    )
        return "\n\n".join(
            part for part in instructions_parts if part
        ).strip(), response_items

    def _extract_response_usage(self, usage: Any) -> TokenUsage | None:
        if usage is None:
            return None
        getter = usage.get if isinstance(usage, dict) else lambda key: getattr(usage, key, None)
        raw_input, raw_output = getter("input_tokens"), getter("output_tokens")
        known_input, known_output = raw_input is not None, raw_output is not None
        if not known_input and not known_output:
            return None
        for value in (raw_input, raw_output):
            if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
                return None
        details = getter("input_tokens_details")
        cached = (details.get("cached_tokens") if isinstance(details, dict) else getattr(details, "cached_tokens", None)) if details is not None else None
        cached = cached if known_input and cached is not None else 0
        if type(cached) is not int or cached < 0 or (known_input and cached > raw_input):
            return None
        values = {"input_other": (raw_input or 0) - cached, "input_cached": cached, "output": raw_output or 0}
        try:
            return TokenUsage(**values, is_partial=not (known_input and known_output))
        except TypeError:
            result = TokenUsage(**values)
            try:
                result.is_partial = not (known_input and known_output)
            except (AttributeError, TypeError):
                pass
            return result

    @staticmethod
    def _partial_usage(usage: TokenUsage | None) -> TokenUsage | None:
        if usage is None:
            return None
        values = {"input_other": usage.input_other, "input_cached": usage.input_cached, "output": usage.output}
        try:
            return TokenUsage(**values, is_partial=True)
        except TypeError:
            result = TokenUsage(**values)
            try:
                result.is_partial = True
            except (AttributeError, TypeError):
                pass
            return result

    async def _record_provider_stat(
        self,
        *,
        request_kind: str,
        status: str,
        usage: TokenUsage | None,
        start_time: float,
        end_time: float,
        model: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Persist one OAuth provider call without affecting its caller.

        Args:
            request_kind: Logical call type used by the synthetic UMO.
            status: Provider call status stored in the database.
            usage: Parsed token usage, or None when the backend omitted it.
            start_time: Epoch time immediately before the public call.
            end_time: Epoch time immediately after the public call.
            model: Explicit request model when supplied.
            session_id: Session identifier when supplied by the caller.
        """
        if getattr(self, "_usage_recording_enabled", False) or request_kind == "transcription":
            recorded = await record_provider_usage(
                self,
                getattr(self, "_usage_recording_db", db_helper),
                usage=usage,
                status=status,
                start_time=start_time,
                end_time=end_time,
                request_kind=request_kind,
                model=model,
            )
            if recorded:
                return
        try:
            if db_helper is None:
                return
            provider_id = str(self.provider_config.get("id") or "").strip()
            if not provider_id:
                try:
                    provider_id = str(self.meta().id).strip()
                except Exception:
                    return
            if not provider_id:
                return
            await db_helper.insert_provider_stat(
                umo=session_id or f"provider:{provider_id}:{request_kind}",
                provider_id=provider_id,
                provider_model=model or self.get_model(),
                status=status,
                stats={
                    "token_usage": ({
                        "input_other": usage.input_other,
                        "input_cached": usage.input_cached,
                        "output": usage.output,
                    } if usage is not None else {}),
                    "usage_status": "missing" if usage is None else ("partial" if getattr(usage, "is_partial", False) else "reported"),
                    "start_time": start_time,
                    "end_time": end_time,
                    "time_to_first_token": 0.0,
                },
                agent_type="test" if request_kind == "test" else "provider",
            )
        except Exception as exc:
            logger.warning(
                "Failed to record OAuth_plug provider statistics (%s).",
                type(exc).__name__,
            )

    def _convert_tools_to_backend_format(
        self, tool_list: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        backend_tools: list[dict[str, Any]] = []
        for tool in tool_list:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") != "function":
                backend_tools.append(tool)
                continue
            function = tool.get("function") or {}
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            backend_tool = {
                "type": "function",
                "name": name,
                "description": str(function.get("description") or "").strip(),
                "parameters": function.get("parameters")
                or {"type": "object", "properties": {}},
            }
            backend_tools.append(backend_tool)
        return backend_tools

    @staticmethod
    def _tool_identity(tool: dict[str, Any]) -> str:
        tool_type = str(tool.get("type") or "").strip()
        if tool_type == "function":
            name = str(tool.get("name") or "").strip()
            if not name:
                raise ValueError("function 工具缺少 name。")
            return f"function:{name}"
        if tool_type == "mcp":
            label = str(tool.get("server_label") or "").strip()
            if not label:
                raise ValueError("mcp 工具缺少 server_label。")
            return f"mcp:{label}"
        if not tool_type:
            raise ValueError("工具定义缺少 type。")
        return tool_type

    def _merge_backend_tools(
        self,
        *tool_groups: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        by_identity: dict[str, dict[str, Any]] = {}
        for group in tool_groups:
            for tool in group:
                if not isinstance(tool, dict):
                    raise ValueError("工具定义必须是对象。")
                identity = self._tool_identity(tool)
                existing = by_identity.get(identity)
                if existing is None:
                    copied = dict(tool)
                    by_identity[identity] = copied
                    merged.append(copied)
                    continue
                if existing != tool:
                    raise ValueError(f"工具 {identity} 存在冲突定义。")
        return merged

    def _configured_web_search_tool(
        self, request_mode: str = "inherit"
    ) -> list[dict[str, Any]]:
        mode = str(request_mode or "inherit").strip().lower()
        if mode == "inherit":
            mode = (
                str(self.provider_config.get("oauth_web_search") or "disabled")
                .strip()
                .lower()
            )
        if mode == "disabled":
            return []
        if mode not in {"cached", "live"}:
            raise ValueError("oauth_web_search 必须是 disabled、cached 或 live。")
        tool: dict[str, Any] = {
            "type": "web_search",
            "external_web_access": mode == "live",
        }
        domains = self.provider_config.get("oauth_web_search_domains") or []
        if not isinstance(domains, list):
            raise ValueError("oauth_web_search_domains 必须是字符串列表。")
        normalized_domains = []
        for domain in domains:
            if not isinstance(domain, str) or not domain.strip():
                raise ValueError("oauth_web_search_domains 必须是非空字符串列表。")
            normalized = domain.strip().lower()
            if "://" in normalized or "/" in normalized:
                raise ValueError(
                    "oauth_web_search_domains 仅接受域名，不接受 URL 或路径。"
                )
            if normalized not in normalized_domains:
                normalized_domains.append(normalized)
        if normalized_domains:
            tool["filters"] = {"allowed_domains": normalized_domains}
        return [tool]

    @staticmethod
    def _input_mentions_json(value) -> bool:
        if isinstance(value, str):
            return "json" in value.casefold()
        if isinstance(value, list):
            return any(
                isinstance(part, dict) and part.get("type") == "input_text"
                and isinstance(part.get("text"), str) and "json" in part["text"].casefold()
                for part in value
            )
        return False

    @staticmethod
    def _is_managed_web_search_tool(tool: dict[str, Any]) -> bool:
        return str(tool.get("type") or "").strip().lower() in {
            "web_search",
            "web_search_preview",
        }

    def _build_responses_params(
        self,
        payloads: dict,
        tools,
        *,
        oauth_web_search: str | None = None,
    ) -> dict[str, Any]:
        payloads = dict(payloads)
        payload_search_mode = payloads.pop("_oauth_web_search", None)
        payloads.pop("_oauth_retry_rate_limits", None)
        if oauth_web_search in {None, "inherit"}:
            oauth_web_search = str(
                payload_search_mode or provider_oauth_web_search.get()
            )
        instructions, backend_input = self._convert_messages_to_backend_input(
            payloads.get("messages", []) or []
        )
        params: dict[str, Any] = {
            "model": payloads.get("model", self.get_model()),
            "input": backend_input,
            "instructions": instructions,
            "stream": True,
            "store": False,
        }
        function_tools: list[dict[str, Any]] = []
        if tools:
            tool_list = tools.get_func_desc_openai_style(
                omit_empty_parameter_field=False,
            )
            if tool_list:
                function_tools = self._convert_tools_to_backend_format(tool_list)

        custom_extra_body = self.provider_config.get("custom_extra_body", {})
        custom_tools: list[dict[str, Any]] = []
        if isinstance(custom_extra_body, dict):
            for key, value in custom_extra_body.items():
                if key in {"model", "input", "instructions"}:
                    continue
                if key == "tools":
                    if not isinstance(value, list):
                        raise ValueError("custom_extra_body.tools 必须是列表。")
                    custom_tools = list(value)
                    continue
                params[key] = value

        response_format = payloads.get("response_format")
        if response_format is not None:
            if not isinstance(response_format, dict):
                raise ValueError("response_format must be an object")
            format_type = response_format.get("type")
            if format_type not in ("text", "json_object", "json_schema"):
                raise ValueError("Unsupported response_format type")
            merged_format: dict[str, Any] = {"type": format_type}
            if format_type == "json_schema":
                schema_body = response_format.get("json_schema")
                if not isinstance(schema_body, dict) or not isinstance(
                    schema_body.get("schema"), dict
                ) or not isinstance(schema_body.get("name"), str) or not schema_body["name"]:
                    raise ValueError("json_schema requires a name and schema object")
                for key in ("name", "schema", "description", "strict"):
                    if key in schema_body:
                        merged_format[key] = schema_body[key]
                if "strict" in merged_format and not isinstance(merged_format["strict"], bool):
                    raise ValueError("json_schema.strict must be a boolean")
            text_config = params.get("text") or {}
            if not isinstance(text_config, dict):
                raise ValueError("text must be an object")
            params["text"] = {**text_config, "format": merged_format}

        request_search_mode = str(oauth_web_search or "inherit").strip().lower()
        if request_search_mode not in {"inherit", "disabled", "cached", "live"}:
            raise ValueError(
                "oauth_web_search 必须是 inherit、disabled、cached 或 live。"
            )
        if request_search_mode != "inherit":
            custom_tools = [
                tool
                for tool in custom_tools
                if not self._is_managed_web_search_tool(tool)
            ]

        merged_tools = self._merge_backend_tools(
            function_tools,
            custom_tools,
            self._configured_web_search_tool(request_search_mode),
        )
        if merged_tools:
            params["tools"] = merged_tools
        if payloads.get("tool_choice") is not None:
            params["tool_choice"] = payloads["tool_choice"]
        tool_choice = params.get("tool_choice")
        tool_choice_type = (
            str(tool_choice.get("type") or "").strip().lower()
            if isinstance(tool_choice, dict)
            else str(tool_choice or "").strip().lower()
        )
        if request_search_mode == "disabled" and tool_choice_type in {
            "web_search",
            "web_search_preview",
        }:
            raise ValueError("oauth_web_search=disabled 时不能选择托管搜索工具。")

        reasoning_value = params.get("reasoning")
        if reasoning_value is not None and not isinstance(reasoning_value, dict):
            raise ValueError("reasoning 必须是对象。")
        reasoning = dict(reasoning_value or {})

        configured_effort = params.pop("reasoning_effort", None)
        if configured_effort is not None and "effort" not in reasoning:
            reasoning["effort"] = configured_effort

        request_effort = payloads.get("reasoning_effort")
        if request_effort is not None:
            reasoning["effort"] = request_effort
        request_reasoning = payloads.get("reasoning")
        if request_reasoning is not None:
            if not isinstance(request_reasoning, dict):
                raise ValueError("reasoning 必须是对象。")
            reasoning.update(request_reasoning)

        if "effort" in reasoning:
            effort = str(reasoning["effort"] or "").strip().lower()
            if effort == "off":
                effort = "none"
            if effort == "ultra":
                raise ValueError(
                    "reasoning_effort=ultra 需要多代理调度，不能作为单次 Provider 请求发送。"
                )
            model = str(params["model"] or "").strip().lower()
            capability = self.model_capabilities.get(model)
            if capability:
                supported = capability["supported_reasoning_efforts"]
                if effort == "max" and effort not in supported and "xhigh" in supported:
                    effort = "xhigh"
                elif effort not in supported:
                    supported_text = ", ".join(supported)
                    raise ValueError(
                        f"模型 {model} 不支持 reasoning_effort={effort}；"
                        f"可用值：{supported_text}。"
                    )
            reasoning["effort"] = effort

        if reasoning:
            params["reasoning"] = reasoning
        else:
            params.pop("reasoning", None)
        params.pop("max_output_tokens", None)
        params.pop("temperature", None)
        model_name = str(params.get("model") or "").strip().lower()
        if model_name.startswith(
            ("gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna")
        ):
            params.pop("top_p", None)
            params.pop("top_logprobs", None)
        strips_reasoning_sampling = model_name.startswith(
            ("gpt-6.1-sol", "gpt-6-astra")
        ) or (
            model_name.startswith(("gpt-6-sol", "gpt-6-luna", "gpt-6.1-terra"))
            and reasoning.get("effort") != "none"
        )
        if strips_reasoning_sampling:
            include = params.get("include")
            if isinstance(include, list):
                filtered_include = [
                    item for item in include if item != "message.output_text.logprobs"
                ]
                if filtered_include:
                    params["include"] = filtered_include
                else:
                    params.pop("include", None)
            elif include == "message.output_text.logprobs":
                params.pop("include", None)
        text_options = params.get("text")
        if (
            isinstance(text_options, dict)
            and isinstance(text_options.get("format"), dict)
            and text_options["format"].get("type") == "json_object"
        ):
            messages = [item for item in params["input"] if item.get("type") == "message"]
            if not any(self._input_mentions_json(item.get("content")) for item in messages):
                note = {"type": "input_text", "text": "Output format: json."}
                params["input"] = list(params["input"])
                for index in range(len(params["input"]) - 1, -1, -1):
                    item = params["input"][index]
                    if item.get("type") != "message" or item.get("role") != "user":
                        continue
                    content = item.get("content")
                    parts = list(content) if isinstance(content, list) else (
                        [{"type": "input_text", "text": content}] if isinstance(content, str) and content else []
                    )
                    params["input"][index] = {**item, "content": [*parts, note]}
                    break
                else:
                    params["input"].append({"type": "message", "role": "user", "content": [note]})
        return params

    @staticmethod
    def _extract_url_citations(response: Any) -> list[tuple[str, str]]:
        if not isinstance(response, dict):
            return []
        citations: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for item in response.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                for annotation in content.get("annotations", []) or []:
                    if not isinstance(annotation, dict):
                        continue
                    if annotation.get("type") not in {"url_citation", "source"}:
                        continue
                    url = str(annotation.get("url") or "").strip()
                    if not url.startswith(("https://", "http://")) or url in seen_urls:
                        continue
                    if any(ord(char) < 32 for char in url):
                        continue
                    try:
                        parsed_url = urlsplit(url)
                        if not parsed_url.hostname or parsed_url.username:
                            continue
                    except ValueError:
                        continue
                    title = str(annotation.get("title") or url).strip() or url
                    title = " ".join(title.split())[:512]
                    title = (
                        title.replace("\\", "\\\\")
                        .replace("[", "\\[")
                        .replace("]", "\\]")
                    )
                    seen_urls.add(url)
                    citations.append((title, quote(url, safe=":/?#@!$&'*+,;=%~.-_")))
        return citations

    async def _parse_responses_completion(self, response: Any, tools) -> LLMResponse:
        status = response.get("status") if isinstance(response, dict) else getattr(response, "status", None)
        error = response.get("error") if isinstance(response, dict) else getattr(response, "error", None)
        if error is not None or status not in (None, "completed"):
            self._log_backend_failure(response)
            raise RuntimeError("Codex response failed or did not complete")
        llm_response = LLMResponse("assistant")
        output_text = ""
        if isinstance(response, dict):
            output_text = str(response.get("output_text") or "").strip()
        else:
            output_text = (getattr(response, "output_text", None) or "").strip()
        output_items = list(
            response.get("output", []) or []
            if isinstance(response, dict)
            else getattr(response, "output", []) or []
        )
        reasoning_parts: list[str] = []
        tool_args: list[dict[str, Any]] = []
        rejected_calls: list[dict[str, str]] = []
        tool_names: list[str] = []
        tool_ids: list[str] = []
        message_text_parts: list[str] = []

        for item in output_items:
            item_type = (
                item.get("type")
                if isinstance(item, dict)
                else getattr(item, "type", None)
            )
            if item_type == "reasoning":
                summaries = (
                    item.get("summary", []) or []
                    if isinstance(item, dict)
                    else getattr(item, "summary", []) or []
                )
                for summary in summaries:
                    text = (
                        summary.get("text")
                        if isinstance(summary, dict)
                        else getattr(summary, "text", None)
                    )
                    if text:
                        reasoning_parts.append(str(text))
            elif item_type == "function_call":
                arguments = (
                    item.get("arguments")
                    if isinstance(item, dict)
                    else getattr(item, "arguments", None)
                )
                parsed_args, rejection_code = parse_tool_arguments(arguments)
                if rejection_code is not None:
                    name = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
                    call_id = item.get("call_id") if isinstance(item, dict) else getattr(item, "call_id", None)
                    rejected_calls.append(rejected_tool_call(name, call_id, rejection_code))
                    continue
                tool_args.append(parsed_args)
                tool_names.append(
                    str(
                        item.get("name", "")
                        if isinstance(item, dict)
                        else getattr(item, "name", "") or ""
                    )
                )
                tool_ids.append(
                    str(
                        item.get("call_id", "")
                        if isinstance(item, dict)
                        else getattr(item, "call_id", "") or ""
                    )
                )
            elif item_type == "message" and not output_text:
                content_items = (
                    item.get("content", []) or []
                    if isinstance(item, dict)
                    else getattr(item, "content", []) or []
                )
                item_text_parts: list[str] = []
                for content in content_items:
                    ctype = (
                        content.get("type")
                        if isinstance(content, dict)
                        else getattr(content, "type", None)
                    )
                    if ctype in {"output_text", "text", "refusal"}:
                        text = (
                            content.get("text")
                            if isinstance(content, dict)
                            else getattr(content, "text", None)
                        )
                        if ctype == "refusal":
                            text = (
                                content.get("refusal", text)
                                if isinstance(content, dict)
                                else getattr(content, "refusal", text)
                            )
                        if text:
                            item_text_parts.append(str(text))
                if item_text_parts:
                    message_text_parts.append("".join(item_text_parts).strip())

        output_text = output_text or "\n".join(message_text_parts)
        output_text = apply_tool_rejections(
            llm_response, rejected_calls, output_text, has_valid_calls=bool(tool_args)
        )
        if rejected_calls:
            logger.warning("Codex rejected %d invalid tool calls; arguments were not exposed.", len(rejected_calls))
        citations = self._extract_url_citations(response)
        if output_text:
            if citations:
                citation_text = "\n".join(
                    f"- [{title}]({url})" for title, url in citations
                )
                output_text += f"\n\n来源：\n{citation_text}"
            llm_response.result_chain = MessageChain().message(output_text)

        if reasoning_parts:
            llm_response.reasoning_content = "\n".join(
                part for part in reasoning_parts if part
            )

        if tool_args:
            llm_response.role = "tool"
            llm_response.tools_call_args = tool_args
            llm_response.tools_call_name = tool_names
            llm_response.tools_call_ids = tool_ids

        response_id = (
            response.get("id")
            if isinstance(response, dict)
            else getattr(response, "id", None)
        )
        if not output_text and not tool_args and status != "completed":
            raise RuntimeError("Codex response has no usable output or completion status")
        if llm_response.completion_text is None and not llm_response.tools_call_args:
            llm_response.result_chain = MessageChain().message("")

        llm_response.raw_completion = response
        if response_id:
            llm_response.id = response_id
        usage = self._extract_response_usage(
            response.get("usage")
            if isinstance(response, dict)
            else getattr(response, "usage", None)
        )
        if usage is not None:
            llm_response.usage = usage
        return llm_response

    async def text_chat(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        tool_choice="auto",
        request_max_retries=None,
        retry_rate_limits=None,
        oauth_web_search=None,
        **kwargs,
    ) -> LLMResponse:
        """Run an OAuth chat request and account for direct provider usage.

        Args:
            prompt: User prompt for a new conversation turn.
            session_id: Session identifier used by provider statistics.
            image_urls: Image inputs attached to the user message.
            audio_urls: Audio inputs attached to the user message.
            func_tool: Tools available to the model.
            contexts: Existing conversation messages.
            system_prompt: System instruction for the request.
            tool_calls_result: Results returned from earlier tool calls.
            model: Explicit model override.
            extra_user_content_parts: Additional user message content parts.
            tool_choice: Tool selection policy.
            request_max_retries: Maximum attempts for retryable backend requests.
            retry_rate_limits: Whether HTTP 429 responses may be retried.
            oauth_web_search: Request-level managed web search mode.
            **kwargs: Additional provider request options.

        Returns:
            Parsed model response from the inherited OpenAI provider.

        Raises:
            Exception: Re-raises the original provider exception unchanged.
        """
        managed_by_agent = provider_stats_managed_by_agent.get()
        ledger = current_attempt_ledger.get()
        if (
            not getattr(self, "_usage_recording_enabled", False)
            or ledger is None or ledger.provider_key != id(self)
        ):
            ledger = UsageAttemptLedger(id(self))
        ledger_token = current_attempt_ledger.set(ledger)
        request_kind = oauth_provider_stat_kind.get()
        start_time = time.time()
        kwargs["_oauth_tool_choice"] = tool_choice
        try:
            response = await super().text_chat(
                prompt=prompt,
                session_id=session_id,
                image_urls=image_urls,
                audio_urls=audio_urls,
                func_tool=func_tool,
                contexts=contexts,
                system_prompt=system_prompt,
                tool_calls_result=tool_calls_result,
                model=model,
                extra_user_content_parts=extra_user_content_parts,
                tool_choice=tool_choice,
                request_max_retries=request_max_retries,
                retry_rate_limits=retry_rate_limits,
                oauth_web_search=oauth_web_search,
                **kwargs,
            )
        except asyncio.CancelledError as exc:
            if not managed_by_agent:
                await self._record_provider_stat(
                    request_kind=request_kind,
                    status="error" if request_kind == "test" else "aborted",
                    usage=ledger.combined(
                        getattr(exc, "_astrbot_token_usage", None), exc
                    ),
                    start_time=start_time,
                    end_time=time.time(),
                    model=model,
                    session_id=session_id,
                )
            raise
        except Exception as exc:
            if not managed_by_agent:
                await self._record_provider_stat(
                    request_kind=request_kind,
                    status="error",
                    usage=ledger.combined(
                        getattr(exc, "_astrbot_token_usage", None), exc
                    ),
                    start_time=start_time,
                    end_time=time.time(),
                    model=model,
                    session_id=session_id,
                )
            raise
        finally:
            current_attempt_ledger.reset(ledger_token)

        if not managed_by_agent:
            await self._record_provider_stat(
                request_kind=request_kind,
                status="error" if response.role == "err" else "completed",
                usage=ledger.combined(response.usage),
                start_time=start_time,
                end_time=time.time(),
                model=model,
                session_id=session_id,
            )
        return response


    def _http_response_error(self, status_code: int, text: str) -> ProviderRequestError:
        """Preserve actual error-body counters without exposing its raw body.

        Args:
            status_code: HTTP status returned by the backend.
            text: Untrusted response body, used only for safe classification.

        Returns:
            Sanitized exception carrying optional reported usage and model.
        """
        error = ProviderRequestError(
            self._format_backend_error(status_code, text), status_code=status_code
        )
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return error
        if isinstance(data, dict):
            payload = data.get("response")
            payload = payload if isinstance(payload, dict) else data
            self._observe_backend_usage(
                payload, complete=True, response_id=data.get("response_id")
            )
            usage = self._extract_response_usage(payload.get("usage"))
            if usage is not None:
                error._astrbot_token_usage = usage
            if isinstance(payload.get("model"), str):
                error._astrbot_provider_model = payload["model"]
        return error


    def _observe_backend_usage(
        self, payload: Any, *, complete: bool, response_id: str | None = None
    ) -> None:
        """Forward only raw reported directions to the request-local usage sink.

        Args:
            payload: Backend response or event body with a raw usage object.
            complete: Whether a terminal response supplied the counters.
            response_id: Explicit enclosing event response identity, if present.
        """
        if not isinstance(payload, dict):
            return
        raw = payload.get("usage")
        if not isinstance(raw, dict):
            return
        observe_attempt_usage(
            self._extract_response_usage(raw),
            complete=complete,
            response_id=response_id or payload.get("response_id") or payload.get("id"),
            reported_fields=frozenset(
                direction
                for direction in ("input", "output")
                if raw.get(f"{direction}_tokens") is not None
            ),
        )


    async def _handle_api_error(self, error, *args, **kwargs):
        """Observe inherited compatibility retries without changing retry policy.

        Args:
            error: Exception from the attempted OAuth request.
            *args: Inherited retry handler arguments.
            **kwargs: Inherited retry handler options.

        Returns:
            The inherited retry decision and adjusted payload.
        """
        note_failed_attempt(error)
        return await super()._handle_api_error(error, *args, **kwargs)


    async def test(self, timeout: float = 45.0) -> None:
        token = oauth_provider_stat_kind.set("test")
        try:
            await super().test(timeout)
        finally:
            oauth_provider_stat_kind.reset(token)

    async def _query(
        self,
        payloads: dict,
        tools,
        *,
        conversation_id: str | None = None,
        request_max_retries: int | None = None,
        retry_rate_limits: bool | None = None,
        oauth_web_search: str | None = None,
        **_extra: Any,
    ) -> LLMResponse:
        if retry_rate_limits is None:
            retry_rate_limits = payloads.get("_oauth_retry_rate_limits")
        params = self._build_responses_params(
            payloads,
            tools,
            oauth_web_search=oauth_web_search,
        )
        budget = await resolve_context_budget(self, model=params["model"])
        params = trim_context_to_budget(params, budget)
        response = await retry_provider_request(
            "OpenAI OAuth",
            lambda: self._request_backend(params),
            retry_rate_limits=retry_rate_limits,
            max_attempts=request_max_retries,
        )
        self._observe_backend_usage(response, complete=True)
        try:
            return await self._parse_responses_completion(response, tools)
        except BaseException as exc:
            usage = self._extract_response_usage(
                response.get("usage")
                if isinstance(response, dict)
                else getattr(response, "usage", None)
            )
            if usage is not None:
                setattr(exc, "_astrbot_token_usage", usage)
            note_failed_attempt(exc, new_attempt=True)
            raise


    async def _query_stream(
        self,
        payloads: dict,
        tools,
        *,
        conversation_id: str | None = None,
        request_max_retries: int | None = None,
        retry_rate_limits: bool | None = None,
        oauth_web_search: str | None = None,
        **_extra: Any,
    ) -> AsyncGenerator[LLMResponse, None]:
        del request_max_retries, retry_rate_limits
        params = self._build_responses_params(
            payloads,
            tools,
            oauth_web_search=oauth_web_search,
        )
        budget = await resolve_context_budget(self, model=params["model"])
        params = trim_context_to_budget(params, budget)
        output_text_parts: list[str] = []
        reasoning_parts: list[str] = []
        output_items: dict[str, dict[str, Any]] = {}
        completed_response: dict[str, Any] | None = None

        async with aclosing(self._stream_backend_events(params)) as event_stream:
            async for event in event_stream:
                chunk, completed = self._apply_stream_event(
                    event,
                    output_text_parts,
                    reasoning_parts,
                    output_items,
                )
                if completed is not None:
                    completed_response = completed
                if chunk is not None:
                    yield chunk

        if completed_response is None:
            raise Exception(
                "Codex backend stream ended without response.completed event"
            )
        if not completed_response.get("output") and output_items:
            completed_response["output"] = list(output_items.values())
        if output_text_parts and not completed_response.get("output_text"):
            completed_response["output_text"] = "".join(output_text_parts)
        if reasoning_parts:
            existing_output = list(completed_response.get("output") or [])
            if not any(
                isinstance(item, dict) and item.get("type") == "reasoning"
                for item in existing_output
            ):
                existing_output.append(
                    {
                        "type": "reasoning",
                        "summary": [
                            {"type": "summary_text", "text": "".join(reasoning_parts)}
                        ],
                    }
                )
                completed_response["output"] = existing_output
        self._observe_backend_usage(completed_response, complete=True)
        try:
            final_response = await self._parse_responses_completion(
                completed_response, tools
            )
        except BaseException as exc:
            usage = self._extract_response_usage(completed_response.get("usage"))
            if usage is not None:
                setattr(exc, "_astrbot_token_usage", usage)
            note_failed_attempt(exc, new_attempt=True)
            raise
        if not output_text_parts and final_response.completion_text:
            yield LLMResponse(
                role="assistant",
                completion_text=final_response.completion_text,
                is_chunk=True,
            )
        elif output_text_parts:
            citations = self._extract_url_citations(completed_response)
            if citations:
                citation_text = "\n".join(
                    f"- [{title}]({url})" for title, url in citations
                )
                yield LLMResponse(
                    role="assistant",
                    completion_text=f"\n\n来源：\n{citation_text}",
                    is_chunk=True,
                )
        yield final_response


    def _apply_stream_event(
        self,
        event: dict[str, Any],
        output_text_parts: list[str],
        reasoning_parts: list[str],
        output_items: dict[str, dict[str, Any]],
    ) -> tuple[LLMResponse | None, dict[str, Any] | None]:
        event_type = event.get("type")
        if event_type in {
            "error",
            "response.error",
            "response.failed",
            "response.incomplete",
        }:
            status_code = self._extract_stream_error_status_code(event)
            self._log_backend_failure(event, status_code)
            if status_code is None:
                error = RuntimeError(f"Codex backend stream ended with {event_type}")
            else:
                error = ProviderRequestError(
                    f"Codex backend stream ended with {event_type}",
                    status_code=status_code,
                )
            response = event.get("response")
            if isinstance(response, dict):
                usage = self._extract_response_usage(response.get("usage"))
                if usage is not None:
                    setattr(error, "_astrbot_token_usage", usage)
            raise error
        if event_type == "response.output_text.delta":
            delta = str(event.get("delta") or "")
            if delta:
                output_text_parts.append(delta)
                return (
                    LLMResponse(
                        role="assistant",
                        completion_text=delta,
                        is_chunk=True,
                    ),
                    None,
                )
        elif event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }:
            delta = str(event.get("delta") or "")
            if delta:
                reasoning_parts.append(delta)
                return (
                    LLMResponse(
                        role="assistant",
                        reasoning_content=delta,
                        is_chunk=True,
                    ),
                    None,
                )
        elif event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict):
                item_id = str(item.get("id") or event.get("output_index") or "")
                if item_id:
                    output_items[item_id] = dict(item)
        elif event_type == "response.function_call_arguments.delta":
            item_id = str(event.get("item_id") or event.get("output_index") or "")
            delta = str(event.get("delta") or "")
            item = output_items.setdefault(
                item_id,
                {
                    "type": "function_call",
                    "call_id": str(event.get("call_id") or ""),
                    "name": str(event.get("name") or ""),
                    "arguments": "",
                },
            )
            item["arguments"] = str(item.get("arguments") or "") + delta
            if delta:
                return (
                    LLMResponse(
                        role="tool",
                        tools_call_args=[{"_arguments_delta": delta}],
                        tools_call_name=[str(item.get("name") or "")],
                        tools_call_ids=[str(item.get("call_id") or item_id)],
                        is_chunk=True,
                    ),
                    None,
                )
        elif event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                item_id = str(item.get("id") or event.get("output_index") or "")
                output_items[item_id] = dict(item)
        elif event_type == "response.completed":
            response = event.get("response")
            if not isinstance(response, dict):
                raise ValueError("Codex completed event has no response object")
            completed = dict(response)
            completed.setdefault("status", "completed")
            return None, completed
        return None, None

    @staticmethod
    def _extract_stream_error_status_code(event: dict[str, Any]) -> int | None:
        candidates = [event]
        for key in ("error", "response"):
            value = event.get(key)
            if isinstance(value, dict):
                candidates.append(value)
                nested_error = value.get("error")
                if isinstance(nested_error, dict):
                    candidates.append(nested_error)
        for candidate in candidates:
            for key in ("status_code", "status"):
                value = candidate.get(key)
                if isinstance(value, int):
                    return value
            code = str(candidate.get("code") or "").strip().lower()
            if code in {
                "rate_limit_exceeded",
                "rate_limit_error",
                "insufficient_quota",
                "credit_balance_exhausted",
                "429",
            }:
                return 429
        return None

    async def generate_image(
        self,
        prompt: str,
        model: str | None = None,
        size: str | None = None,
        n: int = 1,
        reference_images: list[str] | None = None,
        action: str | None = None,
        timeout: float | None = None,
        transport: str = "http",
        *,
        image_model: str | None = None,
        output_format: str | None = None,
        gif_input_mode: str = "reject",
    ) -> list[OAuthPlugImageResult]:
        """Generate images and persist aggregate OAuth token usage.

        Args:
            prompt: Image generation or editing instruction.
            model: Main Responses controller model, not the image model.
            size: Requested image dimensions.
            n: Number of backend image generations.
            reference_images: Local files, URLs, or data URLs used as references.
            action: Image tool action override.
            timeout: Optional per-image deadline, including the whole HTTP request.
            transport: Image transport, either HTTP or WebSocket.
            image_model: Requested image tool model. None inherits the source
                setting (Images 2.6 Sunburst by default); an empty string omits it.
            output_format: Optional png, jpeg or webp request. The current Codex
                OAuth backend rejects gif output.
            gif_input_mode: Reject animation by default; first_frame explicitly
                converts an animated GIF reference to a single PNG frame.

        Returns:
            Extracted image results from all backend generations.

        Raises:
            Exception: Re-raises validation, backend, or extraction failures.
        """
        if transport not in {"http", "websocket"}:
            raise ValueError("图片传输方式必须为 http 或 websocket。")
        explicit_timeout = timeout is not None
        if timeout is None:
            request_timeout = self.timeout
        else:
            try:
                if isinstance(timeout, bool):
                    raise ValueError("timeout 必须是有限正浮点数。")
                request_timeout = float(timeout)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("timeout 必须是有限正浮点数。") from exc
            if not math.isfinite(request_timeout) or request_timeout <= 0:
                raise ValueError("timeout 必须是有限正浮点数。")
        selected_image_model = (
            self.provider_config.get("oauth_image_model", "gpt-image-2.6-sunburst")
            if image_model is None
            else image_model
        )
        if selected_image_model is None:
            selected_image_model = ""
        if not isinstance(selected_image_model, str):
            raise ValueError("图片模型选择无效。")
        if selected_image_model.strip():
            if selected_image_model not in OAUTH_IMAGE_MODEL_ALIASES:
                raise ValueError("图片模型选择无效。")
        else:
            selected_image_model = ""

        if output_format not in (None, "png", "jpeg", "webp", "gif"):
            raise ValueError("output_format 必须为 png、jpeg、webp 或 gif。")
        if gif_input_mode not in ("reject", "first_frame"):
            raise ValueError("gif_input_mode 必须为 reject 或 first_frame。")
        if output_format == "gif":
            raise ValueError("当前 Codex OAuth 后端不支持 GIF 输出；不会自动改为 PNG。")

        start_time = time.time()
        total_usage = None
        missing_image_usage = False
        try:
            references = [
                str(image).strip()
                for image in reference_images or []
                if str(image).strip()
            ]
            instructions = str(prompt or "").strip()
            if not instructions:
                raise ValueError("图片生成提示词不能为空。")
            references = [
                await asyncio.to_thread(reference_image_url, image, gif_input_mode=gif_input_mode)
                for image in references
            ]
            image_input = self._build_image_generation_input(instructions, references)
            image_action = (action or ("edit" if references else "generate")).strip()
            if not image_action:
                image_action = "edit" if references else "generate"
            results: list[OAuthPlugImageResult] = []
            count = max(1, int(n or 1))
            for _ in range(count):
                tool: dict[str, Any] = {
                    "type": "image_generation",
                    "action": image_action,
                }
                if size:
                    tool["size"] = size
                if selected_image_model:
                    tool["model"] = selected_image_model
                if output_format is not None:
                    tool["output_format"] = output_format
                payload = {
                    "model": model or self.get_model(),
                    "input": image_input,
                    "instructions": instructions,
                    "tools": [tool],
                    "tool_choice": {"type": "image_generation"},
                    "stream": True,
                    "store": False,
                }
                image_start = time.time()
                response_usage = None
                image_ledger = UsageAttemptLedger(id(self))
                ledger_token = current_attempt_ledger.set(image_ledger)
                image_error = None
                image_status = "completed"
                response: dict[str, Any] = {}
                try:
                    if transport == "websocket":
                        response = await self._request_image_backend_websocket(
                            payload, timeout=request_timeout
                        )
                    elif explicit_timeout:
                        response = await asyncio.wait_for(
                            self._request_image_backend(payload, request_timeout),
                            timeout=request_timeout,
                        )
                    else:
                        response = await self._request_image_backend(
                            payload, request_timeout
                        )
                    response_usage = self._extract_response_usage(
                        response.get("usage")
                    )
                    self._observe_backend_usage(response, complete=True)
                    if output_format is None:
                        results.extend(await self._extract_generated_images(response))
                    else:
                        results.extend(await self._extract_generated_images(response, expected_format=output_format))
                except asyncio.CancelledError as exc:
                    image_error = exc
                    if response_usage is None:
                        response_usage = getattr(exc, "_astrbot_token_usage", None)
                    image_status = "aborted"
                    raise
                except Exception as exc:
                    image_error = exc
                    image_status = "error"
                    if response_usage is None:
                        response_usage = getattr(exc, "_astrbot_token_usage", None)
                    raise
                finally:
                    current_attempt_ledger.reset(ledger_token)
                    response_usage = image_ledger.combined(response_usage, image_error)
                    if response_usage is None:
                        missing_image_usage = True
                    else:
                        partial_total = getattr(total_usage, "is_partial", False) or getattr(response_usage, "is_partial", False)
                        total_usage = (total_usage or TokenUsage()) + response_usage
                        if partial_total:
                            total_usage = self._partial_usage(total_usage)
                    if missing_image_usage and total_usage is not None:
                        total_usage = self._partial_usage(total_usage)
                    if getattr(self, "_usage_recording_enabled", False):
                        await self._record_provider_stat(
                            request_kind="image",
                            status=image_status,
                            usage=response_usage,
                            start_time=image_start,
                            end_time=time.time(),
                            model=response.get("model") or getattr(image_error, "_astrbot_provider_model", None) or model,
                        )
                        if image_error is not None:
                            image_error._astrbot_usage_recorded = True
        except (Exception, asyncio.CancelledError) as exc:
            if not getattr(self, "_usage_recording_enabled", False):
                await self._record_provider_stat(
                    request_kind="image",
                    status="aborted" if isinstance(exc, asyncio.CancelledError) else "error",
                    usage=total_usage,
                    start_time=start_time,
                    end_time=time.time(),
                    model=model,
                )
                exc._astrbot_usage_recorded = True
            raise

        if not getattr(self, "_usage_recording_enabled", False):
            await self._record_provider_stat(
                request_kind="image",
                status="completed",
                usage=total_usage,
                start_time=start_time,
                end_time=time.time(),
                model=model,
            )
        return results

    def _build_image_generation_input(
        self,
        prompt: str,
        reference_images: list[str],
    ) -> list[dict[str, Any]]:
        image_parts = [
            self._reference_image_to_input_part(image)
            for image in reference_images
            if str(image or "").strip()
        ]
        return [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt,
                    },
                    *image_parts,
                ],
            }
        ]

    def _reference_image_to_input_part(self, image: str) -> dict[str, str]:
        return {
            "type": "input_image",
            "image_url": self._reference_image_to_image_url(image),
        }

    def _reference_image_to_image_url(self, image: str) -> str:
        return reference_image_url(image)

    async def _extract_generated_images(
        self,
        response: dict[str, Any],
        *,
        expected_format: str | None = None,
    ) -> list[OAuthPlugImageResult]:
        output = response.get("output") or []
        if not isinstance(output, list):
            output = []

        image_dir_value = self.provider_config.get("generated_image_dir")
        image_dir = (
            Path(str(image_dir_value))
            if image_dir_value
            else Path(get_astrbot_data_path()) / "generated" / "openai_oauth_images"
        )
        image_dir.mkdir(parents=True, exist_ok=True)

        results: list[OAuthPlugImageResult] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            image_base64 = self._extract_image_base64_from_output_item(item)
            if not image_base64:
                continue
            data, extension, mime_type, frame_count = await asyncio.to_thread(decode_result, image_base64)
            if expected_format is not None and extension != expected_format:
                raise ValueError("后端返回的图片格式与请求不一致，未保存为错误格式。")
            file_path = image_dir / f"{uuid.uuid4().hex}.{extension}"
            file_path.write_bytes(data)
            results.append(
                OAuthPlugImageResult(
                    path=str(file_path),
                    mime_type=mime_type,
                    frame_count=frame_count,
                    is_animated=frame_count > 1,
                    revised_prompt=str(item.get("revised_prompt") or ""),
                    raw=item,
                )
            )

        if not results:
            raise ValueError("Codex 图像生成响应未包含可提取图片。")
        return results

    def _extract_image_base64_from_output_item(self, item: dict[str, Any]) -> str:
        if item.get("type") == "image_generation_call":
            value = item.get("result")
            if value:
                return str(value)

        content = item.get("content")
        if not isinstance(content, list):
            return ""
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") not in {"output_image", "image"}:
                continue
            value = (
                part.get("image_base64")
                or part.get("b64_json")
                or part.get("data")
                or ""
            )
            if value:
                return str(value)
        return ""

    async def text_chat_stream(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        tool_choice="auto",
        request_max_retries=None,
        retry_rate_limits=None,
        oauth_web_search=None,
        **kwargs,
    ) -> AsyncGenerator[LLMResponse, None]:
        extra_user_content_parts = kwargs.pop("extra_user_content_parts", None)
        conversation_id = kwargs.pop("conversation_id", None)
        payloads, _context_query = await self._prepare_chat_payload(
            prompt,
            image_urls,
            audio_urls,
            contexts,
            system_prompt,
            tool_calls_result,
            model=model,
            extra_user_content_parts=extra_user_content_parts,
            **kwargs,
        )
        if (func_tool and not func_tool.empty()) or tool_choice != "auto":
            payloads["tool_choice"] = tool_choice

        managed_by_agent = provider_stats_managed_by_agent.get()
        request_kind = oauth_provider_stat_kind.get()
        start_time = time.time()
        final_response: LLMResponse | None = None
        ledger = current_attempt_ledger.get()
        if (
            not getattr(self, "_usage_recording_enabled", False)
            or ledger is None or ledger.provider_key != id(self)
        ):
            ledger = UsageAttemptLedger(id(self))
        try:
            query_kwargs = {}
            if conversation_id:
                query_kwargs["conversation_id"] = conversation_id
            async with _usage_bound_closing(
                self._query_stream(
                    payloads,
                    func_tool,
                    **query_kwargs,
                    request_max_retries=request_max_retries,
                    retry_rate_limits=retry_rate_limits,
                    oauth_web_search=oauth_web_search,
                ),
                ledger,
            ) as response_stream:
                while True:
                    ledger_token = current_attempt_ledger.set(ledger)
                    try:
                        response = await anext(response_stream)
                    except StopAsyncIteration:
                        break
                    finally:
                        current_attempt_ledger.reset(ledger_token)
                    if not response.is_chunk:
                        final_response = response
                    yield response
        except (asyncio.CancelledError, GeneratorExit) as exc:
            if not managed_by_agent:
                await self._record_provider_stat(
                    request_kind=request_kind,
                    status="error" if request_kind == "test" else "aborted",
                    usage=ledger.combined(
                        getattr(exc, "_astrbot_token_usage", None), exc
                    ),
                    start_time=start_time,
                    end_time=time.time(),
                    model=model,
                    session_id=session_id,
                )
            raise
        except Exception as exc:
            if not managed_by_agent:
                await self._record_provider_stat(
                    request_kind=request_kind,
                    status="error",
                    usage=ledger.combined(
                        getattr(exc, "_astrbot_token_usage", None), exc
                    ),
                    start_time=start_time,
                    end_time=time.time(),
                    model=model,
                    session_id=session_id,
                )
            raise

        if final_response is None:
            raise Exception("Codex backend stream did not produce a final response")
        if not managed_by_agent:
            await self._record_provider_stat(
                request_kind=request_kind,
                status="error" if final_response.role == "err" else "completed",
                usage=ledger.combined(final_response.usage),
                start_time=start_time,
                end_time=time.time(),
                model=model,
                session_id=session_id,
            )
