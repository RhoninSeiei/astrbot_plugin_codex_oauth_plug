"""Isolated tests of the actual main.py lifecycle methods.

Manager methods below mirror core manager.py:637-658, 713-875, 943-988:
merge sources, append before indexing, remove one list entry, delete map only
AFTER await terminate. No AstrBot imports or live state are used.

Adapted from the locally verified suite (7 tests) to read the repository
root main.py instead of a plugin/ subdirectory copy.
"""
import ast
import asyncio
import logging
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from test_provider import _install_fake_astrbot_runtime

_install_fake_astrbot_runtime()

from oauth_plug_openai_codex.provider_lifecycle import (  # noqa: E402
    close_retired_generations,
    finish_cleanup,
    reconcile_providers,
)

TYPE = 'oauth_plug_openai_codex_chat_completion'
STATE = {}
MAIN_PY = Path(__file__).resolve().parent.parent / 'main.py'

class Instance:
    def __init__(self, config, service, fail=False):
        self.provider_config = config
        self._oauth_service = service
        self.fail = fail
        self.closed = False
        self.cleanups = 0

    async def terminate(self):
        if not self.closed:
            self.cleanups += 1
            self.closed = True
        if self.fail:
            raise RuntimeError('cleanup failed')

class Manager:
    def __init__(self):
        self.resource_lock = asyncio.Lock()
        self.reload_lock = asyncio.Lock()
        self._mcp_init_task = object()
        self.providers_config = []
        self.provider_sources_config = [{'id': 'source', 'type': TYPE, 'enable': True}]
        self.provider_insts = []
        self.inst_map = {}
        self.curr_provider_inst = None
        self.loads = []
        self.evictions = []
        self.fail_load = set()

    def get_merged_provider_config(self, config):
        source = next((s for s in self.provider_sources_config
                       if s['id'] == config.get('provider_source_id')), {})
        return {**source, **config}

    async def load_provider(self, config):
        config = self.get_merged_provider_config(config)
        if not config['enable']:
            return
        self.loads.append(config['id'])
        if config['id'] in self.fail_load:
            raise RuntimeError('load failed')
        instance = Instance(config, STATE['service'])
        self.provider_insts.append(instance)
        if not self.curr_provider_inst:
            self.curr_provider_inst = instance
        self.inst_map[config['id']] = instance

    async def terminate_provider(self, provider_id):
        instance = self.inst_map.get(provider_id)
        if instance is None:
            return
        self.evictions.append(provider_id)
        if instance in self.provider_insts:
            self.provider_insts.remove(instance)
        if instance is self.curr_provider_inst:
            self.curr_provider_inst = None
        await instance.terminate()
        del self.inst_map[provider_id]

    def add(self, provider_id, service, type=TYPE, fail=False):
        instance = Instance({'id': provider_id, 'type': type}, service, fail)
        self.inst_map[provider_id] = instance
        self.provider_insts.append(instance)
        self.curr_provider_inst = instance
        return instance


def plugin_class():
    tree = ast.parse(MAIN_PY.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    names = {'initialize', 'terminate', '_reconcile_providers', '_remove_owned_web_apis',
             '_cleanup_lifecycle', '_register_web_apis'}
    cls.body = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
                and n.name in names]
    cls.bases = []
    namespace = dict(logger=logging.getLogger('lifecycle-test'), PROVIDER_TYPE=TYPE,
                     asyncio=asyncio,
                     set_service=lambda service: STATE.update(service=service),
                     get_service=lambda: STATE.get('service'),
                     reconcile_providers=reconcile_providers,
                     close_retired_generations=close_retired_generations,
                     finish_cleanup=finish_cleanup,
                     register_provider_adapter=lambda: STATE.update(registered=True),
                     unregister_provider_adapter=lambda: STATE.update(registered=False))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(MAIN_PY), 'exec'), namespace)
    return namespace[cls.name]

Plugin = plugin_class()

class Tests(unittest.IsolatedAsyncioTestCase):
    def make(self, manager):
        plugin = Plugin()
        routes = []
        plugin.context = SimpleNamespace(provider_manager=manager, registered_web_apis=routes,
                                         register_web_api=lambda *args: routes.append(args))
        plugin.tools_only = False
        plugin.service = SimpleNamespace(is_enabled=lambda: True)
        async def close():
            self.assertFalse(any(instance._oauth_service is plugin.service
                                 for instance in getattr(manager, 'inst_map', {}).values()))
            self.assertFalse(any(instance._oauth_service is plugin.service
                                 for instance in getattr(manager, 'provider_insts', [])))
            for instance in list(getattr(manager, "provider_insts", [])) + self.retired:
                if instance._oauth_service is plugin.service:
                    await instance.terminate()
        plugin.service.close = close
        plugin.usage_service = SimpleNamespace(close=AsyncMock())
        for name in ('start', 'complete', 'refresh', 'test', 'disconnect'):
            setattr(plugin, 'api_' + name, MethodType(lambda _self: None, plugin))
        return plugin

    async def asyncSetUp(self):
        STATE.clear()
        self.retired = []

    async def test_terminate_evicts_before_close_and_double_cleanup_is_safe(self):
        manager = Manager()
        plugin = self.make(manager)
        owned = manager.add('owned', plugin.service)
        manager.provider_insts.append(owned)  # core API removes only one occurrence
        self.retired.append(owned)
        foreign = manager.add('foreign', object(), 'openai_chat_completion')
        await plugin.terminate()
        self.assertEqual(manager.inst_map, {'foreign': foreign})
        self.assertEqual(manager.provider_insts, [foreign])
        self.assertEqual(len(manager.evictions), 1)
        self.assertEqual(owned.cleanups, 1)
        self.assertFalse(foreign.closed)

    async def test_initialize_rebuilds_merged_enabled_configs_once(self):
        manager = Manager()
        old = manager.add('old', object())
        foreign = manager.add('foreign', object(), 'other_plugin_type')
        manager.providers_config = [
            {'id': 'old', 'provider_source_id': 'source', 'enable': True},
            {'id': 'new', 'type': TYPE, 'enable': True},
            {'id': 'off', 'type': TYPE, 'enable': False},
            {'id': 'foreign', 'type': 'other_plugin_type', 'enable': True},
            {'id': 'new', 'type': TYPE, 'enable': True},
            {'id': 'foreign', 'type': TYPE, 'enable': True},
        ]
        plugin = self.make(manager)
        await plugin.initialize()
        await plugin.initialize()
        self.assertEqual(manager.loads, ['old', 'new'])
        self.assertTrue(old.closed)
        self.assertEqual(len(manager.provider_insts), 3)
        self.assertIs(manager.inst_map['foreign'], foreign)
        for key in ('old', 'new'):
            self.assertIs(manager.inst_map[key]._oauth_service, plugin.service)
        self.assertTrue(STATE['registered'])

    async def test_eviction_and_load_failures_are_isolated(self):
        manager = Manager()
        plugin = self.make(manager)
        failed = manager.add('broken', plugin.service, fail=True)
        manager.add('second', plugin.service)
        with self.assertRaises(RuntimeError):
            await plugin.terminate()
        self.assertEqual(manager.inst_map, {})
        self.assertEqual(manager.provider_insts, [])
        self.assertTrue(failed.closed)
        manager.providers_config = [{'id': key, 'type': TYPE, 'enable': True}
                                    for key in ('bad', 'good')]
        manager.fail_load.add('bad')
        plugin = self.make(manager)
        await plugin.initialize()
        self.assertEqual(set(manager.inst_map), {'good'})

    async def test_cold_start_defers_to_core_initial_load(self):
        manager = Manager()
        manager._mcp_init_task = None
        manager.providers_config = [{'id': 'owned', 'type': TYPE, 'enable': True}]
        plugin = self.make(manager)
        await plugin.initialize()
        self.assertEqual(manager.loads, [])
        for config in manager.providers_config:
            await manager.load_provider(config)
        manager._mcp_init_task = object()
        await plugin.initialize()
        self.assertEqual(manager.loads, ['owned'])

    async def test_tools_only_and_disabled_do_not_rebuild(self):
        for tools_only, enabled in ((True, True), (False, False)):
            manager = Manager()
            manager.providers_config = [{'id': 'owned', 'type': TYPE, 'enable': True}]
            plugin = self.make(manager)
            plugin.tools_only = tools_only
            plugin.service.is_enabled = lambda: enabled
            await plugin.initialize()
            self.assertEqual(manager.loads, [])

    async def test_list_only_orphan_is_evicted_without_touching_foreign_id(self):
        manager = Manager()
        plugin = self.make(manager)
        orphan = manager.add('same', plugin.service)
        foreign = manager.add('same', object(), 'core_type')
        await plugin.terminate()
        self.assertTrue(orphan.closed)
        self.assertFalse(foreign.closed)
        self.assertEqual(manager.inst_map, {'same': foreign})
        self.assertEqual(manager.provider_insts, [foreign])
        self.assertEqual(len(manager.evictions), 1)

    async def test_manager_failure_does_not_break_lifecycle(self):
        plugin = self.make(SimpleNamespace())
        await plugin.initialize()
        await plugin.terminate()

    async def test_old_termination_preserves_new_generation_and_routes(self):
        manager = Manager()
        old = self.make(manager)
        new = self.make(manager)
        old.context = new.context
        old_instance = manager.add('old-only', old.service)
        new_instance = manager.add('same-id', new.service)
        STATE['service'] = new.service
        STATE['registered'] = True
        old.context.registered_web_apis.extend([
            ('oauth-plug-openai-codex/start', old.api_start),
            ('oauth-plug-openai-codex/start', new.api_start),
        ])
        await old.terminate()
        self.assertTrue(old_instance.closed)
        self.assertFalse(new_instance.closed)
        self.assertIs(manager.inst_map['same-id'], new_instance)
        self.assertIs(STATE['service'], new.service)
        self.assertTrue(STATE['registered'])
        self.assertEqual(old.context.registered_web_apis,
                         [('oauth-plug-openai-codex/start', new.api_start)])

    async def test_cancelled_lock_wait_still_cleans_services_and_routes(self):
        manager = Manager()
        plugin = self.make(manager)
        instance = manager.add('own', plugin.service)
        STATE['service'] = plugin.service
        plugin.context.registered_web_apis.append(
            ('oauth-plug-openai-codex/start', plugin.api_start))
        closed = AsyncMock(wraps=plugin.service.close)
        plugin.service.close = closed
        await manager.resource_lock.acquire()
        try:
            task = asyncio.create_task(plugin.terminate())
            await asyncio.sleep(0)
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertIs(manager.inst_map['own'], instance)
            self.assertFalse(instance.closed)
            manager.resource_lock.release()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(manager.resource_lock.locked())
            self.assertFalse(manager.reload_lock.locked())
            self.assertEqual(manager.inst_map, {})
            self.assertTrue(instance.closed)
            closed.assert_awaited_once()
            plugin.usage_service.close.assert_awaited_once()
            self.assertEqual(plugin.context.registered_web_apis, [])
            self.assertIsNone(STATE['service'])
        finally:
            if manager.resource_lock.locked():
                manager.resource_lock.release()

    async def test_repeated_cancel_waits_for_cleanup_and_preserves_cancellation(self):
        manager = Manager()
        plugin = self.make(manager)
        entered, release = asyncio.Event(), asyncio.Event()

        async def close():
            entered.set()
            await release.wait()
            raise RuntimeError('cleanup error must not hide cancellation')

        plugin.service.close = close
        await manager.reload_lock.acquire()
        task = asyncio.create_task(plugin.terminate())
        try:
            await asyncio.sleep(0)
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(task.done())
            manager.reload_lock.release()
            await entered.wait()
            self.assertFalse(manager.resource_lock.locked())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            plugin.usage_service.close.assert_awaited_once()
        finally:
            release.set()
            if manager.reload_lock.locked():
                manager.reload_lock.release()

    async def test_initialize_cancelled_while_waiting_retires_previous_generation(self):
        manager = Manager()
        previous = SimpleNamespace(close=AsyncMock())
        STATE['service'] = previous
        old = manager.add('old', previous)
        plugin = self.make(manager)
        close = AsyncMock(wraps=plugin.service.close)
        plugin.service.close = close
        await manager.resource_lock.acquire()
        try:
            task = asyncio.create_task(plugin.initialize())
            await asyncio.sleep(0)
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertIs(manager.inst_map['old'], old)
            manager.resource_lock.release()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(old.closed)
            self.assertEqual(manager.inst_map, {})
            previous.close.assert_awaited_once()
            close.assert_awaited_once()
            plugin.usage_service.close.assert_awaited_once()
            self.assertIsNone(STATE['service'])
        finally:
            if manager.resource_lock.locked():
                manager.resource_lock.release()

    async def test_initialize_closes_old_service_and_ignores_stale_generation(self):
        manager = Manager()
        old_service = SimpleNamespace(close=AsyncMock())
        STATE['service'] = old_service
        manager.add('old', old_service)
        manager.providers_config = [{'id': 'active', 'type': TYPE, 'enable': True}]
        plugin = self.make(manager)
        await plugin.initialize()
        old_service.close.assert_awaited_once()
        self.assertIs(manager.inst_map['active']._oauth_service, plugin.service)

        stale = self.make(manager)
        newer = self.make(manager)
        await manager.resource_lock.acquire()
        task = asyncio.create_task(stale.initialize())
        await asyncio.sleep(0)
        STATE['service'] = newer.service
        newest = manager.add('newest', newer.service)
        manager.resource_lock.release()
        await task
        self.assertFalse(newest.closed)
        self.assertIs(manager.inst_map['newest'], newest)
        self.assertIs(STATE['service'], newer.service)
        stale.usage_service.close.assert_awaited_once()

    async def test_cleanup_cancellation_takes_priority_over_initialization_error(self):
        manager = Manager()
        plugin = self.make(manager)
        entered, release = asyncio.Event(), asyncio.Event()

        def fail_registration():
            raise ValueError('registration failed')

        async def close():
            entered.set()
            await release.wait()

        plugin._register_web_apis = fail_registration
        plugin.service.close = close
        task = asyncio.create_task(plugin.initialize())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        plugin.usage_service.close.assert_awaited_once()

if __name__ == '__main__':
    unittest.main()
