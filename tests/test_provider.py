import asyncio
import base64
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def _install_fake_astrbot_runtime():
    if "astrbot.core.provider.sources.openai_source" in sys.modules:
        return
    try:
        importlib.import_module("astrbot.core.provider.sources.openai_source")
        return
    except Exception:
        pass

    astrbot_module = types.ModuleType("astrbot")
    astrbot_module.logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )

    message_result_module = types.ModuleType(
        "astrbot.core.message.message_event_result"
    )

    class MessageChain:
        def __init__(self):
            self.messages = []

        def message(self, text):
            self.messages.append(str(text))
            return self

    message_result_module.MessageChain = MessageChain

    entities_module = types.ModuleType("astrbot.core.provider.entities")

    class LLMResponse:
        def __init__(self, role):
            self.role = role
            self.result_chain = None
            self.reasoning_content = ""
            self.tools_call_args = []
            self.tools_call_name = []
            self.tools_call_ids = []
            self.raw_completion = None
            self.id = ""
            self.usage = None

        @property
        def completion_text(self):
            if self.result_chain and self.result_chain.messages:
                return "".join(self.result_chain.messages)
            return None

    class TokenUsage:
        def __init__(self, input_other=0, input_cached=0, output=0):
            self.input_other = input_other
            self.input_cached = input_cached
            self.output = output

    entities_module.LLMResponse = LLMResponse
    entities_module.TokenUsage = TokenUsage

    astrbot_path_module = types.ModuleType("astrbot.core.utils.astrbot_path")
    astrbot_path_module.get_astrbot_data_path = lambda: "/tmp"

    openai_source_module = types.ModuleType(
        "astrbot.core.provider.sources.openai_source"
    )

    class ProviderOpenAIOfficial:
        def get_model(self):
            return getattr(self, "provider_config", {}).get("model", "")

        async def _prepare_chat_payload(self, *args, **kwargs):
            return {"messages": [], "model": kwargs.get("model", "")}, []

    openai_source_module.ProviderOpenAIOfficial = ProviderOpenAIOfficial

    request_retry_module = types.ModuleType(
        "astrbot.core.provider.sources.request_retry"
    )

    async def retry_provider_request(
        provider_label,
        request_factory,
        *,
        retry_rate_limits=True,
        max_attempts=None,
    ):
        return await request_factory()

    request_retry_module.retry_provider_request = retry_provider_request

    for name in [
        "astrbot.core",
        "astrbot.core.message",
        "astrbot.core.provider",
        "astrbot.core.provider.sources",
        "astrbot.core.utils",
    ]:
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.core.message.message_event_result"] = message_result_module
    sys.modules["astrbot.core.provider.entities"] = entities_module
    sys.modules["astrbot.core.utils.astrbot_path"] = astrbot_path_module
    sys.modules["astrbot.core.provider.sources.openai_source"] = openai_source_module
    sys.modules["astrbot.core.provider.sources.request_retry"] = request_retry_module


_install_fake_astrbot_runtime()

from oauth_plug_openai_codex.provider import ProviderOAuthPlugOpenAICodex  # noqa: E402


def _encode_test_jwt(claims):
    def encode_part(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode_part({'alg': 'none'})}.{encode_part(claims)}.signature"


class ProviderImageGenerationTests(unittest.TestCase):
    def _make_provider(self, generated_image_dir: str):
        provider = ProviderOAuthPlugOpenAICodex.__new__(ProviderOAuthPlugOpenAICodex)
        provider.provider_config = {
            "model": "gpt-5.6-sol",
            "generated_image_dir": generated_image_dir,
            "oauth_access_token": "access-token",
            "oauth_account_id": "account-id",
        }
        provider.model_name = "gpt-5.6-sol"
        provider.client = types.SimpleNamespace(
            base_url=types.SimpleNamespace(host="chatgpt.example")
        )
        provider.account_id = "account-id"
        provider.base_url = "https://chatgpt.example/backend-api/codex"
        provider.timeout = 30
        provider._oauth_refresh_lock = asyncio.Lock()
        return provider

    def _run_query(self, provider, payloads, request_max_retries=None):
        calls = []
        retries = []

        async def fake_request_backend(payload):
            calls.append(payload)
            return {"id": "resp_test", "output_text": "ok", "output": []}

        async def fake_parse(response, tools):
            return response

        async def fake_retry(label, request_factory, *, max_attempts=None, **kwargs):
            retries.append((label, max_attempts))
            return await request_factory()

        provider._request_backend = fake_request_backend
        provider._parse_responses_completion = fake_parse
        with patch(
            "oauth_plug_openai_codex.provider.retry_provider_request",
            fake_retry,
        ):
            result = asyncio.run(
                provider._query(
                    payloads,
                    None,
                    request_max_retries=request_max_retries,
                )
            )
        return result, calls, retries

    def test_provider_advertises_gpt_5_6_models(self):
        capabilities = ProviderOAuthPlugOpenAICodex.model_capabilities

        self.assertEqual(
            list(capabilities)[:3],
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        )
        self.assertEqual(
            capabilities["gpt-5.6-sol"]["default_reasoning_effort"],
            "low",
        )
        self.assertIn(
            "max",
            capabilities["gpt-5.6-terra"]["supported_reasoning_efforts"],
        )

    def test_backend_headers_include_client_version_and_nested_residency(self):
        provider = self._make_provider("/tmp")
        provider.provider_config["oauth_access_token"] = _encode_test_jwt(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_data_residency": "us",
                }
            }
        )
        provider.provider_config["custom_headers"] = {"X-Plugin-Test": "enabled"}

        headers = provider._build_backend_headers()

        self.assertEqual(headers["version"], "0.144.0")
        self.assertEqual(headers["User-Agent"], "codex_cli_rs/0.144.0")
        self.assertEqual(headers["x-openai-internal-codex-residency"], "us")
        self.assertEqual(headers["X-Plugin-Test"], "enabled")

    def test_text_and_image_requests_use_shared_header_builder(self):
        provider = self._make_provider("/tmp")
        requested_headers = []
        requested_timeouts = []
        provider._build_backend_headers = lambda: {"X-Shared-Headers": "yes"}

        class FakePostResponse:
            status_code = 200

            async def aread(self):
                return b'data: {"type":"response.completed","response":{}}'

        class FakeStreamResponse:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def aiter_lines(self):
                yield 'data: {"type":"response.completed","response":{}}'

        class FakeClient:
            def __init__(self, *args, **kwargs):
                requested_timeouts.append(kwargs["timeout"])

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                requested_headers.append(kwargs["headers"])
                return FakePostResponse()

            def stream(self, *args, **kwargs):
                requested_headers.append(kwargs["headers"])
                return FakeStreamResponse()

        async def make_requests():
            await provider._request_backend_once({"stream": True})
            await provider._request_image_backend_once({"stream": True}, 12.5)

        with patch("oauth_plug_openai_codex.provider.httpx.AsyncClient", FakeClient):
            asyncio.run(make_requests())

        self.assertEqual(
            requested_headers,
            [{"X-Shared-Headers": "yes"}, {"X-Shared-Headers": "yes"}],
        )
        self.assertEqual(requested_timeouts, [provider.timeout, 12.5])

    def test_prepare_chat_payload_preserves_request_reasoning_controls(self):
        provider = self._make_provider("/tmp")

        payloads, _ = asyncio.run(
            provider._prepare_chat_payload(
                prompt="hello",
                model="gpt-5.6-sol",
                reasoning_effort="high",
                reasoning={"summary": "auto"},
            )
        )

        self.assertEqual(payloads["reasoning_effort"], "high")
        self.assertEqual(payloads["reasoning"], {"summary": "auto"})

    def test_query_applies_reasoning_priority_and_request_retry_count(self):
        provider = self._make_provider("/tmp")
        provider.provider_config["custom_extra_body"] = {
            "reasoning_effort": "low",
            "reasoning": {"effort": "medium", "summary": "detailed"},
        }

        _, calls, retries = self._run_query(
            provider,
            {
                "model": "gpt-5.6-sol",
                "messages": [],
                "reasoning_effort": "high",
                "reasoning": {"effort": "xhigh", "summary": "auto"},
            },
            request_max_retries=7,
        )

        self.assertEqual(
            calls[0]["reasoning"],
            {"effort": "xhigh", "summary": "auto"},
        )
        self.assertNotIn("reasoning_effort", calls[0])
        self.assertEqual(retries, [("OpenAI OAuth", 7)])

    def test_query_normalizes_off_and_legacy_max_reasoning_efforts(self):
        provider = self._make_provider("/tmp")

        _, off_calls, _ = self._run_query(
            provider,
            {"model": "gpt-5.6-sol", "messages": [], "reasoning_effort": "off"},
        )
        _, max_calls, _ = self._run_query(
            provider,
            {"model": "gpt-5.5", "messages": [], "reasoning_effort": "max"},
        )

        self.assertEqual(off_calls[0]["reasoning"]["effort"], "none")
        self.assertEqual(max_calls[0]["reasoning"]["effort"], "xhigh")

    def test_query_maps_gpt_5_3_codex_max_to_xhigh(self):
        provider = self._make_provider("/tmp")

        _, calls, _ = self._run_query(
            provider,
            {"model": "gpt-5.3-codex", "messages": [], "reasoning_effort": "max"},
        )

        self.assertEqual(calls[0]["reasoning"]["effort"], "xhigh")

    def test_query_keeps_gpt_5_6_max_and_unknown_model_effort(self):
        provider = self._make_provider("/tmp")

        _, max_calls, _ = self._run_query(
            provider,
            {"model": "gpt-5.6-terra", "messages": [], "reasoning_effort": "max"},
        )
        _, unknown_calls, _ = self._run_query(
            provider,
            {"model": "gpt-future", "messages": [], "reasoning_effort": "novel"},
        )

        self.assertEqual(max_calls[0]["reasoning"]["effort"], "max")
        self.assertEqual(unknown_calls[0]["reasoning"]["effort"], "novel")

    def test_query_rejects_ultra_for_single_provider_request(self):
        provider = self._make_provider("/tmp")

        with self.assertRaisesRegex(ValueError, "ultra"):
            self._run_query(
                provider,
                {
                    "model": "gpt-5.6-sol",
                    "messages": [],
                    "reasoning_effort": "ultra",
                },
            )

    def test_generate_image_without_reference_builds_generate_payload(self):
        with TemporaryDirectory() as tmp:
            output_image_bytes = b"\x89PNG\r\n\x1a\noutput"
            requested_payloads = []
            requested_timeouts = []
            provider = self._make_provider(tmp)

            async def fake_request_image_backend(payload, request_timeout):
                requested_payloads.append(payload)
                requested_timeouts.append(request_timeout)
                return {
                    "output": [
                        {
                            "type": "image_generation_call",
                            "result": base64.b64encode(output_image_bytes).decode(),
                            "revised_prompt": "generated",
                        }
                    ]
                }

            provider._request_image_backend = fake_request_image_backend

            results = asyncio.run(
                provider.generate_image(
                    prompt="draw a city under rain",
                    model="gpt-5.4",
                    size="1024x1024",
                )
            )

            payload = requested_payloads[0]
            self.assertEqual(payload["model"], "gpt-5.4")
            self.assertEqual(payload["instructions"], "draw a city under rain")
            self.assertEqual(
                payload["tools"],
                [
                    {
                        "type": "image_generation",
                        "action": "generate",
                        "size": "1024x1024",
                    }
                ],
            )
            self.assertEqual(
                payload["input"],
                [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "draw a city under rain",
                            },
                        ],
                    }
                ],
            )
            self.assertTrue(payload["stream"])
            self.assertEqual(payload["tool_choice"], {"type": "image_generation"})
            self.assertEqual(Path(results[0].path).read_bytes(), output_image_bytes)
            self.assertEqual(requested_timeouts, [provider.timeout])
            self.assertTrue(ProviderOAuthPlugOpenAICodex.capabilities["image_generate"])

    def test_generate_image_with_reference_file_builds_image_edit_payload(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_image_bytes = b"\x89PNG\r\n\x1a\nreference"
            output_image_bytes = b"\x89PNG\r\n\x1a\noutput"
            source_path = tmp_path / "reference.png"
            source_path.write_bytes(source_image_bytes)
            requested_payloads = []
            provider = self._make_provider(str(tmp_path / "generated"))

            async def fake_request_image_backend(payload, request_timeout):
                requested_payloads.append(payload)
                return {
                    "output": [
                        {
                            "type": "image_generation_call",
                            "result": base64.b64encode(output_image_bytes).decode(),
                            "revised_prompt": "revised",
                        }
                    ]
                }

            provider._request_image_backend = fake_request_image_backend

            results = asyncio.run(
                provider.generate_image(
                    prompt="keep the subject and change the background",
                    model="gpt-5.4",
                    size="1024x1024",
                    reference_images=[str(source_path)],
                )
            )

            payload = requested_payloads[0]
            self.assertEqual(
                payload["tools"],
                [
                    {
                        "type": "image_generation",
                        "action": "edit",
                        "size": "1024x1024",
                    }
                ],
            )
            self.assertEqual(
                payload["input"],
                [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "keep the subject and change the background",
                            },
                            {
                                "type": "input_image",
                                "image_url": (
                                    "data:image/png;base64,"
                                    + base64.b64encode(source_image_bytes).decode()
                                ),
                            },
                        ],
                    }
                ],
            )
            self.assertEqual(
                payload["instructions"],
                "keep the subject and change the background",
            )
            self.assertTrue(payload["stream"])
            self.assertEqual(Path(results[0].path).read_bytes(), output_image_bytes)
            self.assertEqual(results[0].revised_prompt, "revised")
            self.assertTrue(ProviderOAuthPlugOpenAICodex.capabilities["image_edit"])

    def test_generate_image_with_data_url_reference_keeps_data_url(self):
        with TemporaryDirectory() as tmp:
            output_image_bytes = b"\x89PNG\r\n\x1a\noutput"
            data_url = "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode()
            requested_payloads = []
            provider = self._make_provider(tmp)

            async def fake_request_image_backend(payload, request_timeout):
                requested_payloads.append(payload)
                return {
                    "output": [
                        {
                            "type": "image_generation_call",
                            "result": base64.b64encode(output_image_bytes).decode(),
                        }
                    ]
                }

            provider._request_image_backend = fake_request_image_backend

            asyncio.run(
                provider.generate_image(
                    prompt="turn this into a watercolor illustration",
                    reference_images=[data_url],
                    action="auto",
                )
            )

            payload = requested_payloads[0]
            self.assertEqual(
                payload["tools"],
                [
                    {
                        "type": "image_generation",
                        "action": "auto",
                    }
                ],
            )
            self.assertEqual(
                payload["input"][0]["content"][1],
                {
                    "type": "input_image",
                    "image_url": data_url,
                },
            )

    def test_generate_image_reads_sse_incrementally(self):
        with TemporaryDirectory() as tmp:
            image_bytes = b"\x89PNG\r\n\x1a\nstreamed"
            image_base64 = base64.b64encode(image_bytes).decode()
            sent_requests = []
            client_timeouts = []
            provider = self._make_provider(tmp)

            class FakeStreamResponse:
                status_code = 200

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def aread(self):
                    raise AssertionError(
                        "image generation should not read the full SSE body"
                    )

                async def aiter_lines(self):
                    yield "event: response.output_item.done"
                    yield (
                        'data: {"type":"response.output_item.done","item":'
                        '{"id":"ig_test","type":"image_generation_call",'
                        f'"result":"{image_base64}",'
                        '"revised_prompt":"streamed prompt"}'
                        ',"output_index":0}'
                    )
                    yield ""
                    yield "event: response.completed"
                    yield (
                        'data: {"type":"response.completed","response":'
                        '{"id":"resp_img","output":[]}}'
                    )
                    yield ""

            class FakeClient:
                def __init__(self, *args, **kwargs):
                    client_timeouts.append(kwargs["timeout"])

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def post(self, *args, **kwargs):
                    raise AssertionError("image generation should use an SSE stream")

                def stream(self, method, url, **kwargs):
                    sent_requests.append(
                        {
                            "method": method,
                            "url": url,
                            **kwargs,
                        }
                    )
                    return FakeStreamResponse()

            with patch(
                "oauth_plug_openai_codex.provider.httpx.AsyncClient", FakeClient
            ):
                results = asyncio.run(
                    provider.generate_image("draw streamed image", timeout="45.5")
                )

            self.assertEqual(sent_requests[0]["method"], "POST")
            self.assertTrue(sent_requests[0]["url"].endswith("/responses"))
            self.assertEqual(
                sent_requests[0]["headers"]["Authorization"],
                "Bearer access-token",
            )
            self.assertEqual(sent_requests[0]["json"]["stream"], True)
            self.assertEqual(
                sent_requests[0]["json"]["tools"],
                [
                    {
                        "type": "image_generation",
                        "action": "generate",
                    }
                ],
            )
            self.assertEqual(results[0].revised_prompt, "streamed prompt")
            self.assertEqual(Path(results[0].path).read_bytes(), image_bytes)
            self.assertEqual(client_timeouts, [45.5])
            self.assertEqual(provider.timeout, 30)

    def test_generate_image_rejects_invalid_timeout_before_backend_request(self):
        provider = self._make_provider("/tmp")
        requests = []

        async def fake_request_image_backend(payload, request_timeout):
            requests.append((payload, request_timeout))
            return {"output": []}

        provider._request_image_backend = fake_request_image_backend

        for timeout in (0, -1, float("nan"), float("inf"), "invalid", object()):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout"):
                    asyncio.run(provider.generate_image("draw image", timeout=timeout))

        self.assertEqual(requests, [])
        self.assertEqual(provider.timeout, 30)

    def test_parse_backend_response_merges_sse_items_and_text_delta(self):
        provider = self._make_provider("/tmp")
        text = """
event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"hello "}

event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"world"}

event: response.output_item.done
data: {"type":"response.output_item.done","item":{"id":"ig_test","type":"image_generation_call","result":"first"}}

event: response.output_item.done
data: {"type":"response.output_item.done","item":{"id":"ig_test","type":"image_generation_call","result":"duplicate"}}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_test","output":[]}}
""".strip()

        response = provider._parse_backend_response(text)

        self.assertEqual(response["output_text"], "hello world")
        self.assertEqual(len(response["output"]), 1)
        self.assertEqual(response["output"][0]["result"], "first")

    def test_request_image_backend_refreshes_token_after_unauthorized_response(self):
        provider = self._make_provider("/tmp")
        calls = []
        refreshes = []

        async def fake_ensure_fresh_oauth_token():
            return None

        async def fake_request_image_backend_once(payload, request_timeout):
            calls.append((payload, request_timeout))
            if len(calls) == 1:
                return 401, 'data: {"type":"response.error","error":"expired"}'
            return 200, (
                'data: {"type":"response.completed","response":'
                '{"id":"resp_test","output":[{"type":"image_generation_call",'
                '"result":"cmVmcmVzaGVk"}]}}'
            )

        async def fake_refresh_oauth_token():
            refreshes.append(True)
            return True

        provider._ensure_fresh_oauth_token = fake_ensure_fresh_oauth_token
        provider._request_image_backend_once = fake_request_image_backend_once
        provider._refresh_oauth_token = fake_refresh_oauth_token

        response = asyncio.run(
            provider._request_image_backend({"stream": True}, 75.25)
        )

        self.assertEqual(
            calls,
            [
                ({"stream": True}, 75.25),
                ({"stream": True}, 75.25),
            ],
        )
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(response["output"][0]["result"], "cmVmcmVzaGVk")

    def test_other_plugin_can_call_generate_image_with_reference_image(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_image_bytes = b"\x89PNG\r\n\x1a\nexternal"
            reference_image_bytes = b"\x89PNG\r\n\x1a\nexternal-reference"
            reference_path = tmp_path / "reference.png"
            reference_path.write_bytes(reference_image_bytes)
            provider = self._make_provider(tmp)
            requested_payloads = []

            async def fake_request_image_backend(payload, request_timeout):
                requested_payloads.append(payload)
                return {
                    "output": [
                        {
                            "type": "image_generation_call",
                            "result": base64.b64encode(output_image_bytes).decode(),
                        }
                    ]
                }

            provider._request_image_backend = fake_request_image_backend

            class FakeContext:
                def get_provider_by_id(self, provider_id):
                    self.requested_provider_id = provider_id
                    return provider

            async def external_plugin_call(context):
                selected = context.get_provider_by_id(
                    "oauth_plug_openai_codex_chat_completion/gpt-5.6-sol"
                )
                self.assertTrue(selected.capabilities["image_generate"])
                self.assertTrue(selected.capabilities["image_edit"])
                return await selected.generate_image(
                    prompt="external plugin image",
                    n=1,
                    reference_images=[str(reference_path)],
                )

            context = FakeContext()
            results = asyncio.run(external_plugin_call(context))

            self.assertEqual(
                context.requested_provider_id,
                "oauth_plug_openai_codex_chat_completion/gpt-5.6-sol",
            )
            self.assertEqual(
                requested_payloads[0]["instructions"], "external plugin image"
            )
            self.assertEqual(requested_payloads[0]["tools"][0]["action"], "edit")
            self.assertEqual(
                requested_payloads[0]["input"][0]["content"][1]["image_url"],
                "data:image/png;base64,"
                + base64.b64encode(reference_image_bytes).decode(),
            )
            self.assertEqual(Path(results[0].path).read_bytes(), output_image_bytes)


if __name__ == "__main__":
    unittest.main()
