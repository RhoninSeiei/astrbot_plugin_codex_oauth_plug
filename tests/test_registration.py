import unittest

from oauth_plug_openai_codex.service import PROVIDER_TYPE


class RegistrationTests(unittest.TestCase):
    def test_provider_type_uses_oauth_plug_prefix(self):
        self.assertTrue(PROVIDER_TYPE.startswith("oauth_plug_"))
        self.assertEqual(PROVIDER_TYPE, "oauth_plug_openai_codex_chat_completion")
