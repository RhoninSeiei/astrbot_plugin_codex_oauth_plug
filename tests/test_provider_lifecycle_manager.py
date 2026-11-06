"""Execute lifecycle compatibility against the installed core manager itself."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

try:
    from astrbot.core.provider.manager import ProviderManager
    from astrbot.core.provider import manager as manager_module
    from astrbot.core.provider.entities import ProviderType
    from astrbot.core.provider.provider import Provider
    from astrbot.core.provider.register import provider_cls_map

    CORE_AVAILABLE = True
except ImportError:
    CORE_AVAILABLE = False

from oauth_plug_openai_codex.provider_lifecycle import (  # noqa: E402
    _CURRENT,
    _LISTS,
    reconcile_providers,
)
from oauth_plug_openai_codex.service import PROVIDER_TYPE  # noqa: E402


@unittest.skipUnless(CORE_AVAILABLE, "Actual AstrBot ProviderManager is unavailable")
class CoreManagerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        manager = ProviderManager.__new__(ProviderManager)
        for name in _LISTS:
            setattr(manager, name, [])
        for name in _CURRENT:
            setattr(manager, name, None)
        manager.inst_map = {}
        manager.resource_lock = asyncio.Lock()
        manager.reload_lock = asyncio.Lock()
        manager._mcp_init_task = object()
        manager.providers_config = []
        manager.provider_sources_config = []
        manager.provider_settings = {}
        manager.default_chat_provider_id = ""
        manager.db_helper = None
        manager._openai_oauth_shared_states = {}
        return manager

    async def reconcile(self, manager, service, *, rebuild=False):
        await reconcile_providers(
            SimpleNamespace(provider_manager=manager), service,
            rebuild=rebuild, retire_previous=rebuild,
            is_current=lambda: True, retired_services=[], pending=[],
        )

    async def test_real_terminate_cannot_delete_same_id_replacement_or_aliases(self):
        manager = self.manager()
        service, newer = object(), object()
        replacement = SimpleNamespace(_oauth_service=newer, provider_config={"type": PROVIDER_TYPE})
        old = SimpleNamespace(
            _oauth_service=service,
            provider_config={"id": "same", "type": PROVIDER_TYPE},
        )

        async def cleanup():
            self.assertTrue(all(old is not item for item in manager.inst_map.values()))
            for name in _LISTS:
                self.assertTrue(all(old is not item for item in getattr(manager, name)))
            self.assertTrue(all(getattr(manager, name) is not old for name in _CURRENT))
            manager.inst_map["same"] = replacement
            manager.inst_map["alias"] = replacement
            manager.provider_insts.append(replacement)
            manager.curr_provider_inst = replacement
            await asyncio.sleep(0)

        old.terminate = AsyncMock(side_effect=cleanup)
        manager.inst_map = {"same": old, "alias": old}
        for name in _LISTS:
            getattr(manager, name).extend([old, old])
        for name in _CURRENT:
            setattr(manager, name, old)
        await self.reconcile(manager, service)
        old.terminate.assert_awaited_once()
        self.assertEqual(manager.inst_map, {"same": replacement, "alias": replacement})
        self.assertEqual(manager.provider_insts, [replacement])
        self.assertIs(manager.curr_provider_inst, replacement)

    async def test_real_load_rebuilds_enabled_configs_without_resurrecting_removed(self):
        manager = self.manager()
        service, previous = object(), object()
        constructed = []

        class LocalProvider(Provider):
            def __init__(self, config, settings):
                super().__init__(config, settings)
                self._oauth_service = service
                self.closed = False
                constructed.append(self)

            def get_current_key(self):
                return ""

            def set_key(self, key):
                pass

            async def get_models(self):
                return []

            async def text_chat(self, *args, **kwargs):
                raise AssertionError("Lifecycle tests must not request models")

            async def terminate(self):
                self.closed = True

        old = SimpleNamespace(
            _oauth_service=previous,
            provider_config={"id": "removed", "type": PROVIDER_TYPE},
            terminate=AsyncMock(),
        )
        manager.inst_map["removed"] = old
        manager.provider_insts.extend([old, old])
        manager.providers_config = [
            {"id": "active", "type": PROVIDER_TYPE, "enable": True},
            {"id": "disabled", "type": PROVIDER_TYPE, "enable": False},
            {"id": "active", "type": PROVIDER_TYPE, "enable": True},
        ]
        metadata = SimpleNamespace(
            cls_type=LocalProvider, provider_type=ProviderType.CHAT_COMPLETION,
            type=PROVIDER_TYPE, id="default",
        )
        with (
            patch.dict(provider_cls_map, {PROVIDER_TYPE: metadata}),
            patch.object(manager, "dynamic_import_provider", lambda _type: None),
            patch.object(manager_module, "instrument_provider", lambda *_args: None, create=True),
        ):
            await self.reconcile(manager, service, rebuild=True)
            await self.reconcile(manager, service, rebuild=True)
        old.terminate.assert_awaited_once()
        self.assertEqual(set(manager.inst_map), {"active"})
        self.assertEqual(len(constructed), 1)
        self.assertIs(manager.inst_map["active"]._oauth_service, service)

    async def test_cancelled_plugin_cleanup_waits_for_real_manager_update(self):
        from test_plugin_lifecycle import STATE, Tests

        manager = self.manager()
        factory = Tests()
        factory.retired = []
        plugin = factory.make(manager)
        STATE['service'] = plugin.service
        entered, release = asyncio.Event(), asyncio.Event()

        async def cleanup():
            entered.set()
            await release.wait()

        old = SimpleNamespace(
            _oauth_service=plugin.service,
            provider_config={"id": "same", "type": PROVIDER_TYPE},
            terminate=AsyncMock(side_effect=cleanup),
        )
        manager.inst_map['same'] = old

        class Config(dict):
            def save_config(self):
                pass

        disabled = {"id": "same", "type": PROVIDER_TYPE, "enable": False}
        config = Config(provider=[dict(disabled)], provider_sources=[])
        manager.acm = SimpleNamespace(default_conf=config)
        manager.providers_config = config['provider']
        update = stop = None
        try:
            with patch.object(manager_module, 'astrbot_config', config):
                # Both lock acquisition and the suspend inside terminate_provider
                # execute the installed core's update/reload/terminate methods.
                update = asyncio.create_task(manager.update_provider('same', disabled))
                await entered.wait()
                before = dict(manager.inst_map)
                self.assertTrue(manager.resource_lock.locked())
                self.assertTrue(manager.reload_lock.locked())
                stop = asyncio.create_task(plugin.terminate())
                await asyncio.sleep(0)
                stop.cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
                self.assertFalse(stop.done())
                # Old core still needs its map entry after its suspended cleanup;
                # new core has already detached it. Preserve either core's state.
                self.assertEqual(set(manager.inst_map), set(before))
                for key, instance in before.items():
                    self.assertIs(manager.inst_map[key], instance)
                release.set()
                await update
                with self.assertRaises(asyncio.CancelledError):
                    await stop
            self.assertEqual(manager.inst_map, {})
            plugin.usage_service.close.assert_awaited_once()
            old.terminate.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (update, stop) if task is not None),
                return_exceptions=True,
            )


if __name__ == "__main__":
    unittest.main()
