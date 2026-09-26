import sys
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

if Path("/work/astrbot").exists():
    sys.path.append("/work")

try:
    from astrbot.core.provider.register import provider_cls_map

    from main import OAuthPlugOpenAICodexPlugin
    from astrbot.core.star.star_handler import star_handlers_registry
    from oauth_plug_openai_codex.registration import (
        register_provider_adapter,
        unregister_provider_adapter,
    )
    from oauth_plug_openai_codex.service import PROVIDER_TYPE
    from astrbot.core.config.default import CONFIG_METADATA_2

    ASTRBOT_AVAILABLE = True
except Exception:
    ASTRBOT_AVAILABLE = False


@unittest.skipUnless(ASTRBOT_AVAILABLE, "AstrBot runtime is not available")
class AstrBotRegistrationTests(unittest.TestCase):
    def test_quota_command_returns_text_without_llm_and_stops_event(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock

        async def check():
            provider = SimpleNamespace(provider_config={"type":"openai_oauth_chat_completion"})
            context = SimpleNamespace(get_provider_by_id=Mock(return_value=provider),get_using_provider_async=AsyncMock(side_effect=AssertionError("chat model must not be resolved")))
            plugin = OAuthPlugOpenAICodexPlugin(context, {"runtime":{"tools_only":True},"usage":{"provider_id":"oauth/model"}})
            plugin.usage_service.reader.read=AsyncMock(return_value={"status":"success","windows":[{"used_percent":61,"remaining_percent":39,"window_seconds":604800}]})
            event=SimpleNamespace(is_admin=lambda:True,is_private_chat=lambda:True,unified_msg_origin="test:FriendMessage:1",plain_result=lambda value:value,stop_event=Mock())
            results=[r async for r in plugin.command_usage(event)]
            self.assertEqual(len(results),1)
            self.assertIn("已用 61%，剩余 39%",results[0])
            event.stop_event.assert_called_once()
            context.get_using_provider_async.assert_not_awaited()
            await plugin.terminate()

        asyncio.run(check())

    def test_tools_only_mode_does_not_register_provider_or_auth_routes(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import Mock
        from oauth_plug_openai_codex.service import get_service

        async def check():
            unregister_provider_adapter()
            context = SimpleNamespace(register_web_api=Mock())
            plugin = OAuthPlugOpenAICodexPlugin(context, {"runtime": {"tools_only": True}})
            await plugin.initialize()
            self.assertNotIn(PROVIDER_TYPE, provider_cls_map)
            self.assertIsNot(get_service(), plugin.service)
            context.register_web_api.assert_not_called()
            for name, args in (("command_start", ()), ("command_complete", ("secret",)), ("command_refresh", ()), ("command_test", ())):
                event = SimpleNamespace(plain_result=lambda value: value)
                results = [r async for r in getattr(plugin, name)(event, *args)]
                self.assertEqual(results, ["当前为仅额度工具模式，账号授权由现有 OAuth 提供商管理。"])
            await plugin.terminate()
            self.assertTrue(plugin.usage_service.closed)

        asyncio.run(check())

    def test_usage_tool_is_registered_with_no_model_supplied_account(self):
        from astrbot.core.provider.register import llm_tools

        tools = [t for t in llm_tools.func_list if t.name == "codex_oauth_usage"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].parameters.get("properties", {}), {})

    def test_tools_only_removes_only_owned_stale_auth_routes(self):
        import asyncio
        from types import SimpleNamespace

        async def check():
            context = SimpleNamespace(registered_web_apis=[])
            old = OAuthPlugOpenAICodexPlugin(context, {})
            unrelated = ("another-plugin/start", lambda: None, ["POST"], "other")
            foreign = ("oauth-plug-openai-codex/start", lambda: None, ["POST"], "foreign")
            context.registered_web_apis.extend([
                ("oauth-plug-openai-codex/start", old.api_start, ["POST"], "owned"),
                unrelated, foreign,
            ])
            plugin = OAuthPlugOpenAICodexPlugin(context, {"runtime": {"tools_only": True}})
            await plugin.initialize()
            self.assertEqual(context.registered_web_apis, [unrelated, foreign])
            await plugin.terminate()
            await old.terminate()

        asyncio.run(check())

    def tearDown(self):
        unregister_provider_adapter()
        from oauth_plug_openai_codex.service import set_service
        set_service(None)

    def test_register_provider_adapter_replaces_existing_type(self):
        register_provider_adapter()
        register_provider_adapter()

        self.assertIn(PROVIDER_TYPE, provider_cls_map)
        self.assertEqual(
            provider_cls_map[PROVIDER_TYPE].provider_display_name,
            "Codex OAuth 插件 / OpenAI",
        )

    def test_register_provider_adapter_injects_dashboard_template(self):
        register_provider_adapter()

        templates = CONFIG_METADATA_2["provider_group"]["metadata"]["provider"][
            "config_template"
        ]
        template_name = "Codex OAuth 插件 / OpenAI"

        self.assertIn(template_name, templates)
        self.assertEqual(templates[template_name]["type"], PROVIDER_TYPE)

    def test_plugin_initialize_registers_provider_and_web_apis(self):
        class FakeContext:
            def __init__(self):
                self.routes = []

            def register_web_api(self, route, view_handler, methods, desc):
                self.routes.append((route, view_handler, methods, desc))

        config = {
            "runtime": {"enabled": True},
            "oauth": {},
            "advanced": {},
        }
        context = FakeContext()
        plugin = OAuthPlugOpenAICodexPlugin(context, config)

        import asyncio

        asyncio.run(plugin.initialize())

        self.assertIn(PROVIDER_TYPE, provider_cls_map)
        self.assertEqual(
            [route[0] for route in context.routes],
            [
                "oauth-plug-openai-codex/start",
                "oauth-plug-openai-codex/complete",
                "oauth-plug-openai-codex/refresh",
                "oauth-plug-openai-codex/test",
                "oauth-plug-openai-codex/disconnect",
            ],
        )

    def test_plugin_registers_admin_only_chat_commands(self):
        expected_commands = {
            "codex_oauth_usage",
            "codex_oauth_start",
            "codex_oauth_complete",
            "codex_oauth_refresh",
            "codex_oauth_test",
        }
        handlers = star_handlers_registry.get_handlers_by_module_name("main")
        command_to_admin = {}
        for handler in handlers:
            command_names = [
                filter_.command_name
                for filter_ in handler.event_filters
                if filter_.__class__.__name__ == "CommandFilter"
            ]
            has_admin = any(
                filter_.__class__.__name__ == "PermissionTypeFilter"
                and str(getattr(filter_, "permission_type", "")).endswith(".ADMIN")
                for filter_ in handler.event_filters
            )
            for command_name in command_names:
                command_to_admin[command_name] = has_admin

        for command_name in expected_commands:
            self.assertTrue(command_to_admin.get(command_name), command_name)

    def test_plugin_imports_with_astrbot_package_path(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = (
                Path(tmp)
                / "data"
                / "plugins"
                / "astrbot_plugin_codex_oauth_plug"
            )
            shutil.copytree(
                repo_root,
                plugin_dir,
                ignore=shutil.ignore_patterns(
                    ".git",
                    "__pycache__",
                    "*.pyc",
                    ".pytest_cache",
                    ".ruff_cache",
                ),
            )
            script = (
                "import sys; "
                f"sys.path.insert(0, {tmp!r}); "
                "import data.plugins.astrbot_plugin_codex_oauth_plug.main"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                cwd=tmp,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
