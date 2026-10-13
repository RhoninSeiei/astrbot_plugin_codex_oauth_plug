import json
import unittest
from pathlib import Path

from oauth_plug_openai_codex.registration import DEFAULT_PROVIDER_CONFIG


class SchemaTests(unittest.TestCase):
    def test_oauth_schema_hides_transient_authorization_fields(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        oauth_items = schema["oauth"]["items"]

        for hidden_key in (
            "authorization_input",
            "last_authorize_url",
            "last_state",
            "oauth_account_email",
        ):
            self.assertNotIn(hidden_key, oauth_items)

    def test_default_provider_source_id_does_not_include_model(self):
        self.assertEqual(DEFAULT_PROVIDER_CONFIG["id"], "oauth_plug_openai_codex")

    def test_schema_lists_gpt_6_models_without_changing_default(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        models = schema["runtime"]["items"]["models"]["default"].splitlines()

        self.assertEqual(
            models,
            [
                "gpt-5.6-sol",
                "gpt-6.1-sol",
                "gpt-6-astra",
                "gpt-6-sol",
                "gpt-6-luna",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
            ],
        )
