import asyncio
import unittest
from unittest.mock import patch

import test_provider as provider_helpers

provider_helpers._install_fake_astrbot_runtime()


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _fake_client_class(lines):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, *args, **kwargs):
            return _FakeStreamResponse(lines)

    return FakeClient


class SseMalformedLineCountingTests(unittest.TestCase):
    def run_scan(self, lines):
        provider = provider_helpers.ProviderImageGenerationTests()._make_provider(
            "unused"
        )
        provider._build_backend_headers = lambda: {"X-Test": "yes"}
        with patch(
            "oauth_plug_openai_codex.provider.httpx.AsyncClient",
            _fake_client_class(lines),
        ), patch(
            "oauth_plug_openai_codex.provider.logger.warning"
        ) as warning:
            status, _text, _version = asyncio.run(
                provider._request_image_backend_once({"stream": True}, 12.5)
            )
        return status, warning

    def test_malformed_lines_are_counted_and_warned_once(self):
        status, warning = self.run_scan(
            [
                'data: {"type":"response.output_text.delta","delta":"x"}',
                "data: {broken json",
                "data: [1,2,3]",
                ": comment line",
                "data: ",
                'data: {"type":"response.completed","response":{}}',
            ]
        )
        self.assertEqual(status, 200)
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], 2)

    def test_clean_stream_does_not_warn(self):
        status, warning = self.run_scan(
            [
                'data: {"type":"response.output_text.delta","delta":"x"}',
                'data: {"type":"response.completed","response":{}}',
            ]
        )
        self.assertEqual(status, 200)
        warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
