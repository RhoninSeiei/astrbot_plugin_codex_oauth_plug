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

    def test_schema_defaults_to_all_gpt_5_6_models(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        models = schema["runtime"]["items"]["models"]["default"].splitlines()

        self.assertEqual(
            models,
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        )

    def test_release_metadata_and_changelog_cover_gpt_5_6_release(self):
        repo_root = Path(__file__).resolve().parents[1]
        metadata = (repo_root / "metadata.yaml").read_text(encoding="utf-8")
        changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("version: v0.1.4", metadata)
        self.assertIn('astrbot_version: ">=4.24.0"', metadata)
        self.assertIn("## 未发布", changelog)
        self.assertIn("GPT-5.6", changelog)
        self.assertIn("reasoning_effort", changelog)
        self.assertIn("version=0.144.0", changelog)
        self.assertIn("SSE", changelog)
