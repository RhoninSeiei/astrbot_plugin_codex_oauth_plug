import json
import sys
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx

import test_provider as provider_helpers
import test_provider_image_websocket as websocket_helpers
from oauth_plug_openai_codex.openai_oauth_sse import iter_json_sse_events, iter_json_sse_text


def frame(event, pretty=False):
    return "".join("data: " + line + "\n" for line in json.dumps(event, indent=2 if pretty else None).split("\n")) + "\n"


class EventStream(httpx.AsyncByteStream):
    def __init__(self, body, forbid_late_read=False):
        self.body = body
        self.forbid_late_read = forbid_late_read
        self.closed = False

    async def __aiter__(self):
        yield self.body.encode()
        if self.forbid_late_read:
            raise AssertionError("read past terminal event")

    async def aclose(self):
        self.closed = True


class ImageSSEFollowupTests(unittest.IsolatedAsyncioTestCase):
    def provider(self):
        provider = provider_helpers.ProviderImageGenerationTests()._make_provider("unused")
        provider._ensure_fresh_oauth_token = AsyncMock()
        provider._record_provider_stat = AsyncMock()
        return provider

    def install_http(self, provider, response_factory):
        @asynccontextmanager
        async def client(**kwargs):
            async with httpx.AsyncClient(transport=httpx.MockTransport(response_factory)) as instance:
                yield instance
        provider._open_oauth_http_client = client

    async def test_multiline_http_terminal_usage_and_cleanup(self):
        for terminal in ("response.completed", "response.failed", "response.incomplete", "response.error", "error"):
            with self.subTest(terminal=terminal):
                provider = self.provider()
                response = {"status": "completed", "output": [], "usage": {"input_tokens": 7, "output_tokens": 2}}
                if terminal != "response.completed":
                    response["error"] = {"code": "rate_limit_exceeded", "status_code": 429}
                stream = EventStream(frame({"type": terminal, "response": response}, pretty=True), True)
                self.install_http(provider, lambda request: httpx.Response(200, stream=stream))
                if terminal == "response.completed":
                    result = await provider._request_image_backend({})
                    self.assertEqual(result["usage"]["input_tokens"], 7)
                else:
                    with self.assertRaises(Exception) as caught:
                        await provider.generate_image("synthetic")
                    self.assertEqual(caught.exception._astrbot_token_usage.output, 2)
                    self.assertEqual(provider._record_provider_stat.call_args.kwargs["usage"].output, 2)
                self.assertTrue(stream.closed)

    async def test_invalid_event_warns_once_and_does_not_expose_body(self):
        provider = self.provider()
        stream = EventStream('data: {"PRIVATE":\ndata: invalid-PRIVATE\n\n')
        self.install_http(provider, lambda request: httpx.Response(200, stream=stream))
        with patch("oauth_plug_openai_codex.provider.logger.warning") as warning:
            with self.assertRaises(ValueError) as caught:
                await provider._request_image_backend({})
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertNotIn("PRIVATE", str(warning.call_args_list))
        warning.assert_called_once()
        self.assertTrue(stream.closed)

    async def test_extraction_failure_keeps_usage_once_for_new_and_old_recorders(self):
        for managed in (False, True):
            with self.subTest(managed=managed):
                provider = self.provider()
                provider._usage_recording_enabled = managed
                provider._request_image_backend = AsyncMock(return_value={
                    "output": [], "usage": {"input_tokens": 7, "output_tokens": 2},
                })
                provider._extract_generated_images = AsyncMock(side_effect=ValueError("invalid image"))
                with self.assertRaises(ValueError):
                    await provider.generate_image("synthetic")
                provider._record_provider_stat.assert_awaited_once()
                recorded = provider._record_provider_stat.call_args.kwargs
                self.assertEqual(recorded["status"], "error")
                self.assertEqual(recorded["usage"].input_other, 7)
                self.assertEqual(recorded["usage"].output, 2)

    async def test_http_error_body_allows_auth_retry_and_plain_json_success(self):
        provider = self.provider()
        provider._refresh_after_auth_failure = AsyncMock(return_value=True)
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401, text="data: invalid-PRIVATE") if len(calls) == 1 else httpx.Response(200, json={"status": "completed", "output": []})
        self.install_http(provider, handler)
        self.assertEqual((await provider._request_image_backend({}))["status"], "completed")
        self.assertEqual(len(calls), 2)
        provider._refresh_after_auth_failure.assert_awaited_once()

    async def test_heartbeats_done_and_eof_keep_complete_events(self):
        event = {"type": "response.completed", "response": {"status": "completed"}}
        for heartbeat in ("data:\n\n", "data:\ndata:\n\n"):
            transcript = heartbeat + frame(event) + "data: [DONE]\n\n"
            async def lines():
                for line in transcript.splitlines():
                    yield line
            self.assertEqual(list(iter_json_sse_text(transcript)), [event])
            self.assertEqual([item async for item in iter_json_sse_events(lines())], [event])
        self.assertEqual(list(iter_json_sse_text(frame(event).rstrip("\n"))), [event])

    async def test_image_frame_above_text_limit_remains_supported(self):
        provider = self.provider()
        result = "x" * (4 * 1024 * 1024 + 1)
        item = {"id": "image", "type": "image_generation_call", "result": result}
        body = frame({"type": "response.output_item.done", "item": item}) + frame({"type": "response.completed", "response": {"status": "completed", "output": []}})
        stream = EventStream(body)
        self.install_http(provider, lambda request: httpx.Response(200, stream=stream))
        self.assertEqual((await provider._request_image_backend({}))["output"][0]["result"], result)

    async def test_pretty_websocket_json_preserves_image_and_usage(self):
        class PrettySocket(websocket_helpers.Socket):
            async def __anext__(self):
                message = await super().__anext__()
                message.data = json.dumps(json.loads(message.data), indent=2)
                return message
        provider = self.provider()
        socket = PrettySocket(websocket_helpers.image_events())
        session = websocket_helpers.Session(socket)
        with patch.dict(sys.modules, {"aiohttp": websocket_helpers.FakeAiohttp([session])}):
            response = await provider._request_image_backend_websocket({}, timeout=10)
        self.assertEqual(response["usage"], {"input_tokens": 1, "output_tokens": 2})
        self.assertEqual(response["output"][0]["type"], "image_generation_call")
        self.assertTrue(socket.closed and session.closed)

    async def test_websocket_failed_event_preserves_usage_without_raw_error(self):
        provider = self.provider()
        socket = websocket_helpers.Socket([{
            "type": "response.failed", "response": {
                "error": {"code": "rate_limit_exceeded", "message": "PRIVATE"},
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        }])
        session = websocket_helpers.Session(socket)
        with patch.dict(sys.modules, {"aiohttp": websocket_helpers.FakeAiohttp([session])}):
            with self.assertRaises(Exception) as caught:
                await provider.generate_image("synthetic", transport="websocket")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(caught.exception._astrbot_token_usage.output, 2)
        self.assertEqual(provider._record_provider_stat.call_args.kwargs["usage"].output, 2)
        self.assertTrue(socket.closed and session.closed)
