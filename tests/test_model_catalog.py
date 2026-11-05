import asyncio
import json
import types
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from test_provider import _install_fake_astrbot_runtime

_install_fake_astrbot_runtime()

from oauth_plug_openai_codex import model_catalog  # noqa: E402
from oauth_plug_openai_codex.headers import CODEX_CLIENT_VERSION  # noqa: E402
from oauth_plug_openai_codex.openai_oauth_shared_state import (  # noqa: E402
    OpenAIOAuthSharedState,
)


class CatalogResponse:
    def __init__(self, body, status=200, before_read=None):
        self.status_code = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.before_read = before_read

    async def aiter_bytes(self):
        if self.before_read is not None:
            await self.before_read()
        midpoint = len(self.body) // 2
        yield self.body[:midpoint]
        yield self.body[midpoint:]


class CatalogProvider:
    def __init__(self, responses):
        self.provider_config = {"proxy": ""}
        self.base_url = "https://chatgpt.example/backend-api/codex"
        self._oauth_shared_state = OpenAIOAuthSharedState(
            "test-catalog",
            {
                "oauth_access_token": "test-access",
                "oauth_refresh_token": "test-refresh",
                "oauth_account_id": "test-account",
            },
        )
        self.responses = list(responses)
        self.requests = []
        self.opened = 0
        self._ensure_fresh_oauth_token = AsyncMock()
        self._refresh_after_auth_failure = AsyncMock(return_value=False)

    def _sync_oauth_credentials_from_shared(self):
        self.provider_config.update(self._oauth_shared_state.snapshot())

    def _build_backend_headers_with_version(self):
        version, credentials = self._oauth_shared_state.versioned_snapshot()
        if not credentials["oauth_access_token"]:
            raise ValueError("No access token")
        return {"Authorization": "Bearer " + credentials["oauth_access_token"]}, version

    @asynccontextmanager
    async def _open_oauth_http_client(self, **options):
        self.opened += 1
        self.client_options = options

        @asynccontextmanager
        async def stream(method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            yield response

        yield types.SimpleNamespace(stream=stream)


class ModelCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_works_without_python_311_timeout_context(self):
        provider = CatalogProvider([CatalogResponse({"models": [{"slug": "remote-model"}]})])
        with patch.dict(asyncio.__dict__):
            asyncio.__dict__.pop("timeout", None)
            result = await model_catalog.discover_models(provider, ["manual"])
        self.assertEqual(result, ["manual", "remote-model"])
        self.assertEqual(len(provider.requests), 1)

    async def test_overall_deadline_cancels_refresh_and_read_operations(self):
        for phase in ("initial_refresh", "read", "auth_refresh"):
            with self.subTest(phase=phase):
                cleaned = asyncio.Event()

                async def block(*args):
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaned.set()

                provider = CatalogProvider([
                    CatalogResponse({"models": []}, status=401 if phase == "auth_refresh" else 200,
                                    before_read=block if phase == "read" else None)
                ])
                if phase == "initial_refresh":
                    provider._ensure_fresh_oauth_token.side_effect = block
                elif phase == "auth_refresh":
                    provider._refresh_after_auth_failure.side_effect = block
                with patch.object(model_catalog, "CATALOG_TIMEOUT_SECONDS", 0.01):
                    result = await model_catalog.discover_models(provider, ["manual"])
                self.assertEqual(result, ["manual"])
                self.assertTrue(cleaned.is_set())
                self.assertFalse(provider._oauth_model_catalog_lock.locked())
                self.assertLessEqual(len(provider.requests), 1)

    async def test_discovers_visible_models_and_preserves_manual_order(self):
        provider = CatalogProvider(
            [
                CatalogResponse(
                    {
                        "models": [
                            {"slug": "gpt-new", "visibility": "list", "supported_in_api": False},
                            {"slug": "custom-model"},
                            {"slug": "hidden-model", "visibility": "hide"},
                            {"slug": "gpt-new"},
                            {"slug": "bad\nmodel"},
                            {"slug": ""},
                            "not-an-object",
                        ]
                    }
                )
            ]
        )
        result = await model_catalog.discover_models(provider, ["custom-model", "gpt-old"])
        self.assertEqual(result, ["custom-model", "gpt-old", "gpt-new"])
        method, url, kwargs = provider.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, provider.base_url + "/models")
        self.assertEqual(kwargs["params"], {"client_version": CODEX_CLIENT_VERSION})
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-access")
        self.assertFalse(provider.client_options["follow_redirects"])

    async def test_unbound_provider_returns_fallback_without_refresh_or_network(self):
        provider = CatalogProvider([])
        provider._oauth_shared_state.replace({})
        self.assertEqual(await model_catalog.discover_models(provider, ["manual"]), ["manual"])
        provider._ensure_fresh_oauth_token.assert_not_awaited()
        provider._refresh_after_auth_failure.assert_not_awaited()
        self.assertEqual(provider.opened, 0)

    async def test_closed_provider_does_not_serve_cache_or_make_requests(self):
        provider = CatalogProvider([CatalogResponse({"models": [{"slug": "cached"}]})])
        await model_catalog.discover_models(provider, [])
        provider._oauth_media_closed = True
        with self.assertRaises(RuntimeError):
            await model_catalog.discover_models(provider, ["manual"])
        self.assertEqual(provider.opened, 1)

    async def test_failure_is_sanitized_and_short_cached(self):
        provider = CatalogProvider([RuntimeError("SECRET_BACKEND_BODY")])
        with patch.object(model_catalog.logger, "warning") as warning:
            with patch.object(model_catalog, "time", types.SimpleNamespace(monotonic=lambda: 100)):
                self.assertEqual(await model_catalog.discover_models(provider, ["first"]), ["first"])
                self.assertEqual(await model_catalog.discover_models(provider, ["second"]), ["second"])
            self.assertEqual(warning.call_count, 1)
            self.assertNotIn("SECRET_BACKEND_BODY", str(warning.call_args))
        self.assertEqual(provider.opened, 1)

    async def test_invalid_status_json_shape_and_oversize_body_fall_back(self):
        responses = (
            CatalogResponse({"error": "SECRET"}, status=404),
            CatalogResponse(b"{invalid SECRET"),
            CatalogResponse({"models": {"slug": "wrong-shape"}}),
            CatalogResponse(b"x" * 128),
        )
        with patch.object(model_catalog, "MAX_CATALOG_BYTES", 100):
            for response in responses:
                with self.subTest(response=response):
                    provider = CatalogProvider([response])
                    self.assertEqual(await model_catalog.discover_models(provider, ["manual"]), ["manual"])

    async def test_auth_failure_refreshes_at_most_once(self):
        provider = CatalogProvider(
            [CatalogResponse({}, status=401), CatalogResponse({"models": [{"slug": "after-refresh"}]})]
        )

        async def refresh(_version):
            provider._oauth_shared_state.apply({"oauth_access_token": "new-access"})
            return True

        provider._refresh_after_auth_failure.side_effect = refresh
        self.assertEqual(await model_catalog.discover_models(provider, []), ["after-refresh"])
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[1][2]["headers"]["Authorization"], "Bearer new-access")
        provider._refresh_after_auth_failure.assert_awaited_once()

        denied = CatalogProvider([CatalogResponse({}, status=403), CatalogResponse({}, status=403)])
        denied._refresh_after_auth_failure.return_value = True
        self.assertEqual(await model_catalog.discover_models(denied, ["manual"]), ["manual"])
        denied._refresh_after_auth_failure.assert_awaited_once()
        self.assertEqual(len(denied.requests), 2)

    async def test_concurrent_callers_share_one_successful_discovery(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def pause():
            started.set()
            await release.wait()

        provider = CatalogProvider([CatalogResponse({"models": [{"slug": "discovered"}]}, before_read=pause)])
        first = asyncio.create_task(model_catalog.discover_models(provider, ["first"]))
        await started.wait()
        second = asyncio.create_task(model_catalog.discover_models(provider, ["second"]))
        release.set()
        self.assertEqual(await first, ["first", "discovered"])
        self.assertEqual(await second, ["second", "discovered"])
        self.assertEqual(provider.opened, 1)

    async def test_account_rotation_invalidates_cache_and_inflight_results(self):
        provider = CatalogProvider(
            [CatalogResponse({"models": [{"slug": "old-account-model"}]}), CatalogResponse({"models": [{"slug": "new-account-model"}]})]
        )
        self.assertEqual(await model_catalog.discover_models(provider, []), ["old-account-model"])
        provider._oauth_shared_state.replace({"oauth_access_token": "new", "oauth_account_id": "new-account"})
        self.assertEqual(await model_catalog.discover_models(provider, []), ["new-account-model"])

        async def switch_account():
            rotated._oauth_shared_state.replace({"oauth_access_token": "rotated", "oauth_account_id": "other"})

        rotated = CatalogProvider([CatalogResponse({"models": [{"slug": "must-not-leak"}]}, before_read=switch_account)])
        self.assertEqual(await model_catalog.discover_models(rotated, ["manual"]), ["manual"])

    async def test_cache_expires_and_base_url_changes_invalidate_it(self):
        provider = CatalogProvider(
            [CatalogResponse({"models": [{"slug": name}]}) for name in ("first", "second", "third")]
        )
        with patch.object(model_catalog, "time", types.SimpleNamespace(monotonic=lambda: 100)):
            self.assertEqual(await model_catalog.discover_models(provider, []), ["first"])
            self.assertEqual(await model_catalog.discover_models(provider, []), ["first"])
        with patch.object(model_catalog, "time", types.SimpleNamespace(monotonic=lambda: 401)):
            self.assertEqual(await model_catalog.discover_models(provider, []), ["second"])
            provider.base_url += "/different"
            self.assertEqual(await model_catalog.discover_models(provider, []), ["third"])
        self.assertEqual(provider.opened, 3)

    async def test_timeout_falls_back_and_cancellation_propagates(self):
        provider = CatalogProvider([TimeoutError("SECRET_TIMEOUT")])
        self.assertEqual(await model_catalog.discover_models(provider, ["manual"]), ["manual"])

        started, release = asyncio.Event(), asyncio.Event()

        async def pause():
            started.set()
            await release.wait()

        cancelled = CatalogProvider([CatalogResponse({"models": []}, before_read=pause)])
        request = asyncio.create_task(model_catalog.discover_models(cancelled, []))
        await started.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request


if __name__ == "__main__":
    unittest.main()
