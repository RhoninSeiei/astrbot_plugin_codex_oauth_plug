import asyncio
import unittest
from unittest.mock import patch

from oauth_plug_openai_codex.service import (
    OAUTH_PLACEHOLDER_KEY,
    PROVIDER_TYPE,
    OpenAICodexOAuthService,
)


class FakeConfig(dict):
    def __init__(self):
        super().__init__()
        self.saved_payloads = []

    def save_config(self, payload=None):
        if payload is not None:
            self.clear()
            self.update(payload)
        self.saved_payloads.append(dict(self))


class ServiceTests(unittest.TestCase):
    def test_service_builds_provider_config_with_oauth_plug_prefix(self):
        config = FakeConfig()
        config.update(
            {
                "base_url": "https://chatgpt.com/backend-api/codex",
                "proxy": "http://127.0.0.1:7890",
                "default_model": "gpt-5.5",
                "oauth_access_token": "access",
                "oauth_refresh_token": "refresh",
                "expires_at": "2026-05-17T16:46:53+00:00",
                "oauth_account_id": "account",
                "oauth_account_email": "codex@example.com",
                "oauth_refresh_skew_seconds": 300,
            }
        )
        service = OpenAICodexOAuthService(config)

        provider_config = service.build_provider_config({"id": "demo/gpt-5.5"})

        self.assertEqual(PROVIDER_TYPE, "oauth_plug_openai_codex_chat_completion")
        self.assertEqual(provider_config["type"], PROVIDER_TYPE)
        self.assertEqual(provider_config["key"], [OAUTH_PLACEHOLDER_KEY])
        self.assertEqual(provider_config["model"], "gpt-5.5")
        self.assertEqual(provider_config["oauth_access_token"], "access")
        self.assertEqual(provider_config["oauth_account_id"], "account")

    def test_service_persists_oauth_token_fields(self):
        config = FakeConfig()
        service = OpenAICodexOAuthService(config)

        asyncio.run(
            service.persist_token(
                {
                    "access_token": "access-2",
                    "refresh_token": "refresh-2",
                    "expires_at": "2026-05-17T16:46:53+00:00",
                    "email": "codex@example.com",
                    "account_id": "account-2",
                }
            )
        )

        self.assertEqual(config["oauth_access_token"], "access-2")
        self.assertEqual(config["oauth_refresh_token"], "refresh-2")
        self.assertEqual(config["oauth_expires_at"], "2026-05-17T16:46:53+00:00")
        self.assertEqual(config["oauth_account_email"], "codex@example.com")
        self.assertEqual(config["oauth_account_id"], "account-2")
        self.assertEqual(config.saved_payloads[-1]["oauth_access_token"], "access-2")

    def test_service_supports_grouped_plugin_config_schema(self):
        config = FakeConfig()
        config.update(
            {
                "runtime": {
                    "base_url": "https://chatgpt.com/backend-api/codex",
                    "proxy": "http://127.0.0.1:7890",
                    "models": "gpt-5.5\ngpt-5.4\ngpt-5.5\n",
                    "enabled": True,
                },
                "oauth": {
                    "oauth_access_token": "access",
                    "oauth_refresh_token": "refresh",
                    "oauth_expires_at": "2026-05-17T16:46:53+00:00",
                    "oauth_account_id": "account",
                },
                "advanced": {"generated_image_dir": "/tmp/images"},
            }
        )
        service = OpenAICodexOAuthService(config)

        provider_config = service.build_provider_config({"id": "demo/gpt-5.5"})

        self.assertEqual(provider_config["api_base"], "https://chatgpt.com/backend-api/codex")
        self.assertEqual(provider_config["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(provider_config["model"], "gpt-5.5")
        self.assertEqual(provider_config["oauth_access_token"], "access")
        self.assertEqual(provider_config["oauth_account_id"], "account")
        self.assertEqual(provider_config["generated_image_dir"], "/tmp/images")

    def test_service_reads_model_list_with_default_fallback(self):
        service = OpenAICodexOAuthService(
            {
                "runtime": {
                    "default_model": "gpt-5.4",
                    "models": "gpt-5.5\n gpt-5.4 \n\n gpt-5.5",
                }
            }
        )

        self.assertEqual(service.get_models(), ["gpt-5.5", "gpt-5.4"])
        self.assertEqual(service.get_default_model(), "gpt-5.5")

    def test_service_uses_default_model_when_model_list_is_empty(self):
        service = OpenAICodexOAuthService(
            {
                "runtime": {
                    "default_model": "gpt-5.4",
                    "models": "",
                }
            }
        )

        self.assertEqual(service.get_models(), ["gpt-5.4"])
        self.assertEqual(service.get_default_model(), "gpt-5.4")

    def test_service_test_connection_posts_minimal_responses_request(self):
        calls = []

        class FakeResponse:
            status_code = 200

            async def aread(self):
                return (
                    b'data: {"type":"response.completed","response":'
                    b'{"id":"resp_test","output_text":"ok"}}\n\n'
                )

        class FakeClient:
            def __init__(self, **kwargs):
                calls.append(("client", kwargs))

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers, json):
                calls.append(("post", url, headers, json))
                return FakeResponse()

        config = {
            "runtime": {
                "base_url": "https://chatgpt.com/backend-api/codex",
                "proxy": "http://127.0.0.1:7890",
                "models": "gpt-5.5\ngpt-5.4",
            },
            "oauth": {
                "oauth_access_token": "access-token",
                "oauth_account_id": "account-id",
            },
        }
        service = OpenAICodexOAuthService(config)

        with patch(
            "oauth_plug_openai_codex.service.httpx.AsyncClient",
            FakeClient,
        ), patch(
            "oauth_plug_openai_codex.service.time.perf_counter",
            side_effect=[10.0, 10.321],
        ):
            result = asyncio.run(service.test_connection())

        self.assertEqual(result["model"], "gpt-5.5")
        self.assertEqual(result["latency_ms"], 321)
        self.assertEqual(result["response_id"], "resp_test")
        self.assertEqual(calls[0][1]["proxy"], "http://127.0.0.1:7890")
        post = calls[1]
        self.assertEqual(post[1], "https://chatgpt.com/backend-api/codex/responses")
        self.assertEqual(post[2]["Authorization"], "Bearer access-token")
        self.assertEqual(post[2]["chatgpt-account-id"], "account-id")
        self.assertEqual(post[3]["model"], "gpt-5.5")
