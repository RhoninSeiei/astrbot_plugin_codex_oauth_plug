"""Official rust-v0.171.0 catalog parity without assuming account availability."""
import unittest
from unittest.mock import AsyncMock
import test_provider as helpers
from oauth_plug_openai_codex.context_budget import resolve_context_budget
from oauth_plug_openai_codex.headers import CODEX_CLIENT_VERSION
from oauth_plug_openai_codex.service import DEFAULT_MODELS


class StableCodexModelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_models_and_windows(self):
        for model, window in (("gpt-6.2-sol", 490000), ("gpt-6.2-astra", 272000), ("gpt-6.1-sol", 490000), ("gpt-6.1-terra", 490000)):
            with self.subTest(model=model):
                provider = helpers.ProviderImageGenerationTests()._make_provider("unused")
                provider.get_model_context_metadata = AsyncMock(return_value={})
                self.assertIn(model, DEFAULT_MODELS)
                budget = await resolve_context_budget(provider, model=model)
                self.assertEqual(budget.context_window, window)
                for name in (model, model + "-2026-11-17"):
                    params = provider._build_responses_params({"model": name, "messages": [], "reasoning_effort": "max"}, None)
                    self.assertEqual(params["reasoning"]["effort"], "max")
                    for effort in ("none", "off"):
                        payload = {"model": name, "messages": [], "reasoning_effort": effort}
                        if model == "gpt-6.1-terra":
                            params = provider._build_responses_params(payload, None)
                            self.assertEqual(params["reasoning"]["effort"], "none")
                        else:
                            with self.assertRaisesRegex(ValueError, "none"):
                                provider._build_responses_params(payload, None)
                    with self.assertRaisesRegex(ValueError, "ultra"):
                        provider._build_responses_params({"model": name, "messages": [], "reasoning_effort": "ultra"}, None)
                provider.provider_config["custom_extra_body"] = {"top_p": .5, "top_logprobs": 2, "include": ["message.output_text.logprobs", "reasoning.encrypted_content"]}
                params = provider._build_responses_params({"model": model, "messages": []}, None)
                self.assertNotIn("top_p", params)
                self.assertNotIn("top_logprobs", params)
                self.assertEqual(params["include"], ["reasoning.encrypted_content"])

    def test_client_marker_matches_stable_release(self):
        self.assertEqual(CODEX_CLIENT_VERSION, "0.171.0")
