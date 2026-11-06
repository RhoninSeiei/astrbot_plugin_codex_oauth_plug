import asyncio
import copy
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_model_catalog import CatalogProvider, CatalogResponse

from oauth_plug_openai_codex import model_catalog


class ModelCatalogMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_and_model_list_share_cache_without_mutating_config(self):
        provider = CatalogProvider([
            CatalogResponse({"models": [
                {"slug": "remote-model", "context_window": 128000,
                 "max_context_window": 256000, "instructions": "SECRET"},
                {"slug": "manual-model", "context_window": 64000},
            ]})
        ])
        provider._sync_oauth_credentials_from_shared()
        original_config = copy.deepcopy(provider.provider_config)
        self.assertEqual(
            await model_catalog.discover_models(provider, ["manual-model", "configured"]),
            ["manual-model", "configured", "remote-model"],
        )
        metadata = await model_catalog.get_model_context_metadata(provider, "remote-model")
        self.assertEqual(metadata, {"context_window": 128000, "max_context_window": 256000})
        metadata["context_window"] = 1
        self.assertEqual(
            await model_catalog.get_model_context_metadata(provider, "remote-model"),
            {"context_window": 128000, "max_context_window": 256000},
        )
        self.assertEqual(provider.provider_config, original_config)
        self.assertEqual(provider.opened, 1)

    async def test_metadata_can_be_queried_when_automatic_list_discovery_is_disabled(self):
        provider = CatalogProvider([
            CatalogResponse({"models": [{"slug": "effective-model", "context_window": 32000}]})
        ])
        provider.provider_config["oauth_auto_models"] = False
        provider.get_models = AsyncMock(side_effect=AssertionError("Do not change list discovery policy"))
        self.assertEqual(
            await model_catalog.get_model_context_metadata(provider, "effective-model"),
            {"context_window": 32000},
        )
        provider.get_models.assert_not_awaited()
        self.assertFalse(provider.provider_config["oauth_auto_models"])
        self.assertEqual(
            await model_catalog.discover_models(provider, ["manual"]),
            ["manual", "effective-model"],
        )
        self.assertEqual(provider.opened, 1)

    async def test_only_bounded_integer_context_limits_are_cached(self):
        invalid = (True, False, 0, -1, 1023, 10000001, 1000000000,
                   32000.0, "32000", None, [], {})
        entries = [
            {"slug": f"invalid-{index}", "context_window": value, "max_context_window": value}
            for index, value in enumerate(invalid)
        ]
        entries.extend([
            {"slug": "lower-bound", "context_window": 1024},
            {"slug": "upper-bound", "max_context_window": 10000000},
            {"slug": "inconsistent", "context_window": 128000, "max_context_window": 64000},
            {"slug": "partial", "context_window": "bad", "max_context_window": 256000},
        ])
        provider = CatalogProvider([CatalogResponse({"models": entries})])
        for index, value in enumerate(invalid):
            with self.subTest(value=value):
                self.assertEqual(
                    await model_catalog.get_model_context_metadata(provider, f"invalid-{index}"), {},
                )
        expected = {
            "lower-bound": {"context_window": 1024},
            "upper-bound": {"max_context_window": 10000000},
            "inconsistent": {"context_window": 128000},
            "partial": {"max_context_window": 256000},
        }
        for model, limits in expected.items():
            with self.subTest(model=model):
                self.assertEqual(await model_catalog.get_model_context_metadata(provider, model), limits)
        self.assertEqual(provider.opened, 1)

    async def test_unknown_hidden_and_invalid_model_ids_have_no_metadata(self):
        provider = CatalogProvider([CatalogResponse({"models": [
            {"slug": "visible", "context_window": 64000, "supported_in_api": False},
            {"slug": "hidden", "visibility": "hide", "context_window": 128000},
            {"slug": "bad\nmodel", "context_window": 128000},
        ]})])
        for model in (None, 42, "", "bad\nmodel", " visible "):
            with self.subTest(model=model):
                self.assertEqual(await model_catalog.get_model_context_metadata(provider, model), {})
        self.assertEqual(provider.opened, 0)
        self.assertEqual(await model_catalog.get_model_context_metadata(provider, "visible"),
                         {"context_window": 64000})
        self.assertEqual(await model_catalog.get_model_context_metadata(provider, "hidden"), {})
        self.assertEqual(await model_catalog.get_model_context_metadata(provider, "unknown"), {})

    async def test_failure_and_expiry_never_reuse_old_context_limits(self):
        provider = CatalogProvider([
            CatalogResponse({"models": [{"slug": "model", "context_window": 64000}]}),
            CatalogResponse(b"{SECRET_MALFORMED"),
            CatalogResponse({"models": [{"slug": "model", "context_window": 128000}]}),
        ])
        with patch.object(model_catalog, "time", types.SimpleNamespace(monotonic=lambda: 100)):
            self.assertEqual(await model_catalog.get_model_context_metadata(provider, "model"),
                             {"context_window": 64000})
        with patch.object(model_catalog, "time", types.SimpleNamespace(monotonic=lambda: 401)):
            with patch.object(model_catalog.logger, "warning") as warning:
                self.assertEqual(await model_catalog.get_model_context_metadata(provider, "model"), {})
                self.assertNotIn("SECRET_MALFORMED", str(warning.call_args))
            self.assertEqual(await model_catalog.get_model_context_metadata(provider, "model"), {})
        with patch.object(model_catalog, "time", types.SimpleNamespace(monotonic=lambda: 432)):
            self.assertEqual(await model_catalog.get_model_context_metadata(provider, "model"),
                             {"context_window": 128000})
        self.assertEqual(provider.opened, 3)

    async def test_metadata_is_isolated_from_account_switches_and_inflight_changes(self):
        provider = CatalogProvider([
            CatalogResponse({"models": [{"slug": "model", "context_window": 64000}]}),
            CatalogResponse({"models": [{"slug": "model", "context_window": 128000}]}),
        ])
        self.assertEqual(await model_catalog.get_model_context_metadata(provider, "model"),
                         {"context_window": 64000})
        provider._oauth_shared_state.replace({"oauth_access_token": "new", "oauth_account_id": "new-account"})
        self.assertEqual(await model_catalog.get_model_context_metadata(provider, "model"),
                         {"context_window": 128000})

        async def switch_account():
            inflight._oauth_shared_state.replace({"oauth_access_token": "changed", "oauth_account_id": "other"})

        inflight = CatalogProvider([
            CatalogResponse({"models": [{"slug": "model", "context_window": 256000}]}, before_read=switch_account)
        ])
        self.assertEqual(await model_catalog.get_model_context_metadata(inflight, "model"), {})

    async def test_concurrent_metadata_and_list_requests_share_one_fetch(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause():
            entered.set()
            await release.wait()

        provider = CatalogProvider([
            CatalogResponse({"models": [{"slug": "model", "context_window": 64000}]}, before_read=pause)
        ])
        metadata = asyncio.create_task(model_catalog.get_model_context_metadata(provider, "model"))
        await entered.wait()
        listing = asyncio.create_task(model_catalog.discover_models(provider, ["manual"]))
        release.set()
        self.assertEqual(await metadata, {"context_window": 64000})
        self.assertEqual(await listing, ["manual", "model"])
        self.assertEqual(provider.opened, 1)

    async def test_metadata_deadline_unbound_closed_and_cancelled_paths(self):
        unbound = CatalogProvider([])
        unbound._oauth_shared_state.replace({})
        self.assertEqual(await model_catalog.get_model_context_metadata(unbound, "model"), {})
        self.assertEqual(unbound.opened, 0)
        unbound._oauth_media_closed = True
        with self.assertRaises(RuntimeError):
            await model_catalog.get_model_context_metadata(unbound, "model")

        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def block():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        provider = CatalogProvider([CatalogResponse({"models": []}, before_read=block)])
        with patch.object(model_catalog, "CATALOG_TIMEOUT_SECONDS", 0.01):
            self.assertEqual(await model_catalog.get_model_context_metadata(provider, "model"), {})
        self.assertTrue(cleaned.is_set())

        entered.clear()
        cleaned.clear()
        cancelled = CatalogProvider([CatalogResponse({"models": []}, before_read=block)])
        task = asyncio.create_task(model_catalog.get_model_context_metadata(cancelled, "model"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())


if __name__ == "__main__":
    unittest.main()
