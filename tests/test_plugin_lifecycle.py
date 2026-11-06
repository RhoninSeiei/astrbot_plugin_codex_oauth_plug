"""Dependency-free tests of the actual main.py lifecycle methods.

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
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

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
    tree = ast.parse(MAIN_PY.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    names = {'initialize', 'terminate', '_reconcile_providers', '_remove_owned_web_apis'}
    cls.body = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
                and n.name in names]
    cls.bases = []
    namespace = dict(logger=logging.getLogger('lifecycle-test'), PROVIDER_TYPE=TYPE,
                     set_service=lambda service: STATE.update(service=service),
                     get_service=lambda: STATE.get('service'),
                     register_provider_adapter=lambda: STATE.update(registered=True),
                     unregister_provider_adapter=lambda: STATE.update(registered=False))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(MAIN_PY), 'exec'), namespace)
    return namespace[cls.name]

Plugin = plugin_class()

class Tests(unittest.IsolatedAsyncioTestCase):
    def make(self, manager):
        plugin = Plugin()
        plugin.context = SimpleNamespace(provider_manager=manager, registered_web_apis=[],
                                         register_web_api=lambda *a: None)
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
            setattr(plugin, 'api_' + name, lambda: None)
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
        self.assertEqual(manager.evictions, ['owned'])
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
        failed = manager.add('broken', object(), fail=True)
        manager.add('second', object())
        plugin = self.make(manager)
        await plugin.terminate()
        self.assertEqual(manager.inst_map, {})
        self.assertEqual(manager.provider_insts, [])
        self.assertTrue(failed.closed)
        manager.providers_config = [{'id': key, 'type': TYPE, 'enable': True}
                                    for key in ('bad', 'good')]
        manager.fail_load.add('bad')
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
        orphan = manager.add('same', object())
        foreign = manager.add('same', object(), 'core_type')
        plugin = self.make(manager)
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

if __name__ == '__main__':
    unittest.main()
