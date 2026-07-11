from __future__ import annotations

from typing import Any

from .oauth import decode_jwt_claims

CODEX_CLIENT_VERSION = "0.144.0"
OPENAI_AUTH_CLAIM_PATH = "https://api.openai.com/auth"


def _extract_residency(access_token: str) -> str:
    claims = decode_jwt_claims(access_token)
    auth_claims = claims.get(OPENAI_AUTH_CLAIM_PATH)
    if isinstance(auth_claims, dict):
        residency = str(
            auth_claims.get("chatgpt_data_residency")
            or auth_claims.get("chatgpt_compute_residency")
            or ""
        ).strip()
        if residency:
            return residency
    return str(
        claims.get("chatgpt_data_residency")
        or claims.get("chatgpt_compute_residency")
        or ""
    ).strip()


def build_codex_backend_headers(
    access_token: str,
    account_id: str,
    *,
    custom_headers: dict[str, Any] | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "version": CODEX_CLIENT_VERSION,
        "User-Agent": f"codex_cli_rs/{CODEX_CLIENT_VERSION}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    residency = _extract_residency(access_token)
    if residency:
        headers["x-openai-internal-codex-residency"] = residency
    if isinstance(custom_headers, dict):
        for key, value in custom_headers.items():
            headers[str(key)] = str(value)
    return headers
