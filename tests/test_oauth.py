import unittest
from urllib.parse import parse_qs, urlparse

from oauth_plug_openai_codex.oauth import (
    OPENAI_OAUTH_CLIENT_ID,
    OPENAI_OAUTH_REDIRECT_URI,
    build_authorize_url,
    create_pkce_flow,
    parse_authorization_input,
    parse_oauth_credential_json,
)


class OAuthTests(unittest.TestCase):
    def test_create_pkce_flow_uses_codex_oauth_parameters(self):
        flow = create_pkce_flow()

        self.assertTrue(flow["state"])
        self.assertTrue(flow["verifier"])
        self.assertTrue(flow["challenge"])
        self.assertNotEqual(flow["challenge"], flow["verifier"])

        parsed = urlparse(flow["authorize_url"])
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "auth.openai.com")
        self.assertEqual(query["client_id"], [OPENAI_OAUTH_CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [OPENAI_OAUTH_REDIRECT_URI])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [flow["challenge"]])
        self.assertEqual(query["state"], [flow["state"]])
        self.assertEqual(query["codex_cli_simplified_flow"], ["true"])
        self.assertEqual(query["originator"], ["codex_cli_rs"])

    def test_build_authorize_url_accepts_given_state_and_challenge(self):
        parsed = urlparse(build_authorize_url("state-1", "challenge-1"))
        query = parse_qs(parsed.query)

        self.assertEqual(query["state"], ["state-1"])
        self.assertEqual(query["code_challenge"], ["challenge-1"])

    def test_parse_authorization_input_supports_callback_url_and_code_state_pair(self):
        callback = "http://localhost:1455/auth/callback?code=abc&state=def"

        self.assertEqual(parse_authorization_input(callback), ("abc", "def"))
        self.assertEqual(parse_authorization_input("abc#def"), ("abc", "def"))

    def test_parse_oauth_credential_json_supports_codex_auth_json_shape(self):
        raw = """
        {
          "tokens": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "expires_at": "2026-05-17T16:46:53+00:00"
          },
          "account_id": "account-id",
          "email": "codex@example.com"
        }
        """

        token = parse_oauth_credential_json(raw)

        self.assertIsNotNone(token)
        self.assertEqual(token["access_token"], "access-token")
        self.assertEqual(token["refresh_token"], "refresh-token")
        self.assertEqual(token["expires_at"], "2026-05-17T16:46:53+00:00")
        self.assertEqual(token["account_id"], "account-id")
        self.assertEqual(token["email"], "codex@example.com")
