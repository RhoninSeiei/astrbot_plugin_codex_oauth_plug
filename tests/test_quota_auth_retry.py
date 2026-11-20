"""Quota authentication recovery against an isolated HTTP transport."""

import asyncio
import unittest
from unittest.mock import AsyncMock

import httpx

from oauth_plug_openai_codex.quota import QuotaReader


class QuotaAuthRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.account = "account-one"
        self.token = "old-token"
        self.version = 17
        self.requests = []
        self.responses = []

        class Provider:
            base_url = "https://chatgpt.com/backend-api/codex"
            provider_config = {}

        self.provider = Provider()
        self.provider._build_backend_headers = self.headers
        self.provider._build_backend_headers_with_version = lambda: (self.headers(), self.version)
        self.provider._refresh_after_auth_failure = AsyncMock(side_effect=self.refresh)
        transport = httpx.MockTransport(self.respond)
        self.reader = QuotaReader(client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport))
        self.addAsyncCleanup(self.reader.close)

    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "ChatGPT-Account-Id": self.account}

    def rotate(self, *, account=None):
        self.version += 1
        self.token = f"token-{self.version}"
        if account is not None:
            self.account = account

    async def refresh(self, version):
        if version == self.version:
            self.rotate()
        return True

    async def respond(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        return response() if callable(response) else response

    async def test_401_recovers_once_and_caches_only_the_new_credentials(self):
        # Only the versioned builder provides the actual credentials to send.
        self.provider._build_backend_headers = lambda: {
            "Authorization": "Bearer stale-builder-token", "ChatGPT-Account-Id": self.account
        }
        self.responses.extend([httpx.Response(401), httpx.Response(200, json={"plan_type": "pro"})])
        result = await self.reader.read(self.provider)
        self.assertEqual((result["status"], result["cached"], result["plan_type"]), ("success", False, "pro"))
        self.assertEqual([req.headers["authorization"] for req in self.requests], ["Bearer old-token", "Bearer token-18"])
        self.provider._refresh_after_auth_failure.assert_awaited_once_with(17)
        self.assertTrue((await self.reader.read(self.provider))["cached"])

        self.rotate()
        self.responses.append(httpx.Response(200, json={"plan_type": "plus"}))
        result = await self.reader.read(self.provider)
        self.assertEqual((result["cached"], result["plan_type"]), (False, "plus"))
        self.assertEqual(len(self.requests), 3)

    async def test_second_401_terminates_even_if_credentials_rotated_again(self):
        def denied_after_another_rotation():
            self.rotate()
            return httpx.Response(401)

        self.responses.extend([httpx.Response(401), denied_after_another_rotation, httpx.Response(200, json={})])
        self.assertEqual(await self.reader.read(self.provider), {"status": "reauth_required", "http_status": 401})
        self.assertEqual(len(self.requests), 2)
        self.provider._refresh_after_auth_failure.assert_awaited_once_with(17)

    async def test_401_after_an_inflight_refetch_cannot_start_a_third_get(self):
        def stale_success():
            self.rotate()
            return httpx.Response(200, json={"plan_type": "stale"})

        self.responses.extend([stale_success, httpx.Response(401), httpx.Response(200, json={})])
        self.assertEqual(await self.reader.read(self.provider), {"status": "reauth_required", "http_status": 401})
        self.assertEqual(len(self.requests), 2)
        self.provider._refresh_after_auth_failure.assert_not_awaited()

    async def test_already_rotated_credentials_reuse_the_original_attempt_version(self):
        def stale_unauthorized():
            self.rotate()
            return httpx.Response(401)

        self.responses.extend([stale_unauthorized, httpx.Response(200, json={})])
        self.assertEqual((await self.reader.read(self.provider))["status"], "success")
        self.provider._refresh_after_auth_failure.assert_awaited_once_with(17)
        self.assertEqual(self.requests[1].headers["authorization"], "Bearer token-18")

    async def test_retry_success_for_replaced_credentials_is_not_returned_or_cached(self):
        def outdated_success():
            self.rotate(account="account-two")
            return httpx.Response(200, json={"plan_type": "stale"})

        self.responses.extend([httpx.Response(401), outdated_success, httpx.Response(200, json={"plan_type": "pro"})])
        self.assertEqual(await self.reader.read(self.provider), {"status": "unavailable"})
        self.assertEqual(len(self.requests), 2)
        result = await self.reader.read(self.provider)
        self.assertEqual((result["cached"], result["plan_type"]), (False, "pro"))
        self.assertEqual(self.requests[-1].headers["chatgpt-account-id"], "account-two")

    async def test_account_change_during_401_does_not_refresh_or_replay(self):
        def changed_account():
            self.rotate(account="account-two")
            return httpx.Response(401)

        self.responses.extend([changed_account, httpx.Response(200, json={})])
        self.assertEqual(await self.reader.read(self.provider), {"status": "reauth_required", "http_status": 401})
        self.assertEqual(len(self.requests), 1)
        self.provider._refresh_after_auth_failure.assert_not_awaited()

    async def test_account_change_during_refresh_does_not_replay(self):
        async def refresh(version):
            self.rotate(account="account-two")
            return True

        self.provider._refresh_after_auth_failure.side_effect = refresh
        self.responses.extend([httpx.Response(401), httpx.Response(200, json={})])
        self.assertEqual(await self.reader.read(self.provider), {"status": "reauth_required", "http_status": 401})
        self.assertEqual(len(self.requests), 1)
        self.provider._refresh_after_auth_failure.assert_awaited_once_with(17)

    async def test_forbidden_and_legacy_provider_do_not_refresh(self):
        self.responses.extend([httpx.Response(403), httpx.Response(401)])
        self.assertEqual(await self.reader.read(self.provider), {"status": "forbidden", "http_status": 403})
        del self.provider._build_backend_headers_with_version
        self.assertEqual(await self.reader.read(self.provider), {"status": "reauth_required", "http_status": 401})
        self.assertEqual(len(self.requests), 2)
        self.provider._refresh_after_auth_failure.assert_not_awaited()

    async def test_unsuccessful_refresh_requires_reauthorization_without_replay(self):
        refresh = self.provider._refresh_after_auth_failure
        refresh.side_effect = [False, ValueError("private refresh error")]
        self.responses.extend([httpx.Response(401), httpx.Response(401)])
        for _ in range(2):
            self.assertEqual(await self.reader.read(self.provider), {"status": "reauth_required", "http_status": 401})
        self.assertEqual(refresh.await_count, 2)
        self.assertEqual(len(self.requests), 2)

    async def test_cancelled_refresh_propagates_and_releases_reader_lock(self):
        started = asyncio.Event()

        async def refresh(version):
            started.set()
            await asyncio.Future()

        self.provider._refresh_after_auth_failure.side_effect = refresh
        self.responses.extend([httpx.Response(401), httpx.Response(200, json={})])
        task = asyncio.create_task(self.reader.read(self.provider))
        try:
            await asyncio.wait_for(started.wait(), 2)
        finally:
            task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await self.reader.read(self.provider))["status"], "success")
        self.assertEqual(len(self.requests), 2)
