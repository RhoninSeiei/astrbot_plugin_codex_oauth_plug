"""Generation-scoped provider retirement for old and current AstrBot cores."""

from __future__ import annotations

import asyncio
import copy
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Callable

from astrbot import logger

from .service import PROVIDER_TYPE

_LISTS = (
    "provider_insts", "stt_provider_insts", "tts_provider_insts",
    "embedding_provider_insts", "rerank_provider_insts", "decision_provider_insts",
)
_CURRENT = (
    "curr_provider_inst", "curr_stt_provider_inst", "curr_tts_provider_inst",
    "curr_decision_provider_inst",
)


def _instances(manager: Any) -> list[Any]:
    candidates = list(getattr(manager, "inst_map", {}).values())
    for name in _LISTS:
        candidates.extend(getattr(manager, name, ()))
    candidates.extend(getattr(manager, name, None) for name in _CURRENT)
    return list({id(instance): instance for instance in candidates if instance is not None}.values())


def _owned(instance: Any) -> bool:
    config = getattr(instance, "provider_config", None)
    return isinstance(config, dict) and config.get("type") == PROVIDER_TYPE


def _detach(manager: Any, instance: Any) -> None:
    """Remove every public identity reference without awaiting cleanup."""
    for name in _LISTS:
        values = getattr(manager, name, None)
        if isinstance(values, list):
            values[:] = [value for value in values if value is not instance]
    for name in _CURRENT:
        if getattr(manager, name, None) is instance:
            setattr(manager, name, None)
    mapping = getattr(manager, "inst_map", {})
    for key, value in list(mapping.items()):
        if value is instance:
            mapping.pop(key, None)


async def _retire(manager: Any, instance: Any) -> None:
    # 4.29.4 deletes inst_map[id] AFTER await terminate(). Its original ID may
    # already belong to a replacement, so run the core's normal cleanup method
    # against a private retirement view, never the publicly reachable mapping.
    view = copy.copy(manager)
    for name in _LISTS:
        setattr(view, name, [])
    for name in _CURRENT:
        setattr(view, name, None)
    provider_id = "__oauth_retired__"
    view.inst_map = {provider_id: instance}
    await type(manager).terminate_provider(view, provider_id)


@asynccontextmanager
async def _manager_locks(manager: Any):
    # ProviderManager.update_provider() holds resource_lock before reload().
    # Cleanup must wait for that operation even when its caller was cancelled:
    # an old core may still need its public map entry after terminate() awaits.
    async with AsyncExitStack() as stack:
        entered: list[Any] = []
        for name in ("resource_lock", "reload_lock"):
            lock = getattr(manager, name, None)
            if lock is not None and all(lock is not item for item in entered):
                await stack.enter_async_context(lock)
                entered.append(lock)
        yield


async def reconcile_providers(
    context: Any,
    service: Any,
    *,
    rebuild: bool,
    retire_previous: bool,
    is_current: Callable[[], bool],
    retired_services: list[Any],
    pending: list[tuple[Any, Any]],
) -> None:
    manager = getattr(context, "provider_manager", None)
    if manager is None or not isinstance(getattr(manager, "inst_map", None), dict):
        return
    async with _manager_locks(manager):
        if retire_previous and not is_current():
            return
        candidates = []
        for instance in _instances(manager):
            generation = getattr(instance, "_oauth_service", None)
            selected = (
                generation is not service or not rebuild
                if retire_previous else generation is service
            )
            if _owned(instance) and selected:
                candidates.append(instance)
                _detach(manager, instance)
                pending.append((manager, instance))
                if generation is not None and generation is not service:
                    if all(generation is not item for item in retired_services):
                        retired_services.append(generation)
        for instance in candidates:
            try:
                await _retire(manager, instance)
            except Exception:
                logger.warning("Codex OAuth provider cleanup failed.")
            else:
                pending[:] = [(m, p) for m, p in pending if p is not instance]
        if not rebuild or not is_current():
            return
        # The normal cold-start manager pass loads all config entries itself.
        if getattr(manager, "_mcp_init_task", None) is None:
            return
        for config in list(manager.providers_config):
            if not is_current():
                return
            try:
                merged = manager.get_merged_provider_config(config)
                if merged.get("type") != PROVIDER_TYPE or not merged.get("enable"):
                    continue
                if merged["id"] not in manager.inst_map:
                    await manager.load_provider(config)
            except Exception:
                logger.warning("Codex OAuth provider rebuild failed.")


async def close_retired_generations(
    context: Any,
    services: list[Any],
    pending: list[tuple[Any, Any]],
    *,
    protected: Any = None,
) -> None:
    """Finish cleanup even if reconciliation was cancelled while taking locks."""
    generations = list({id(item): item for item in services if item is not None and item is not protected}.values())
    errors: list[BaseException] = []
    managers = [getattr(context, "provider_manager", None), *(owner for owner, _ in pending)]
    managers = list({id(owner): owner for owner in managers if owner is not None}.values())
    for manager in managers:
        async with _manager_locks(manager):
            for instance in _instances(manager):
                if _owned(instance) and any(
                    getattr(instance, "_oauth_service", None) is item for item in generations
                ):
                    _detach(manager, instance)
                    if not any(existing is instance for _, existing in pending):
                        pending.append((manager, instance))
            for owner, instance in list(pending):
                if owner is not manager:
                    continue
                generation = getattr(instance, "_oauth_service", None)
                if protected is not None and generation is protected:
                    continue
                try:
                    await _retire(owner, instance)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    pending[:] = [(m, p) for m, p in pending if p is not instance]
    for generation in generations:
        close = getattr(generation, "close", None)
        if close is not None:
            try:
                await close()
            except BaseException as exc:
                errors.append(exc)
    if errors:
        if any(isinstance(error, asyncio.CancelledError) for error in errors):
            raise asyncio.CancelledError
        raise RuntimeError("Codex OAuth generation cleanup failed") from None


async def finish_cleanup(awaitable: Any) -> None:
    """Wait for cleanup on repeated cancellation, then propagate cancellation."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    if cancelled:
        # Retrieve any exception so a cancelled caller cannot leak task warnings.
        try:
            task.result()
        except BaseException:
            pass
        raise asyncio.CancelledError
    task.result()
