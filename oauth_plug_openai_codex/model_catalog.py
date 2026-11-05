"""Bounded, account-scoped discovery of Codex OAuth model IDs."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from astrbot import logger

from .headers import CODEX_CLIENT_VERSION

MAX_CATALOG_BYTES = 4 * 1024 * 1024
CATALOG_TIMEOUT_SECONDS = 10
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


def _check_open(provider: Any) -> None:
    if getattr(provider, "_oauth_media_closed", False):
        raise RuntimeError("OAuth provider is closed")


def _cache_key(provider: Any) -> tuple[Any, ...]:
    provider._sync_oauth_credentials_from_shared()
    state = provider._oauth_shared_state
    return (
        id(state),
        state.version,
        provider.base_url.rstrip("/"),
        provider.provider_config.get("proxy") or "",
    )


def _merge(fallback: list[str], discovered: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys([*fallback, *discovered]))


async def _request_catalog(provider: Any) -> tuple[int, tuple[str, ...], int]:
    headers, version = provider._build_backend_headers_with_version()
    headers = {key: value for key, value in headers.items() if key.lower() != "accept"}
    headers["Accept"] = "application/json"
    async with provider._open_oauth_http_client(
        follow_redirects=False, timeout=CATALOG_TIMEOUT_SECONDS
    ) as client:
        async with client.stream(
            "GET",
            provider.base_url.rstrip("/") + "/models",
            params={"client_version": CODEX_CLIENT_VERSION},
            headers=headers,
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                return response.status_code, (), version
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_CATALOG_BYTES:
                    raise ValueError("Model catalog exceeds size limit")
                body.extend(chunk)

    # Never retain a catalog received under credentials replaced in flight.
    if provider._oauth_shared_state.version != version:
        return 200, (), version
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("Invalid model catalog")
    models: list[str] = []
    for item in payload["models"]:
        if not isinstance(item, dict):
            continue
        if item.get("visibility") not in (None, "list"):
            continue
        slug = item.get("slug")
        if isinstance(slug, str) and _MODEL_ID.fullmatch(slug):
            models.append(slug)
    return response.status_code, tuple(dict.fromkeys(models)), version


async def discover_models(provider: Any, fallback: list[str]) -> list[str]:
    """Append remotely discovered models while preserving configured order.

    Failures use the current fallback, with a short cache to bound repeated
    requests. Credentials, raw response bodies and backend errors are not logged.
    """
    _check_open(provider)
    _cache_key(provider)
    credentials = provider._oauth_shared_state.snapshot()
    if not (
        credentials.get("oauth_access_token") or credentials.get("oauth_refresh_token")
    ):
        return list(fallback)
    lock = getattr(provider, "_oauth_model_catalog_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        provider._oauth_model_catalog_lock = lock
    async with lock:
        _check_open(provider)
        key = _cache_key(provider)
        credentials = provider._oauth_shared_state.snapshot()
        if not (
            credentials.get("oauth_access_token") or credentials.get("oauth_refresh_token")
        ):
            return list(fallback)
        cached = getattr(provider, "_oauth_model_catalog_cache", None)
        if cached is not None and cached[0] == key and cached[1] > time.monotonic():
            return _merge(fallback, cached[2])

        discovered: tuple[str, ...] = ()
        ttl = 300
        request_key = key
        try:
            async with asyncio.timeout(CATALOG_TIMEOUT_SECONDS):
                await provider._ensure_fresh_oauth_token()
                provider._sync_oauth_credentials_from_shared()
                credentials = provider._oauth_shared_state.snapshot()
                if not credentials.get("oauth_access_token") and credentials.get(
                    "oauth_refresh_token"
                ):
                    await provider._refresh_after_auth_failure(
                        provider._oauth_shared_state.version
                    )
                request_key = _cache_key(provider)
                status, discovered, attempted_version = await _request_catalog(provider)
                if status in (401, 403):
                    if await provider._refresh_after_auth_failure(attempted_version):
                        request_key = _cache_key(provider)
                        status, discovered, _version = await _request_catalog(provider)
                if status < 200 or status >= 300:
                    raise ValueError("Model catalog request failed")
        except Exception:
            ttl = 30
            discovered = ()
            logger.warning("Codex model discovery unavailable; using configured models.")

        _check_open(provider)
        if _cache_key(provider) != request_key:
            return list(fallback)
        provider._oauth_model_catalog_cache = (request_key, time.monotonic() + ttl, discovered)
        return _merge(fallback, discovered)
