import unittest
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from unittest.mock import patch

from oauth_plug_openai_codex.service import PROVIDER_TYPE


@contextmanager
def fake_astrbot_provider_environment():
    provider_registry = []
    provider_cls_map = {}
    config_template = {}

    class ProviderType(Enum):
        CHAT_COMPLETION = "chat_completion"

    @dataclass
    class ProviderMetaData:
        id: str
        model: str | None
        type: str
        desc: str
        provider_type: ProviderType
        cls_type: type
        default_config_tmpl: dict
        provider_display_name: str

    register_module = types.ModuleType("astrbot.core.provider.register")
    register_module.provider_registry = provider_registry
    register_module.provider_cls_map = provider_cls_map

    entities_module = types.ModuleType("astrbot.core.provider.entities")
    entities_module.ProviderMetaData = ProviderMetaData
    entities_module.ProviderType = ProviderType

    default_module = types.ModuleType("astrbot.core.config.default")
    default_module.CONFIG_METADATA_2 = {
        "provider_group": {
            "metadata": {
                "provider": {
                    "config_template": config_template,
                },
            },
        },
    }

    provider_module = types.ModuleType("oauth_plug_openai_codex.provider")
    provider_module.ProviderOAuthPlugOpenAICodex = type(
        "ProviderOAuthPlugOpenAICodex",
        (),
        {},
    )

    fake_modules = {
        "astrbot.core.provider.register": register_module,
        "astrbot.core.provider.entities": entities_module,
        "astrbot.core.config.default": default_module,
        "oauth_plug_openai_codex.provider": provider_module,
    }

    with patch.dict(sys.modules, fake_modules):
        yield config_template


class RegistrationTests(unittest.TestCase):
    def test_provider_type_uses_oauth_plug_prefix(self):
        self.assertTrue(PROVIDER_TYPE.startswith("oauth_plug_"))
        self.assertEqual(PROVIDER_TYPE, "oauth_plug_openai_codex_chat_completion")

    def test_provider_adapter_registration_injects_dashboard_template(self):
        with fake_astrbot_provider_environment() as config_template:
            from oauth_plug_openai_codex.registration import register_provider_adapter

            register_provider_adapter()

        template_name = "OAuth_plug OpenAI Codex OAuth"
        self.assertIn(template_name, config_template)
        self.assertEqual(config_template[template_name]["type"], PROVIDER_TYPE)

    def test_provider_adapter_unregistration_removes_dashboard_template(self):
        with fake_astrbot_provider_environment() as config_template:
            from oauth_plug_openai_codex.registration import (
                register_provider_adapter,
                unregister_provider_adapter,
            )

            register_provider_adapter()
            unregister_provider_adapter()

        self.assertNotIn("OAuth_plug OpenAI Codex OAuth", config_template)
