import unittest

from oauth_plug_openai_codex.headers import build_codex_backend_headers
from oauth_plug_openai_codex.service import OpenAICodexOAuthService


class OctoberConfigurationTests(unittest.TestCase):
    def test_new_model_is_available_without_overwriting_existing_selection(self):
        service = OpenAICodexOAuthService({})
        self.assertIn("gpt-6.1-sol", service.get_models())
        self.assertEqual(service.get_default_model(), "gpt-5.6-sol")
        configured = OpenAICodexOAuthService({"runtime": {"models": "my-model\ngpt-6-sol"}})
        self.assertEqual(configured.get_models(), ["my-model", "gpt-6-sol"])

    def test_headers_identify_the_supported_client_version(self):
        headers = build_codex_backend_headers("test-access", "test-account")
        self.assertEqual(headers["version"], "0.159.2")
        self.assertEqual(headers["User-Agent"], "codex_cli_rs/0.159.2")

    def test_image_model_setting_is_inherited_and_can_be_overridden(self):
        service = OpenAICodexOAuthService({"advanced": {"oauth_image_model": "gpt-image-2.5-flare"}})
        self.assertEqual(service.build_provider_config({})["oauth_image_model"], "gpt-image-2.5-flare")
        self.assertEqual(service.build_provider_config({"oauth_image_model": ""})["oauth_image_model"], "")
        self.assertEqual(service.build_provider_config({"oauth_image_model": "gpt-image-2"})["oauth_image_model"], "gpt-image-2")
        self.assertEqual(OpenAICodexOAuthService({}).build_provider_config({})["oauth_image_model"], "")
