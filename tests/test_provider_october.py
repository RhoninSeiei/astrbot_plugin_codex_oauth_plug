import asyncio
import inspect
import types
import unittest
from tests import test_provider as provider_test_helpers
from unittest.mock import AsyncMock, patch

import oauth_plug_openai_codex.provider as provider_module


class OctoberProviderParityTests(unittest.TestCase):
    def _provider(self):
        return provider_test_helpers.ProviderImageGenerationTests()._make_provider(
            "unused"
        )

    def test_gpt_6_1_sol_capability_and_request_filtering(self):
        provider = self._provider()
        capability = provider.model_capabilities["gpt-6.1-sol"]
        self.assertEqual(capability["default_reasoning_effort"], "medium")
        self.assertEqual(
            capability["supported_reasoning_efforts"],
            ("low", "medium", "high", "xhigh", "max"),
        )
        provider.provider_config["custom_extra_body"] = {
            "top_p": 0.7,
            "top_logprobs": 4,
            "include": [
                "message.output_text.logprobs",
                "reasoning.encrypted_content",
            ],
        }

        params = provider._build_responses_params(
            {
                "model": "gpt-6.1-sol-2026-10-13",
                "messages": [],
                "reasoning_effort": "high",
            },
            None,
        )

        self.assertNotIn("top_p", params)
        self.assertNotIn("top_logprobs", params)
        self.assertEqual(params["include"], ["reasoning.encrypted_content"])
        with self.assertRaisesRegex(ValueError, "none"):
            provider._build_responses_params(
                {
                    "model": "gpt-6.1-sol",
                    "messages": [],
                    "reasoning_effort": "none",
                },
                None,
            )

    def test_usage_unknown_partial_and_legacy_token_usage(self):
        provider = self._provider()
        self.assertIsNone(provider._extract_response_usage({}))

        class ModernTokenUsage:
            def __init__(
                self,
                input_other=0,
                input_cached=0,
                output=0,
                is_partial=False,
            ):
                self.input_other = input_other
                self.input_cached = input_cached
                self.output = output
                self.is_partial = is_partial

        with patch.object(provider_module, "TokenUsage", ModernTokenUsage):
            partial = provider._extract_response_usage({"input_tokens": 9})
        self.assertEqual(partial.input_other, 9)
        self.assertEqual(partial.output, 0)
        self.assertTrue(partial.is_partial)

        class LegacyTokenUsage:
            __slots__ = ("input_other", "input_cached", "output")

            def __init__(self, input_other=0, input_cached=0, output=0):
                self.input_other = input_other
                self.input_cached = input_cached
                self.output = output

        with patch.object(provider_module, "TokenUsage", LegacyTokenUsage):
            legacy = provider._extract_response_usage({"output_tokens": 7})

        self.assertEqual(legacy.input_other, 0)
        self.assertEqual(legacy.output, 7)
        self.assertFalse(hasattr(legacy, "is_partial"))

    def test_generate_image_keeps_controller_model_separate_from_image_model(self):
        provider = self._provider()
        provider.provider_config["oauth_image_model"] = "gpt-image-2"
        requested = []
        provider._record_provider_stat = AsyncMock()
        provider._extract_generated_images = AsyncMock(return_value=["image"])

        async def http(payload, _request_timeout):
            requested.append(("http", payload))
            return {"model": "gpt-6.1-sol", "output": []}

        async def websocket(payload, *, timeout):
            requested.append(("websocket", payload, timeout))
            return {"model": "gpt-6.1-sol", "output": []}

        provider._request_image_backend = http
        provider._request_image_backend_websocket = websocket

        first = asyncio.run(
            provider.generate_image(
                "cat",
                model="gpt-6.1-sol",
                image_model=None,
            )
        )
        second = asyncio.run(
            provider.generate_image(
                "cat",
                model="gpt-6.1-sol",
                transport="websocket",
                image_model="",
            )
        )
        third = asyncio.run(
            provider.generate_image(
                "cat",
                model="gpt-6.1-sol",
                image_model="gpt-image-2.5-flare",
            )
        )
        fourth = asyncio.run(
            provider.generate_image(
                "edit cat",
                model="gpt-6.1-sol",
                reference_images=["data:image/png;base64,aW1hZ2U="],
                transport="websocket",
                image_model="gpt-image-2.5-sunburst",
            )
        )

        self.assertEqual(first, ["image"])
        self.assertEqual(second, ["image"])
        self.assertEqual(third, ["image"])
        self.assertEqual(fourth, ["image"])
        self.assertEqual(requested[0][1]["model"], "gpt-6.1-sol")
        self.assertEqual(requested[0][1]["tools"][0]["model"], "gpt-image-2")
        self.assertNotIn("model", requested[1][1]["tools"][0])
        self.assertEqual(
            requested[2][1]["tools"][0]["model"],
            "gpt-image-2.5-flare",
        )
        self.assertEqual(requested[3][0], "websocket")
        self.assertEqual(requested[3][1]["model"], "gpt-6.1-sol")
        self.assertEqual(requested[3][1]["tools"][0]["model"], "gpt-image-2.5-sunburst")
        self.assertEqual(requested[3][1]["tools"][0]["action"], "edit")
        signature = inspect.signature(provider.generate_image)
        self.assertEqual(
            signature.parameters["image_model"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )

    def test_invalid_image_model_fails_before_transport(self):
        provider = self._provider()
        provider._request_image_backend = AsyncMock()

        for image_model in ("gpt-image-1", " gpt-image-2 ", 123):
            with self.subTest(image_model=image_model):
                with self.assertRaisesRegex(ValueError, "image model|图片模型"):
                    asyncio.run(
                        provider.generate_image("cat", image_model=image_model)
                    )

        provider._request_image_backend.assert_not_awaited()

    def test_new_usage_recorder_delegates_and_missing_recorder_falls_back(self):
        provider = self._provider()
        provider.provider_config["id"] = "test-oauth-provider"
        provider._usage_recording_enabled = True
        provider._usage_recording_db = object()
        delegated = AsyncMock(return_value=True)
        fallback_calls = []

        async def insert_provider_stat(**kwargs):
            fallback_calls.append(kwargs)

        with (
            patch.object(provider_module, "record_provider_usage", delegated),
            patch.object(
                provider_module,
                "db_helper",
                types.SimpleNamespace(insert_provider_stat=insert_provider_stat),
            ),
        ):
            asyncio.run(
                provider._record_provider_stat(
                    request_kind="image",
                    status="completed",
                    usage=None,
                    start_time=1,
                    end_time=2,
                    model="gpt-6.1-sol",
                )
            )

        delegated.assert_awaited_once()
        self.assertEqual(fallback_calls, [])

        delegated.reset_mock()
        delegated.return_value = False
        with (
            patch.object(provider_module, "record_provider_usage", delegated),
            patch.object(
                provider_module,
                "db_helper",
                types.SimpleNamespace(insert_provider_stat=insert_provider_stat),
            ),
        ):
            asyncio.run(
                provider._record_provider_stat(
                    request_kind="image",
                    status="completed",
                    usage=None,
                    start_time=3,
                    end_time=4,
                    model="gpt-6.1-sol",
                )
            )

        delegated.assert_awaited_once()
        self.assertEqual(len(fallback_calls), 1)

    def test_image_usage_records_per_call_when_managed_and_once_when_legacy(self):
        async def run(managed):
            provider = self._provider()
            provider._usage_recording_enabled = managed
            provider._record_provider_stat = AsyncMock()
            provider._extract_generated_images = AsyncMock(return_value=["image"])
            responses = iter(
                [
                    {
                        "model": "gpt-image-2",
                        "usage": {"input_tokens": 2, "output_tokens": 3},
                    },
                    {
                        "model": "gpt-image-2",
                        "usage": {"input_tokens": 5, "output_tokens": 7},
                    },
                ]
            )

            async def request(_payload, _request_timeout):
                return next(responses)

            provider._request_image_backend = request
            await provider.generate_image("cat", n=2)
            return provider._record_provider_stat

        managed = asyncio.run(run(True))
        legacy = asyncio.run(run(False))

        self.assertEqual(managed.await_count, 2)
        self.assertEqual(
            [call.kwargs["status"] for call in managed.await_args_list],
            ["completed", "completed"],
        )
        self.assertEqual(
            [call.kwargs["usage"].output for call in managed.await_args_list],
            [3, 7],
        )
        legacy.assert_awaited_once()
        self.assertEqual(legacy.await_args.kwargs["status"], "completed")
        self.assertEqual(legacy.await_args.kwargs["usage"].output, 10)

    def test_managed_image_error_records_each_attempt_without_aggregate(self):
        provider = self._provider()
        provider._usage_recording_enabled = True
        provider._record_provider_stat = AsyncMock()
        provider._extract_generated_images = AsyncMock(return_value=["image"])
        attempts = 0

        async def request(_payload, _request_timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return {
                    "model": "gpt-image-2.5-sunburst",
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                }
            raise RuntimeError("second image failed")

        provider._request_image_backend = request

        with self.assertRaisesRegex(RuntimeError, "second image failed"):
            asyncio.run(provider.generate_image("cat", n=2))

        self.assertEqual(provider._record_provider_stat.await_count, 2)
        self.assertEqual(
            [
                call.kwargs["status"]
                for call in provider._record_provider_stat.await_args_list
            ],
            ["completed", "error"],
        )
        self.assertEqual(
            provider._record_provider_stat.await_args_list[0].kwargs["model"],
            "gpt-image-2.5-sunburst",
        )
        self.assertIsNone(
            provider._record_provider_stat.await_args_list[1].kwargs["usage"]
        )


if __name__ == "__main__":
    unittest.main()
