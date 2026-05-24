import asyncio
import base64
import importlib
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


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

    message_result_module = types.ModuleType("astrbot.core.message.message_event_result")

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

    openai_source_module = types.ModuleType("astrbot.core.provider.sources.openai_source")

    class ProviderOpenAIOfficial:
        def get_model(self):
            return getattr(self, "provider_config", {}).get("model", "")

    openai_source_module.ProviderOpenAIOfficial = ProviderOpenAIOfficial

    for name in [
        "astrbot.core",
        "astrbot.core.message",
        "astrbot.core.provider",
        "astrbot.core.provider.sources",
        "astrbot.core.utils",
    ]:
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["astrbot"] = astrbot_module
    sys.modules[
        "astrbot.core.message.message_event_result"
    ] = message_result_module
    sys.modules["astrbot.core.provider.entities"] = entities_module
    sys.modules["astrbot.core.utils.astrbot_path"] = astrbot_path_module
    sys.modules[
        "astrbot.core.provider.sources.openai_source"
    ] = openai_source_module


_install_fake_astrbot_runtime()

from oauth_plug_openai_codex.provider import ProviderOAuthPlugOpenAICodex


class ProviderImageGenerationTests(unittest.TestCase):
    def _make_provider(self, generated_image_dir: str):
        provider = ProviderOAuthPlugOpenAICodex.__new__(ProviderOAuthPlugOpenAICodex)
        provider.provider_config = {
            "model": "gpt-5.5",
            "generated_image_dir": generated_image_dir,
        }
        provider.model_name = "gpt-5.5"
        return provider

    def test_generate_image_with_reference_file_builds_image_edit_payload(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_image_bytes = b"\x89PNG\r\n\x1a\nreference"
            output_image_bytes = b"\x89PNG\r\n\x1a\noutput"
            source_path = tmp_path / "reference.png"
            source_path.write_bytes(source_image_bytes)
            requested_payloads = []
            provider = self._make_provider(str(tmp_path / "generated"))

            async def fake_request_backend(payload):
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

            provider._request_backend = fake_request_backend

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

            async def fake_request_backend(payload):
                requested_payloads.append(payload)
                return {
                    "output": [
                        {
                            "type": "image_generation_call",
                            "result": base64.b64encode(output_image_bytes).decode(),
                        }
                    ]
                }

            provider._request_backend = fake_request_backend

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


if __name__ == "__main__":
    unittest.main()
