import asyncio
import unittest

from test_provider import _install_fake_astrbot_runtime

_install_fake_astrbot_runtime()

from oauth_plug_openai_codex.provider import ProviderOAuthPlugOpenAICodex  # noqa: E402


class _OpenService:
    def _require_open(self):
        return None


class _ClosedService:
    def _require_open(self):
        raise RuntimeError("OAuth service is closed")


class ProviderClosedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_provider(self, **attrs):
        provider = ProviderOAuthPlugOpenAICodex.__new__(
            ProviderOAuthPlugOpenAICodex
        )
        provider.provider_config = {"model": "gpt-6.1-sol"}
        provider.model_name = "gpt-6.1-sol"
        for name, value in attrs.items():
            setattr(provider, name, value)
        return provider

    async def test_open_instance_is_noop(self):
        provider = self.make_provider(_oauth_media_closed=False)
        await provider._ensure_oauth_provider_open()
        self.assertFalse(provider._oauth_media_closed)

    async def test_recovers_after_terminate_when_service_open(self):
        transcription = object()
        realtime = object()
        provider = self.make_provider(
            _oauth_media_closed=True,
            _oauth_service=_OpenService(),
            _oauth_transcription_client=transcription,
            _oauth_realtime_client=realtime,
        )
        await provider._ensure_oauth_provider_open()
        self.assertFalse(provider._oauth_media_closed)
        self.assertNotIn("_oauth_transcription_client", provider.__dict__)
        self.assertNotIn("_oauth_realtime_client", provider.__dict__)
        self.assertNotIn("_oauth_media_cleanup_task", provider.__dict__)

    async def test_refuses_recovery_when_service_closed(self):
        provider = self.make_provider(
            _oauth_media_closed=True,
            _oauth_service=_ClosedService(),
        )
        with self.assertRaisesRegex(RuntimeError, "service is closed"):
            await provider._ensure_oauth_provider_open()
        self.assertTrue(provider._oauth_media_closed)

    async def test_waits_for_pending_cleanup_before_recovering(self):
        provider = self.make_provider(
            _oauth_media_closed=True,
            _oauth_service=_OpenService(),
        )
        finished = asyncio.Event()

        async def cleanup():
            await asyncio.sleep(0.05)
            finished.set()

        provider._oauth_media_cleanup_task = asyncio.create_task(cleanup())
        await provider._ensure_oauth_provider_open()
        self.assertTrue(finished.is_set())
        self.assertFalse(provider._oauth_media_closed)

    async def test_cleanup_failure_propagates_and_stays_closed(self):
        provider = self.make_provider(
            _oauth_media_closed=True,
            _oauth_service=_OpenService(),
        )

        async def broken_cleanup():
            raise ValueError("cleanup failed")

        provider._oauth_media_cleanup_task = asyncio.create_task(
            broken_cleanup()
        )
        with self.assertRaisesRegex(ValueError, "cleanup failed"):
            await provider._ensure_oauth_provider_open()
        self.assertTrue(provider._oauth_media_closed)

    async def test_request_backend_recovers_and_serves(self):
        provider = self.make_provider(
            _oauth_media_closed=True,
            _oauth_service=_OpenService(),
        )
        calls = []

        async def fake_token():
            calls.append("token")

        async def fake_once(payload):
            calls.append("once")
            return 200, "raw-body", "v1"

        provider._ensure_fresh_oauth_token = fake_token
        provider._request_backend_once = fake_once
        provider._parse_backend_response = lambda text: {"parsed": text}

        result = await provider._request_backend({"model": "gpt-6.1-sol"})
        self.assertEqual(result, {"parsed": "raw-body"})
        self.assertEqual(calls, ["token", "once"])
        self.assertFalse(provider._oauth_media_closed)

    async def test_request_backend_still_fails_when_service_closed(self):
        provider = self.make_provider(
            _oauth_media_closed=True,
            _oauth_service=_ClosedService(),
        )

        async def fake_once(payload):  # pragma: no cover - must not run
            raise AssertionError("backend must not be called")

        provider._request_backend_once = fake_once
        with self.assertRaisesRegex(RuntimeError, "service is closed"):
            await provider._request_backend({"model": "gpt-6.1-sol"})
        self.assertTrue(provider._oauth_media_closed)


if __name__ == "__main__":
    unittest.main()
