import sys
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

    ASTRBOT_AVAILABLE = True
except Exception:
    ASTRBOT_AVAILABLE = False


@unittest.skipUnless(ASTRBOT_AVAILABLE, "AstrBot runtime is not available")
class AstrBotRegistrationTests(unittest.TestCase):
    def tearDown(self):
        unregister_provider_adapter()

    def test_register_provider_adapter_replaces_existing_type(self):
        register_provider_adapter()
        register_provider_adapter()

        self.assertIn(PROVIDER_TYPE, provider_cls_map)
        self.assertEqual(
            provider_cls_map[PROVIDER_TYPE].provider_display_name,
            "OAuth_plug OpenAI Codex OAuth",
        )

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
