import base64
import io
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_provider as provider_helpers
from oauth_plug_openai_codex.image_media import decode_result, inspect_gif, reference_image_url


def gif_data(animated=False):
    from PIL import Image

    first = Image.new("RGB", (2, 2), "red")
    second = Image.new("RGB", (2, 2), "blue")
    output = io.BytesIO()
    first.save(output, format="GIF", save_all=animated,
               append_images=[second] if animated else [], duration=100, loop=0)
    return output.getvalue()


def raster_data(image_type):
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format=image_type)
    return output.getvalue()


class NovemberProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self):
        provider = provider_helpers.ProviderImageGenerationTests()._make_provider("unused")
        provider._record_provider_stat = AsyncMock()
        return provider

    async def test_manual_catalog_and_closed_provider_do_not_discover(self):
        provider = self.provider()
        provider.provider_config["oauth_auto_models"] = False
        with patch("oauth_plug_openai_codex.model_catalog.discover_models", AsyncMock()) as discover:
            self.assertEqual(await provider.get_models(), ["gpt-5.6-sol"])
            provider._oauth_media_closed = True
            with self.assertRaises(RuntimeError):
                await provider.get_models()
        discover.assert_not_awaited()

    async def test_requested_output_format_mismatch_does_not_write_file(self):
        provider = self.provider()
        data = raster_data("PNG")
        response = {"output": [{"type": "image_generation_call", "result": base64.b64encode(data).decode()}]}
        with patch.object(Path, "mkdir"), patch.object(Path, "write_bytes") as write:
            with self.assertRaises(ValueError):
                await provider._extract_generated_images(response, expected_format="jpeg")
        write.assert_not_called()

    async def test_jpeg_and_webp_requests_preserve_actual_format(self):
        provider = self.provider()
        for extension in ("jpeg", "webp"):
            data = raster_data(extension.upper())
            provider._request_image_backend = AsyncMock(return_value={"output": [
                {"type": "image_generation_call", "result": base64.b64encode(data).decode()}
            ]})
            with patch.object(Path, "mkdir"), patch.object(Path, "write_bytes") as write:
                result = (await provider.generate_image("cat", output_format=extension))[0]
            self.assertEqual(provider._request_image_backend.call_args.args[0]["tools"][0]["output_format"], extension)
            self.assertEqual(result.mime_type, "image/" + extension)
            self.assertTrue(result.path.endswith("." + extension))
            self.assertEqual(write.call_args.args[0], data)

    async def test_real_core_context_does_not_gain_empty_user_message(self):
        from astrbot.core.provider.sources import openai_source

        if not hasattr(openai_source, "TextPart"):
            self.skipTest("Requires real AstrBot core")
        provider = self.provider()
        contexts = [{"role": "user", "content": "actual user text"}]
        extra = [openai_source.TextPart(text="user identity and time")]
        payload, _ = await provider._prepare_chat_payload(None, contexts=contexts, extra_user_content_parts=extra)
        self.assertEqual(payload["messages"], contexts)
        fresh, _ = await provider._prepare_chat_payload(None, contexts=[], extra_user_content_parts=extra)
        self.assertEqual(len(fresh["messages"]), 1)
        self.assertIn("user identity and time", str(fresh["messages"][0]))

    async def test_explicit_image_models_and_default_26(self):
        provider = self.provider()
        provider._request_image_backend = AsyncMock(return_value={"output": []})
        provider._extract_generated_images = AsyncMock(return_value=[])
        for model in (None, "gpt-image-2.6-sunburst", "gpt-image-2.6-flare",
                      "gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-image-2"):
            await provider.generate_image("cat", image_model=model)
            payload = provider._request_image_backend.call_args.args[0]
            self.assertEqual(payload["tools"][0]["model"], model or "gpt-image-2.6-sunburst")
            self.assertEqual(payload["model"], "gpt-5.6-sol")
        await provider.generate_image("cat", image_model="")
        self.assertNotIn("model", provider._request_image_backend.call_args.args[0]["tools"][0])

    async def test_gif_request_is_explicit_and_rejects_edit_before_network(self):
        provider = self.provider()
        provider._request_image_backend = AsyncMock(return_value={"output": []})
        provider._extract_generated_images = AsyncMock(return_value=[])
        for kwargs in ({}, {"reference_images": ["https://example.com/a.png"]},
                       {"action": "edit"}, {"image_model": "gpt-image-2"}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "GIF"):
                await provider.generate_image("wave", output_format="gif", **kwargs)
        provider._request_image_backend.assert_not_awaited()

    async def test_gif_result_keeps_bytes_extension_and_frame_count(self):
        provider = self.provider()
        data = gif_data(animated=True)
        response = {"output": [{"type": "image_generation_call", "result": base64.b64encode(data).decode()}]}
        with patch.object(Path, "mkdir"), patch.object(Path, "write_bytes") as write:
            result = (await provider._extract_generated_images(response))[0]
        self.assertEqual(write.call_args.args[0], data)
        self.assertTrue(result.path.endswith(".gif"))
        self.assertEqual(result.mime_type, "image/gif")
        self.assertEqual(result.frame_count, 2)
        self.assertTrue(result.is_animated)

    async def test_request_response_format_reaches_payload_builder(self):
        provider = self.provider()
        with patch("oauth_plug_openai_codex.openai_oauth_audio.OpenAIOAuthAudioMixin._prepare_chat_payload",
                   AsyncMock(return_value=({"messages": []}, []))):
            payload, _ = await provider._prepare_chat_payload("hello", response_format={"type": "json_object"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_terra_reasoning_and_backend_error_logging(self):
        provider = self.provider()
        self.assertIn("gpt-6.1-terra", provider.model_capabilities)
        for effort in ("none", "low", "medium", "high", "xhigh", "max"):
            request = provider._build_responses_params({"messages": [], "model": "gpt-6.1-terra", "reasoning_effort": effort}, None)
            self.assertEqual(request["reasoning"]["effort"], effort)
        secret = "secret-prompt-token"
        with patch("oauth_plug_openai_codex.provider.logger.warning") as warning:
            with self.assertRaises(Exception):
                provider._parse_backend_response('{"type":"error","error":{"code":"rate_limit_exceeded","message":"' + secret + '"}}')
        self.assertTrue(warning.called)
        self.assertNotIn(secret, str(warning.call_args_list))
        self.assertIn("rate_limit_exceeded", str(warning.call_args_list))
        with patch("oauth_plug_openai_codex.provider.logger.warning") as warning:
            with self.assertRaises(Exception):
                provider._apply_stream_event({"type": "response.failed", "error": {"message": secret}}, [], [], {})
        self.assertTrue(warning.called)
        self.assertNotIn(secret, str(warning.call_args_list))


class GifMediaTests(unittest.TestCase):
    def test_static_and_animated_reference_policies(self):
        for animated in (False, True):
            value = "data:image/gif;base64," + base64.b64encode(gif_data(animated)).decode()
            if animated:
                with self.assertRaises(ValueError):
                    reference_image_url(value)
            else:
                self.assertTrue(reference_image_url(value).startswith("data:image/png;base64,"))
            self.assertTrue(reference_image_url(value, gif_input_mode="first_frame").startswith("data:image/png;base64,"))
        with self.assertRaises(ValueError):
            reference_image_url("https://example.com/animation.gif?download=1")

    def test_result_format_is_detected_from_bytes_not_mime_label(self):
        gif = gif_data(True)
        data, extension, mime, frames = decode_result("data:image/png;base64," + base64.b64encode(gif).decode())
        self.assertEqual((data, extension, mime, frames), (gif, "gif", "image/gif", 2))
        for bad in ("not-base64", base64.b64encode(b"not an image").decode()):
            with self.assertRaises(ValueError):
                decode_result(bad)

    def test_truncated_images_and_byte_limit_are_rejected(self):
        for data in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFFxxxxWEBP"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode_result(base64.b64encode(data).decode())
        with patch("oauth_plug_openai_codex.image_media.MAX_RESULT_BYTES", 4):
            with self.assertRaises(ValueError):
                decode_result(base64.b64encode(raster_data("PNG")).decode())

    def test_gif_frame_budget_is_enforced(self):
        from PIL import Image

        frames = [Image.new("RGB", (2, 2), "red" if i % 2 else "blue") for i in range(257)]
        output = io.BytesIO()
        frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=100)
        with self.assertRaises(ValueError):
            inspect_gif(output.getvalue())
