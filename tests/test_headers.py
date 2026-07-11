import base64
import json
import unittest


def _encode_test_jwt(claims):
    def encode_part(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode_part({'alg': 'none'})}.{encode_part(claims)}.signature"


class CodexBackendHeaderTests(unittest.TestCase):
    def _build_headers(self, claims, custom_headers=None):
        from oauth_plug_openai_codex.headers import build_codex_backend_headers

        return build_codex_backend_headers(
            _encode_test_jwt(claims),
            "account-id",
            custom_headers=custom_headers,
        )

    def test_nested_data_residency_is_forwarded(self):
        headers = self._build_headers(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_data_residency": "data-region",
                }
            }
        )

        self.assertEqual(
            headers["x-openai-internal-codex-residency"],
            "data-region",
        )

    def test_nested_compute_residency_is_forwarded(self):
        headers = self._build_headers(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_compute_residency": "compute-region",
                }
            }
        )

        self.assertEqual(
            headers["x-openai-internal-codex-residency"],
            "compute-region",
        )

    def test_top_level_residency_is_used_as_fallback(self):
        headers = self._build_headers(
            {
                "chatgpt_data_residency": "top-level-region",
            }
        )

        self.assertEqual(
            headers["x-openai-internal-codex-residency"],
            "top-level-region",
        )

    def test_custom_headers_override_generated_headers(self):
        headers = self._build_headers(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_data_residency": "jwt-region",
                }
            },
            {
                "version": "custom-version",
                "User-Agent": "custom-agent",
                "x-openai-internal-codex-residency": "custom-region",
                "X-Custom": 42,
            },
        )

        self.assertEqual(headers["version"], "custom-version")
        self.assertEqual(headers["User-Agent"], "custom-agent")
        self.assertEqual(
            headers["x-openai-internal-codex-residency"],
            "custom-region",
        )
        self.assertEqual(headers["X-Custom"], "42")
