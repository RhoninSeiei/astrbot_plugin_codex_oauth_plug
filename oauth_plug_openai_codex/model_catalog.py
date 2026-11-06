"""Bounded, account-scoped discovery of Codex OAuth model IDs."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, NamedTuple

from astrbot import logger

from .headers import CODEX_CLIENT_VERSION

MAX_CATALOG_BYTES = 4 * 1024 * 1024
CATALOG_TIMEOUT_SECONDS = 10
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_CONTEXT_LIMIT_FIELDS = ("context_window", "max_context_window")


class _Catalog(NamedTuple):
    models: tuple[str, ...]
    context_metadata: dict[str, dict[str, int]]


def _context_limits(entry: dict[str, Any]) -> dict[str, int]:
    limits = {
        key: entry[key]
        for key in _CONTEXT_LIMIT_FIELDS
        if type(entry.get(key)) is int and 1024 <= entry[key] <= 10_000_000
    }
    if limits.get("max_context_window", 0) < limits.get("context_window", 0):
        limits.pop("max_context_window", None)
    return limits


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


async def _request_catalog(provider: Any) -> tuple[int, _Catalog, int]:
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
                return response.status_code, _Catalog((), {}), version
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_CATALOG_BYTES:
                    raise ValueError("Model catalog exceeds size limit")
                body.extend(chunk)

    # Never retain a catalog received under credentials replaced in flight.
    if provider._oauth_shared_state.version != version:
        return 200, _Catalog((), {}), version
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("Invalid model catalog")
    models: list[str] = []
    metadata: dict[str, dict[str, int]] = {}
    for item in payload["models"]:
        if not isinstance(item, dict):
            continue
        if item.get("visibility") not in (None, "list"):
            continue
        slug = item.get("slug")
        if isinstance(slug, str) and _MODEL_ID.fullmatch(slug):
            models.append(slug)
            limits = _context_limits(item)
            if limits and slug not in metadata:
                metadata[slug] = limits
    return response.status_code, _Catalog(tuple(dict.fromkeys(models)), metadata), version


async def _load_catalog(provider: Any) -> _Catalog:
    """Share one bounded catalog fetch between discovery and budget metadata."""
    _check_open(provider)
    _cache_key(provider)
    credentials = provider._oauth_shared_state.snapshot()
    if not (
        credentials.get("oauth_access_token") or credentials.get("oauth_refresh_token")
    ):
        return _Catalog((), {})
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
            return _Catalog((), {})
        cached = getattr(provider, "_oauth_model_catalog_cache", None)
        if (
            cached is not None
            and cached[0] == key
            and cached[1] > time.monotonic()
            and isinstance(cached[2], _Catalog)
        ):
            return cached[2]

        catalog = _Catalog((), {})
        ttl = 300
        request_key = key

        async def fetch_with_refresh() -> _Catalog:
            nonlocal request_key
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
            status, data, attempted_version = await _request_catalog(provider)
            if status in (401, 403):
                if await provider._refresh_after_auth_failure(attempted_version):
                    request_key = _cache_key(provider)
                    status, data, _version = await _request_catalog(provider)
            if status < 200 or status >= 300:
                raise ValueError("Model catalog request failed")
            return data

        try:
            catalog = await asyncio.wait_for(
                fetch_with_refresh(), timeout=CATALOG_TIMEOUT_SECONDS
            )
        except Exception:
            ttl = 30
            catalog = _Catalog((), {})
            logger.warning("Codex model catalog unavailable; using configured models and limits.")

        _check_open(provider)
        if _cache_key(provider) != request_key:
            return _Catalog((), {})
        provider._oauth_model_catalog_cache = (request_key, time.monotonic() + ttl, catalog)
        return catalog


async def discover_models(provider: Any, fallback: list[str]) -> list[str]:
    """Append account model IDs while preserving configured order.

    Failures use the current fallback. Only validated IDs and context limits are
    cached; credentials, raw responses and backend errors are never logged.
    """
    catalog = await _load_catalog(provider)
    return _merge(fallback, catalog.models)


async def get_model_context_metadata(provider: Any, model: str) -> dict[str, int]:
    """Return validated account-specific context limits for an effective model.

    This explicit metadata query is independent of ``oauth_auto_models``, which
    controls model-list discovery. It shares the same authenticated bounded
    catalog and returns an isolated copy, without changing provider config or
    inferring limits from public model metadata. Unknown or unavailable limits
    produce an empty dict; cancellation and closed-provider errors propagate.
    """
    _check_open(provider)
    if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
        return {}
    catalog = await _load_catalog(provider)
    return dict(catalog.context_metadata.get(model, {}))
