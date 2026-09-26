import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from oauth_plug_openai_codex.usage_tool import UsageToolService


class UsageToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = SimpleNamespace(provider_config={"type": "openai_oauth_chat_completion"})
        self.context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=self.provider))
        self.reader = SimpleNamespace(read=AsyncMock(return_value={"status": "success", "windows": []}), close=AsyncMock())
        self.config = {"usage": {"enabled": True, "group_allowlist": []}}
        self.service = UsageToolService(self.context, self.config, self.reader)
        self.event = SimpleNamespace(is_admin=lambda: True, is_private_chat=lambda: True, unified_msg_origin="test:FriendMessage:1")

    async def test_admin_private_queries_selected_oauth_provider(self):
        result = await self.service.run(self.event)
        self.assertEqual(result["status"], "success")
        self.reader.read.assert_awaited_once_with(self.provider)
        self.context.get_using_provider_async.assert_awaited_once_with(self.event.unified_msg_origin)

    async def test_non_admin_denied_before_resolving_account(self):
        self.event.is_admin = lambda: False
        self.assertEqual((await self.service.run(self.event))["status"], "denied")
        self.context.get_using_provider_async.assert_not_awaited()
        self.reader.read.assert_not_awaited()

    async def test_group_requires_exact_origin_allowlist(self):
        self.event.is_private_chat = lambda: False
        self.config["usage"]["group_allowlist"] = ["1"]
        self.assertEqual((await self.service.run(self.event))["status"], "denied")
        self.config["usage"]["group_allowlist"] = [self.event.unified_msg_origin]
        self.assertEqual((await self.service.run(self.event))["status"], "success")

    async def test_other_provider_never_queries_an_oauth_account(self):
        self.provider.provider_config["type"] = "openai_chat_completion"
        self.assertEqual((await self.service.run(self.event))["status"], "not_oauth_provider")
        self.reader.read.assert_not_awaited()

    async def test_plugin_oauth_provider_supported(self):
        self.provider.provider_config["type"] = "oauth_plug_openai_codex_chat_completion"
        self.assertEqual((await self.service.run(self.event))["status"], "success")

    async def test_configured_target_works_without_chat_model_lookup(self):
        self.config["usage"]["provider_id"] = "oauth/model"
        self.context.get_provider_by_id = Mock(return_value=self.provider)
        self.context.get_using_provider_async.side_effect = RuntimeError("no chat model")
        self.assertEqual((await self.service.run(self.event))["status"], "success")
        self.context.get_provider_by_id.assert_called_once_with("oauth/model")
        self.context.get_using_provider_async.assert_not_awaited()
        self.reader.read.assert_awaited_once_with(self.provider)

    async def test_invalid_explicit_target_never_falls_back_to_another_account(self):
        self.config["usage"]["provider_id"] = "missing"
        self.context.get_provider_by_id = Mock(return_value=None)
        self.assertEqual((await self.service.run(self.event))["status"], "provider_not_found")
        self.context.get_using_provider_async.assert_not_awaited()
        self.reader.read.assert_not_awaited()

    async def test_explicit_non_oauth_target_is_rejected(self):
        self.config["usage"]["provider_id"] = "other/model"
        self.context.get_provider_by_id = Mock(return_value=SimpleNamespace(provider_config={"type":"openai_chat_completion"}))
        self.assertEqual((await self.service.run(self.event))["status"], "not_oauth_provider")
        self.reader.read.assert_not_awaited()

    async def test_target_changed_during_request_hides_old_result(self):
        self.config["usage"]["provider_id"] = "oauth/model"
        self.context.get_provider_by_id = Mock(return_value=self.provider)
        async def read(provider):
            self.config["usage"]["provider_id"] = "other/model"
            return {"status": "success"}
        self.reader.read.side_effect = read
        self.assertEqual((await self.service.run(self.event))["status"], "authorization_changed")

    async def test_disabled_and_closed_never_query(self):
        self.config["usage"]["enabled"] = False
        self.assertEqual((await self.service.run(self.event))["status"], "disabled")
        self.config["usage"]["enabled"] = True
        await self.service.close()
        self.assertEqual((await self.service.run(self.event))["status"], "closed")
        self.reader.read.assert_not_awaited()

    async def test_lookup_failure_does_not_leak_exception(self):
        self.context.get_using_provider_async.side_effect = RuntimeError("secret-account")
        self.assertEqual(await self.service.run(self.event), {"status": "unavailable"})

    async def test_permission_revoked_during_lookup(self):
        async def lookup(umo):
            self.event.is_admin = lambda: False
            return self.provider
        self.context.get_using_provider_async.side_effect = lookup
        self.assertEqual((await self.service.run(self.event))["status"], "denied")
        self.reader.read.assert_not_awaited()

    async def test_permission_revoked_during_request_hides_result(self):
        async def read(provider):
            self.event.is_admin = lambda: False
            return {"status": "success", "windows": []}
        self.reader.read.side_effect = read
        self.assertEqual((await self.service.run(self.event))["status"], "denied")
