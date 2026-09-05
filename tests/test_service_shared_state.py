import asyncio
import unittest
from unittest.mock import patch

from oauth_plug_openai_codex.service import OpenAICodexOAuthService


class ServiceSharedStateTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self):
        return OpenAICodexOAuthService({"oauth": {
            "oauth_access_token": "old-access", "oauth_refresh_token": "old-refresh",
            "oauth_account_id": "account",
        }})

    async def test_models_share_credentials_and_disconnect_invalidates_them(self):
        service = self.make_service()
        first = service.build_provider_config({"model": "gpt-6-astra"})
        second = service.build_provider_config({"model": "gpt-5.6-sol"})
        shared = first["oauth_shared_state"]
        self.assertIs(shared, second["oauth_shared_state"])
        await service.persist_token({"access_token": "new-access"})
        self.assertEqual(shared.snapshot()["oauth_access_token"], "new-access")
        self.assertEqual(shared.snapshot()["oauth_refresh_token"], "old-refresh")
        version = shared.version
        service.disconnect()
        self.assertEqual(shared.snapshot()["oauth_access_token"], "")
        self.assertGreater(shared.version, version)

    async def test_concurrent_refresh_rotates_once(self):
        service = self.make_service()
        calls = []
        async def refresh(token, proxy):
            calls.append(token)
            await asyncio.sleep(0.01)
            return {"access_token": "new-access", "refresh_token": "new-refresh"}
        with patch("oauth_plug_openai_codex.service.refresh_access_token", refresh):
            await asyncio.gather(service.refresh(), service.refresh())
        self.assertEqual(calls, ["old-refresh"])
        self.assertEqual(service.shared_state.snapshot()["oauth_refresh_token"], "new-refresh")

    async def test_disconnect_during_refresh_cannot_restore_credentials(self):
        service = self.make_service()
        async def refresh(token, proxy):
            service.disconnect()
            return {"access_token": "late-access", "refresh_token": "late-refresh"}
        with patch("oauth_plug_openai_codex.service.refresh_access_token", refresh):
            await service.refresh()
        self.assertEqual(service.shared_state.snapshot()["oauth_access_token"], "")

    async def test_runtime_refresh_skew_and_advanced_options_reach_provider(self):
        service = OpenAICodexOAuthService({
            "runtime": {"oauth_refresh_skew_seconds": 90},
            "advanced": {"oauth_web_search": "live", "oauth_web_search_domains": ["example.com"],
                         "oauth_audio_transcription": True, "oauth_transcription_model": "gpt-4o-transcribe"},
        })
        config = service.build_provider_config({"oauth_web_search": "cached"})
        self.assertEqual(config["oauth_refresh_skew_seconds"], 90)
        self.assertEqual(config["oauth_web_search"], "cached")
        self.assertEqual(config["oauth_web_search_domains"], ["example.com"])
        self.assertTrue(config["oauth_audio_transcription"])

    async def test_test_endpoint_rejects_incomplete_and_failed_events(self):
        service = self.make_service()
        for body in ('data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
                     'data: {"type":"response.failed","response":{"error":{"message":"failed"}}}\n\n'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                service._parse_test_backend_response(body)

    async def test_close_terminates_all_providers_without_erasing_saved_binding(self):
        service = self.make_service()
        calls = []
        class Provider:
            async def terminate(self):
                calls.append(self)
        first, second = Provider(), Provider()
        service.register_provider(first)
        service.register_provider(second)
        await service.close()
        await service.close()
        self.assertCountEqual(calls, [first, second])
        self.assertEqual(service.shared_state.snapshot()["oauth_access_token"], "")
        self.assertEqual(service.config["oauth"]["oauth_access_token"], "old-access")

    async def test_successful_completion_is_reported(self):
        service = self.make_service()
        result = service._parse_test_backend_response(
            'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
            'data: {"type":"response.completed","response":{"id":"test-response"}}\n\n'
        )
        self.assertEqual(result, {"response_id": "test-response", "output_text": "OK"})

    async def test_disconnect_during_authorization_rejects_late_token(self):
        service = self.make_service()
        flow = service.create_flow()
        async def exchange(*args):
            service.disconnect()
            return {"access_token": "late-access"}
        with patch("oauth_plug_openai_codex.service.exchange_authorization_code", exchange):
            with self.assertRaisesRegex(ValueError, "授权状态"):
                await service.complete_flow("code#" + flow["state"])
        self.assertEqual(service.shared_state.snapshot()["oauth_access_token"], "")

    async def test_new_binding_does_not_reuse_previous_account_fields(self):
        service = self.make_service()
        await service.complete_flow('{"access_token":"another-account-access"}')
        state = service.shared_state.snapshot()
        self.assertEqual(state["oauth_refresh_token"], "")
        self.assertEqual(state["oauth_account_id"], "")

    async def test_json_completion_requires_success_status(self):
        service = self.make_service()
        for body in ('{"error":{"message":"denied"}}', '{broken',
                     '{"id":"failed-response","status":"failed"}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                service._parse_test_backend_response(body)
        self.assertEqual(service._parse_test_backend_response(
            '{"id":"ok-response","status":"completed","output_text":"OK"}'
        )["output_text"], "OK")

    async def test_closing_service_rejects_new_authorization(self):
        service = self.make_service()
        entered, release = asyncio.Event(), asyncio.Event()
        class Provider:
            async def terminate(self):
                entered.set()
                await release.wait()
        provider = Provider()
        service.register_provider(provider)
        close_task = asyncio.create_task(service.close())
        await entered.wait()
        try:
            with self.assertRaisesRegex(RuntimeError, "关闭"):
                await service.complete_flow('{"access_token":"late"}')
        finally:
            release.set()
            await close_task
        self.assertEqual(service.shared_state.snapshot()["oauth_access_token"], "")
