import asyncio
import copy
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from oauth_plug_openai_codex import context_budget as budget_module


def provider(model="gpt-6.1-sol", config=None, metadata=None):
    return SimpleNamespace(
        provider_config={} if config is None else config,
        get_model=lambda: model,
        get_model_context_metadata=AsyncMock(return_value=metadata or {}),
    )


class ContextBudgetResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_window_respects_catalog_limits_and_conservative_fallbacks(self):
        for model, metadata, expected in (
            ("gpt-6.1-sol", {}, 490000),
            ("gpt-6.1-terra", {}, 490000),
            ("gpt-6-astra", {}, 272000),
            ("gpt-6-luna", {}, 272000),
            ("unknown-model", {}, 128000),
            ("gpt-6.1-sol", {"context_window": 256000}, 256000),
            ("gpt-6.1-sol", {"context_window": 500000, "max_context_window": 1000000}, 490000),
            ("gpt-6.1-sol", {"max_context_window": 64000}, 64000),
        ):
            with self.subTest(model=model, metadata=metadata):
                instance = provider(model, metadata=metadata)
                budget = await budget_module.resolve_context_budget(instance)
                self.assertEqual(budget.context_window, expected)
                self.assertGreater(budget.input_budget, 0)
                self.assertLess(budget.input_budget, expected)
                self.assertTrue(budget.enforce_input_limit)

    async def test_manual_positive_window_is_capped_by_account_maximum(self):
        instance = provider(config={"max_context_tokens": 900000}, metadata={
            "context_window": 500000, "max_context_window": 1000000,
        })
        self.assertEqual((await budget_module.resolve_context_budget(instance)).context_window, 900000)
        instance.provider_config["max_context_tokens"] = 2000000
        self.assertEqual((await budget_module.resolve_context_budget(instance)).context_window, 1000000)
        instance.get_model_context_metadata.return_value = {}
        self.assertEqual((await budget_module.resolve_context_budget(instance)).context_window, 500000)

    async def test_original_config_snapshot_preserves_auto_and_output_reserve(self):
        instance = provider(config={"max_context_tokens": 1000000, "max_output_tokens": 900000})
        instance._oauth_context_config = {
            "max_context_tokens": 0, "custom_extra_body": {"max_output_tokens": 40000},
        }
        original = copy.deepcopy(instance._oauth_context_config)
        budget = await budget_module.resolve_context_budget(instance)
        self.assertEqual(budget.context_window, 490000)
        self.assertEqual(budget.output_reserve, 40000)
        self.assertEqual(instance._oauth_context_config, original)
        self.assertEqual(instance.provider_config["max_context_tokens"], 1000000)

    async def test_effective_model_override_and_unknown_metadata_are_request_local(self):
        instance = provider(config={"max_context_tokens": 0})
        original = copy.deepcopy(instance.provider_config)
        budget = await budget_module.resolve_context_budget(instance, model="unknown-model")
        self.assertEqual(budget.model, "unknown-model")
        self.assertEqual(budget.context_window, 128000)
        instance.get_model_context_metadata.assert_awaited_once_with("unknown-model")
        self.assertEqual(instance.provider_config, original)

    async def test_invalid_manual_and_metadata_values_do_not_expand_windows(self):
        for manual in (None, 0, -1, True, 490000.0, "490000"):
            with self.subTest(manual=manual):
                instance = provider(config={"max_context_tokens": manual}, metadata={
                    "context_window": True, "max_context_window": "10000000",
                })
                self.assertEqual((await budget_module.resolve_context_budget(instance)).context_window, 490000)
        for metadata in (None, [], "bad", {"context_window": 0},
                         {"context_window": 10000001}, {"context_window": 32000.0}):
            with self.subTest(metadata=metadata):
                instance = provider()
                instance.get_model_context_metadata.return_value = metadata
                self.assertEqual((await budget_module.resolve_context_budget(instance)).context_window, 490000)

    async def test_metadata_failure_uses_known_limit_but_cancellation_and_close_propagate(self):
        instance = provider()
        instance.get_model_context_metadata.side_effect = RuntimeError("SECRET")
        self.assertEqual((await budget_module.resolve_context_budget(instance)).context_window, 490000)
        instance.get_model_context_metadata.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await budget_module.resolve_context_budget(instance)
        instance._oauth_media_closed = True
        with self.assertRaises(RuntimeError):
            await budget_module.resolve_context_budget(instance)

    async def test_output_reservation_and_compression_trigger_are_finite(self):
        instance = provider(config={"max_context_tokens": 100000, "max_tokens": 10000,
                                    "custom_extra_body": {"max_completion_tokens": 20000}})
        budget = await budget_module.resolve_context_budget(instance)
        self.assertEqual(budget.context_window, 100000)
        self.assertEqual(budget.output_reserve, 20000)
        self.assertEqual(budget.input_budget, 79000)
        self.assertEqual(budget.compression_trigger_tokens, 79000)
        instance.provider_config["max_tokens"] = 100000
        with self.assertRaises(budget_module.ContextBudgetExceeded):
            await budget_module.resolve_context_budget(instance)

    async def test_missing_or_corrupt_local_tokenizer_never_uses_remote_factory(self):
        fake = SimpleNamespace(get_encoding=AsyncMock(side_effect=AssertionError("Network factory prohibited")))
        with patch.dict("os.environ", {"TIKTOKEN_CACHE_DIR": "/offline-budget-fixture"}), patch.dict("sys.modules", {"tiktoken": fake}):
            with patch.object(budget_module.Path, "stat", side_effect=FileNotFoundError):
                await budget_module.resolve_context_budget(provider())
                missing = budget_module.estimate_text_tokens("中文 content")
            with patch.object(budget_module.Path, "stat", return_value=SimpleNamespace(st_mtime_ns=1, st_size=13)), patch.object(budget_module.Path, "read_bytes", return_value=b"corrupt-cache"):
                await budget_module.resolve_context_budget(provider())
                self.assertEqual(budget_module.estimate_text_tokens("中文 content"), missing)
                fake.get_encoding.assert_not_awaited()

    async def test_verified_local_tokenizer_uses_only_local_constructor(self):
        data = b"YQ== 0\n"
        encoding = SimpleNamespace(encode=lambda text, **kwargs: list(text.encode()))
        fake = SimpleNamespace(
            Encoding=Mock(return_value=encoding),
            get_encoding=Mock(side_effect=AssertionError("Remote factory prohibited")),
            encoding_for_model=Mock(side_effect=AssertionError("Remote factory prohibited")),
        )
        previous = budget_module._active_tokenizer.get()
        try:
            with (
                patch.dict("os.environ", {"TIKTOKEN_CACHE_DIR": "/verified-offline-fixture"}),
                patch.dict("sys.modules", {"tiktoken": fake}),
                patch.object(budget_module, "_TOKENIZER_SHA256", hashlib.sha256(data).hexdigest()),
                patch.object(budget_module.Path, "stat", return_value=SimpleNamespace(st_mtime_ns=2, st_size=len(data))),
                patch.object(budget_module.Path, "read_bytes", return_value=data),
            ):
                await budget_module.resolve_context_budget(provider())
                self.assertEqual(budget_module.estimate_text_tokens("hello 中文"), len("hello 中文".encode()))
                fake.Encoding.assert_called_once()
                self.assertEqual(fake.Encoding.call_args.kwargs["mergeable_ranks"], {b"a": 0})
                fake.get_encoding.assert_not_called()
                fake.encoding_for_model.assert_not_called()
        finally:
            budget_module._active_tokenizer.set(previous)


class ContextBudgetGuardTests(unittest.TestCase):
    def budget_for(self, request):
        size = budget_module.estimate_context_tokens(request)
        return budget_module.ContextBudget("test", size + 100, size, 0.9, 0)

    def test_multimodal_binary_is_not_counted_as_literal_text(self):
        short = {"input": [{"type": "input_image", "image_url": "data:image/png;base64,small"},
                           {"type": "input_audio", "input_audio": {"data": "small", "format": "wav"}}]}
        large = copy.deepcopy(short)
        large["input"][0]["image_url"] += "a" * 100000
        large["input"][1]["input_audio"]["data"] += "a" * 100000
        self.assertEqual(budget_module.estimate_context_tokens(short), budget_module.estimate_context_tokens(large))
        self.assertGreater(budget_module.estimate_context_tokens(short), 16384 + 32768)
        schema = {"tools": [{"description": "input_image", "example": "a" * 10000}]}
        self.assertGreater(budget_module.estimate_context_tokens(schema), 3000)
        self.assertGreater(budget_module.estimate_context_tokens(
            {"type": ["object", "null"], "properties": {"value": {"type": "string"}}}
        ), 0)

    def test_guard_removes_only_old_whole_turns_without_mutating_input(self):
        latest = [{"role": "developer", "content": "keep developer"},
                  {"role": "user", "content": "latest question"}]
        target = {"instructions": "keep system", "tools": [{"name": "weather"}], "input": latest}
        request = copy.deepcopy(target)
        request["input"] = [latest[0], {"role": "user", "content": "old text " * 1000},
                            {"role": "assistant", "content": "old reply " * 1000}, latest[1]]
        original = copy.deepcopy(request)
        result = budget_module.trim_context_to_budget(request, self.budget_for(target))
        self.assertEqual(result, target)
        self.assertEqual(request, original)
        result["input"][-1]["content"] = "changed"
        self.assertEqual(request, original)

    def test_tool_calls_and_results_are_never_split_across_trim_boundary(self):
        latest = {"role": "user", "content": "latest"}
        target = {"input": [latest]}
        request = {"input": [
            {"role": "user", "content": "old " * 1000},
            {"type": "function_call", "call_id": "call-1", "arguments": "{}"},
            {"role": "user", "content": "middle " * 1000},
            {"type": "function_call_output", "call_id": "call-1", "output": "old output"},
            latest,
        ]}
        result = budget_module.trim_context_to_budget(request, self.budget_for(target))
        self.assertEqual(result, target)
        malformed = copy.deepcopy(request)
        malformed["input"].pop(3)
        with self.assertRaises(budget_module.ContextBudgetExceeded):
            budget_module.trim_context_to_budget(malformed, self.budget_for(target))

    def test_system_schema_and_latest_turn_cannot_be_trimmed(self):
        budget = budget_module.ContextBudget("test", 100, 20, 0.9, 0)
        protected = (
            {"instructions": "SECRET_SYSTEM " * 100, "input": [{"role": "user", "content": "last"}]},
            {"tools": [{"description": "schema " * 100}], "input": [{"role": "user", "content": "last"}]},
            {"input": [{"role": "user", "content": "SECRET_LAST " * 100}]},
        )
        for request in protected:
            with self.subTest(request=request):
                original = copy.deepcopy(request)
                with self.assertRaises(budget_module.ContextBudgetExceeded) as raised:
                    budget_module.trim_context_to_budget(request, budget)
                self.assertNotIn("SECRET", str(raised.exception))
                self.assertEqual(request, original)

    def test_malformed_tool_pair_ids_cannot_be_used_to_trim_history(self):
        latest = {"role": "user", "content": "latest"}
        target = {"input": [latest]}
        for call_id in (None, "", "   ", [], {}, 7, True):
            with self.subTest(call_id=call_id):
                request = {"input": [
                    {"role": "user", "content": "old " * 1000},
                    {"type": "function_call", "call_id": call_id, "arguments": "{}"},
                    {"type": "function_call_output", "call_id": call_id, "output": "done"},
                    latest,
                ]}
                with self.assertRaises(budget_module.ContextBudgetExceeded):
                    budget_module.trim_context_to_budget(request, self.budget_for(target))

    def test_fitting_request_is_independent_and_empty_text_is_zero(self):
        self.assertEqual(budget_module.estimate_text_tokens(""), 0)
        request = {"input": [{"role": "user", "content": "hello 中文 😀"}]}
        copied = budget_module.trim_context_to_budget(request, self.budget_for(request))
        self.assertEqual(copied, request)
        self.assertIsNot(copied["input"], request["input"])


if __name__ == "__main__":
    unittest.main()
