"""Local transport tests for optional Codex image WebSocket requests."""

import asyncio
import base64
import json
import sys
import types
import unittest
from unittest.mock import patch

from tests import test_provider as provider_test_helpers

from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from oauth_plug_openai_codex.provider import ProviderOAuthPlugOpenAICodex


IMAGE = b"\x89PNG\r\n\x1a\nimage"


def image_events():
    return [
        {"type": "response.created", "response": {"id": "response-test"}},
        {
            "type": "response.output_item.done",
            "item": {
                "id": "image-test",
                "type": "image_generation_call",
                "result": base64.b64encode(IMAGE).decode(),
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "response-test",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        },
    ]


class Socket:
    def __init__(self, events=(), *, send_error=None, wait_forever=False):
        self.events = list(events)
        self.send_error = send_error
        self.wait_forever = wait_forever
        self.sent = []
        self.closed = False
        self.reading = asyncio.Event()
        self.wake = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        self.closed = True
        self.wake.set()

    async def send_json(self, value):
        self.sent.append(value)
        if self.send_error:
            raise self.send_error

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.reading.set()
        if self.closed:
            raise StopAsyncIteration
        if self.events:
            return types.SimpleNamespace(
                type="text", data=json.dumps(self.events.pop(0))
            )
        if self.wait_forever:
            await self.wake.wait()
        raise StopAsyncIteration


class HandshakeError(Exception):
    def __init__(self, status):
        self.status = status


class Handshake:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.handshaking.set()
        if self.session.gate:
            await self.session.gate.wait()
        if self.session.error:
            raise self.session.error
        return self.session.socket

    async def __aexit__(self, *_):
        await self.session.socket.close()


class Session:
    def __init__(self, socket, *, error=None, gate=None):
        self.socket = socket
        self.error = error
        self.gate = gate
        self.closed = False
        self.handshaking = asyncio.Event()
        self.connect_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        self.closed = True
        await self.socket.close()

    def ws_connect(self, url, **kwargs):
        self.connect_calls.append((url, kwargs))
        return Handshake(self)


class FakeAiohttp(types.ModuleType):
    class ClientError(Exception):
        pass

    class TraceConfig:
        def __init__(self):
            self.on_request_redirect = []

    class ClientTimeout:
        def __init__(self, *, total):
            self.total = total

    WSMsgType = types.SimpleNamespace(TEXT="text")
    WSServerHandshakeError = HandshakeError

    def __init__(self, sessions):
        super().__init__("aiohttp")
        self.sessions = list(sessions)
        self.created = []

    def ClientSession(self, **kwargs):
        self.created.append(kwargs)
        return self.sessions.pop(0)


class ImageWebSocketTests(unittest.IsolatedAsyncioTestCase):
    def make_provider(self):
        provider = provider_test_helpers.ProviderImageGenerationTests()._make_provider(
            "unused"
        )
        provider._ensure_fresh_oauth_token = self.noop
        provider._build_backend_headers_with_version = lambda: (
            {"Authorization": "Bearer test-token"},
            0,
        )
        provider._record_provider_stat = self.noop
        return provider

    async def noop(self, *args, **kwargs):
        return None

    async def test_http_default_and_explicit_websocket_select_separate_transports(self):
        provider = self.make_provider()
        calls = []
        image_response = {
            "output": [
                {
                    "type": "image_generation_call",
                    "result": base64.b64encode(IMAGE).decode(),
                }
            ]
        }

        async def http(payload, request_timeout):
            calls.append(("http", payload, request_timeout))
            return image_response

        async def websocket(payload, *, timeout):
            calls.append(("websocket", payload, timeout))
            return image_response

        async def extract(response):
            self.assertEqual(response, image_response)
            return ["image-result"]

        provider._request_image_backend = http
        provider._request_image_backend_websocket = websocket
        provider._extract_generated_images = extract

        self.assertEqual(await provider.generate_image("cat"), ["image-result"])
        self.assertEqual(
            await provider.generate_image("cat", None, None, 1, None, None, 45),
            ["image-result"],
        )
        self.assertEqual(
            await provider.generate_image("cat", transport="websocket", timeout=90),
            ["image-result"],
        )
        self.assertEqual([call[0] for call in calls], ["http", "http", "websocket"])
        self.assertEqual([call[2] for call in calls], [30, 45.0, 90.0])
        self.assertEqual(calls[2][1]["tools"][0]["action"], "generate")
        self.assertTrue(ProviderOAuthPlugOpenAICodex.capabilities["image_websocket"])

    async def test_websocket_reassembles_image_stream_and_edit_references(self):
        provider = self.make_provider()
        captured = []

        async def extract(response):
            captured.append(response)
            return ["image-result"]

        provider._extract_generated_images = extract
        socket = Socket(image_events())
        session = Session(socket)
        fake_aiohttp = FakeAiohttp([session])
        reference = "data:image/png;base64," + base64.b64encode(IMAGE).decode()

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            result = await provider.generate_image(
                "edit cat", reference_images=[reference], transport="websocket"
            )

        self.assertEqual(result, ["image-result"])
        self.assertEqual(
            captured[0]["output"][0]["result"], base64.b64encode(IMAGE).decode()
        )
        self.assertEqual(captured[0]["usage"]["input_tokens"], 1)
        self.assertEqual(len(socket.sent), 1)
        request = socket.sent[0]
        self.assertEqual(request["type"], "response.create")
        self.assertNotIn("stream", request)
        self.assertEqual(request["tools"][0]["action"], "edit")
        self.assertEqual(request["input"][0]["content"][1]["image_url"], reference)
        self.assertEqual(
            session.connect_calls[0][0],
            "wss://chatgpt.example/backend-api/codex/responses",
        )
        self.assertEqual(session.connect_calls[0][1]["max_msg_size"], 64 * 1024 * 1024)
        self.assertTrue(session.closed and socket.closed)
        self.assertFalse(provider._oauth_stream_clients)

    async def test_handshake_401_refreshes_once_before_image_submission(self):
        provider = self.make_provider()
        denied = Session(Socket(), error=HandshakeError(401))
        accepted = Session(Socket(image_events()))
        fake_aiohttp = FakeAiohttp([denied, accepted])
        refreshes = []

        async def refresh(version):
            refreshes.append(version)
            return True

        provider._refresh_after_auth_failure = refresh
        provider._build_backend_headers_with_version = lambda: (
            {"Authorization": f"Bearer token-{len(refreshes)}"},
            len(refreshes),
        )
        provider._extract_generated_images = self.empty_images
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            await provider.generate_image("cat", transport="websocket")

        self.assertEqual(refreshes, [0])
        self.assertEqual(denied.socket.sent, [])
        self.assertEqual(len(accepted.socket.sent), 1)
        self.assertNotEqual(
            denied.connect_calls[0][1]["headers"],
            accepted.connect_calls[0][1]["headers"],
        )
        self.assertTrue(denied.closed and accepted.closed)

    async def empty_images(self, response):
        return []

    async def test_after_submission_failure_is_sanitized_and_never_resubmitted(self):
        provider = self.make_provider()
        socket = Socket(
            [
                {
                    "type": "response.failed",
                    "response": {
                        "error": {"status_code": 503, "message": "secret-token"}
                    },
                }
            ]
        )
        fake_aiohttp = FakeAiohttp([Session(socket)])
        http_calls = []

        async def http(*args):
            http_calls.append(args)
            return {}

        provider._request_image_backend = http
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            with self.assertRaises(Exception) as raised:
                await provider.generate_image("cat", transport="websocket")

        self.assertEqual(raised.exception.reason_code, "upstream_unavailable")
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertEqual(len(socket.sent), 1)
        self.assertEqual(http_calls, [])
        self.assertTrue(socket.closed)

    async def test_keepalive_cannot_extend_total_deadline(self):
        provider = self.make_provider()
        socket = Socket([{"type": "keepalive"}], wait_forever=True)
        session = Session(socket)
        with patch.dict(sys.modules, {"aiohttp": FakeAiohttp([session])}):
            with self.assertRaises(Exception) as raised:
                await provider.generate_image(
                    "cat", transport="websocket", timeout=0.02
                )

        self.assertEqual(raised.exception.reason_code, "outcome_unknown")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(socket.sent), 1)
        self.assertTrue(session.closed)

    async def test_terminate_during_handshake_prevents_image_submission(self):
        provider = self.make_provider()
        gate = asyncio.Event()
        socket = Socket(image_events())
        session = Session(socket, gate=gate)
        fake_aiohttp = FakeAiohttp([session])

        async def base_terminate(_self):
            return None

        with (
            patch.dict(sys.modules, {"aiohttp": fake_aiohttp}),
            patch.object(
                ProviderOpenAIOfficial, "terminate", base_terminate, create=True
            ),
        ):
            task = asyncio.create_task(
                provider.generate_image("cat", transport="websocket")
            )
            await asyncio.wait_for(session.handshaking.wait(), 1)
            await provider.terminate()
            gate.set()
            with self.assertRaises(Exception) as raised:
                await asyncio.wait_for(task, 1)

        self.assertEqual(raised.exception.reason_code, "provider_closed")
        self.assertEqual(socket.sent, [])
        self.assertTrue(session.closed)
        self.assertFalse(provider._oauth_stream_clients)

    async def test_terminate_after_submission_closes_socket_without_replay(self):
        provider = self.make_provider()
        socket = Socket(wait_forever=True)
        session = Session(socket)
        fake_aiohttp = FakeAiohttp([session])

        async def base_terminate(_self):
            return None

        with (
            patch.dict(sys.modules, {"aiohttp": fake_aiohttp}),
            patch.object(
                ProviderOpenAIOfficial, "terminate", base_terminate, create=True
            ),
        ):
            task = asyncio.create_task(
                provider.generate_image("cat", transport="websocket")
            )
            await asyncio.wait_for(socket.reading.wait(), 1)
            await provider.terminate()
            with self.assertRaises(Exception) as raised:
                await asyncio.wait_for(task, 1)

        self.assertEqual(raised.exception.reason_code, "outcome_unknown")
        self.assertEqual(len(socket.sent), 1)
        self.assertTrue(session.closed)
        self.assertFalse(provider._oauth_stream_clients)

    async def test_websocket_event_limit_stops_after_one_submission(self):
        import oauth_plug_openai_codex.provider as source

        provider = self.make_provider()
        socket = Socket([{"type": "keepalive"}] * 3)
        session = Session(socket)
        with (
            patch.dict(sys.modules, {"aiohttp": FakeAiohttp([session])}),
            patch.object(source, "IMAGE_WEBSOCKET_MAX_EVENTS", 2),
        ):
            with self.assertRaises(Exception) as raised:
                await provider.generate_image("cat", transport="websocket")

        self.assertEqual(raised.exception.reason_code, "outcome_unknown")
        self.assertEqual(len(socket.sent), 1)
        self.assertTrue(session.closed)

    async def test_invalid_transport_and_timeout_fail_before_request(self):
        provider = self.make_provider()
        provider._request_image_backend = self.empty_images
        for transport in ("", "ws", None):
            with self.subTest(transport=transport), self.assertRaises(ValueError):
                await provider.generate_image("cat", transport=transport)
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await provider.generate_image(
                    "cat", transport="websocket", timeout=timeout
                )
