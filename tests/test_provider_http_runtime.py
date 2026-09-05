"""Actual AstrBot provider integration against an isolated local HTTP backend."""

import asyncio
import json
import unittest
from unittest.mock import patch

try:
    import astrbot
    RUNTIME = bool(getattr(astrbot, "__file__", None))
except ImportError:
    RUNTIME = False


@unittest.skipUnless(RUNTIME, "Real AstrBot runtime required")
class ProviderHTTPRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_constructed_models_share_state_and_keep_service_ownership(self):
        from oauth_plug_openai_codex.provider import ProviderOAuthPlugOpenAICodex
        from oauth_plug_openai_codex.service import OpenAICodexOAuthService, get_service, set_service, PROVIDER_TYPE
        old_service = get_service()
        first = OpenAICodexOAuthService({"oauth": {"oauth_access_token": "first-access", "oauth_refresh_token": "first-refresh", "oauth_account_id": "first-account"}})
        replacement = OpenAICodexOAuthService({"oauth": {"oauth_access_token": "second-access", "oauth_refresh_token": "second-refresh", "oauth_account_id": "second-account"}})
        set_service(first)
        try:
            config = {"id": "ownership-test", "type": PROVIDER_TYPE, "model": "gpt-6-astra"}
            a = ProviderOAuthPlugOpenAICodex(config, {})
            b = ProviderOAuthPlugOpenAICodex({**config, "model": "gpt-5.6-sol"}, {})
            self.assertIs(a._oauth_shared_state, b._oauth_shared_state)
            self.assertIs(a._oauth_shared_state, first.shared_state)
            version = first.shared_state.version
            set_service(replacement)
            calls = []
            async def refresh(token, proxy):
                calls.append(token)
                return {"access_token": "rotated-first", "refresh_token": "rotated-refresh"}
            with patch("oauth_plug_openai_codex.service.refresh_access_token", refresh):
                await a._refresh_after_auth_failure(version)
            self.assertEqual(calls, ["first-refresh"])
            self.assertEqual(b._oauth_shared_state.snapshot()["oauth_access_token"], "rotated-first")
            self.assertEqual(replacement.shared_state.snapshot()["oauth_access_token"], "second-access")
        finally:
            await first.close()
            await replacement.close()
            set_service(old_service)

    async def test_chat_and_stream_through_real_http_and_plugin_registration(self):
        from aiohttp import web
        from oauth_plug_openai_codex.provider import ProviderOAuthPlugOpenAICodex
        from oauth_plug_openai_codex.registration import register_provider_adapter, unregister_provider_adapter
        from oauth_plug_openai_codex.service import OpenAICodexOAuthService, get_service, set_service, PROVIDER_TYPE

        requests = []
        async def respond(request):
            body = await request.json()
            requests.append(body)
            self.assertEqual(request.headers["Authorization"], "Bearer test-access")
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for event in (
                {"type": "response.output_text.delta", "delta": "hello"},
                {"type": "response.output_text.delta", "delta": " world"},
                {"type": "response.completed", "response": {"id": "resp_local", "status": "completed", "output": [], "output_text": "hello world"}},
            ):
                await response.write(("data: " + json.dumps(event) + "\n\n").encode())
                await asyncio.sleep(0)
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/responses", respond)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        old_service = get_service()
        service = OpenAICodexOAuthService({"runtime": {"base_url": f"http://127.0.0.1:{port}"}, "oauth": {
            "oauth_access_token": "test-access", "oauth_account_id": "test-account",
        }})
        set_service(service)
        register_provider_adapter()
        try:
            provider = ProviderOAuthPlugOpenAICodex({"id": "local-http-test", "type": PROVIDER_TYPE, "model": "gpt-6-astra", "timeout": 5}, {})
            with patch("oauth_plug_openai_codex.provider.db_helper", None):
                response = await provider.text_chat(prompt="hello", reasoning_effort="high", oauth_web_search="live")
                self.assertEqual(response.completion_text, "hello world")
                chunks = [chunk async for chunk in provider.text_chat_stream(prompt="hello", oauth_web_search="cached")]
            self.assertEqual([chunk.completion_text for chunk in chunks], ["hello", " world", "hello world"])
            self.assertEqual([chunk.is_chunk for chunk in chunks], [True, True, False])
            self.assertEqual(requests[0]["reasoning"]["effort"], "high")
            self.assertTrue(requests[0]["tools"][0]["external_web_access"])
            self.assertFalse(requests[1]["tools"][0]["external_web_access"])
            self.assertNotIn("oauth_access_token", requests[0])
        finally:
            await service.close()
            unregister_provider_adapter()
            set_service(old_service)
            await runner.cleanup()
