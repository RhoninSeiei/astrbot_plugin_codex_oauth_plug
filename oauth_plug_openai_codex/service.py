from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .oauth import create_pkce_flow, exchange_authorization_code, refresh_access_token

PROVIDER_TYPE = "oauth_plug_openai_codex_chat_completion"
OAUTH_PLACEHOLDER_KEY = "__oauth_plug_openai_codex__"
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_MODELS = (DEFAULT_MODEL,)

_SERVICE: OpenAICodexOAuthService | None = None


class OpenAICodexOAuthService:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._flows: dict[str, dict[str, Any]] = {}

    def _section(self, name: str) -> dict[str, Any]:
        value = self.config.get(name)
        return value if isinstance(value, dict) else {}

    def _get_config_value(self, key: str, section: str, default: Any = "") -> Any:
        section_value = self._section(section).get(key)
        if section_value not in (None, ""):
            return section_value
        root_value = self.config.get(key)
        if root_value not in (None, ""):
            return root_value
        return default

    def _set_config_value(self, key: str, value: Any, section: str) -> None:
        if section in self.config and isinstance(self.config.get(section), dict):
            self.config[section][key] = value
            return
        self.config[key] = value

    def get_proxy(self) -> str:
        return str(self._get_config_value("proxy", "runtime")).strip()

    def get_base_url(self) -> str:
        return str(
            self._get_config_value("base_url", "runtime", DEFAULT_BASE_URL)
        ).strip().rstrip("/")

    def _get_legacy_default_model(self) -> str:
        return str(
            self._get_config_value("default_model", "runtime", DEFAULT_MODEL)
        ).strip()

    def get_models(self) -> list[str]:
        raw_models = self._get_config_value("models", "runtime", "")
        models: list[str] = []
        if isinstance(raw_models, str):
            raw_parts = raw_models.replace(",", "\n").splitlines()
        elif isinstance(raw_models, list):
            raw_parts = raw_models
        else:
            raw_parts = []

        seen: set[str] = set()
        for raw_part in raw_parts:
            model = str(raw_part or "").strip()
            if not model or model in seen:
                continue
            seen.add(model)
            models.append(model)

        if models:
            return models

        fallback = self._get_legacy_default_model()
        return [fallback] if fallback else list(DEFAULT_MODELS)

    def get_default_model(self) -> str:
        models = self.get_models()
        return models[0] if models else DEFAULT_MODEL

    def is_enabled(self) -> bool:
        return bool(self._get_config_value("enabled", "runtime", True))

    def get_authorization_input(self) -> str:
        return str(self._get_config_value("authorization_input", "oauth")).strip()

    def set_oauth_config_value(self, key: str, value: Any) -> None:
        self._set_config_value(key, value, "oauth")

    def build_provider_config(self, provider_config: dict[str, Any]) -> dict[str, Any]:
        merged = dict(provider_config)
        merged["type"] = PROVIDER_TYPE
        merged["provider"] = "openai"
        merged["provider_type"] = "chat_completion"
        merged["key"] = [OAUTH_PLACEHOLDER_KEY]
        merged["api_base"] = self.get_base_url()
        merged["proxy"] = str(merged.get("proxy") or self.get_proxy())
        merged["model"] = str(merged.get("model") or self.get_default_model())
        for key in (
            "oauth_access_token",
            "oauth_refresh_token",
            "oauth_expires_at",
            "oauth_account_email",
            "oauth_account_id",
            "oauth_refresh_skew_seconds",
        ):
            value = self._get_config_value(key, "oauth")
            if value not in (None, ""):
                merged[key] = value
        image_dir = self._get_config_value("generated_image_dir", "advanced")
        if image_dir:
            merged["generated_image_dir"] = image_dir
        return merged

    def create_flow(self, flow_id: str = "default") -> dict[str, str]:
        flow = create_pkce_flow()
        self._flows[flow_id] = flow
        return flow

    async def complete_flow(self, auth_input: str, flow_id: str = "default") -> dict:
        from .oauth import parse_authorization_input, parse_oauth_credential_json

        token = parse_oauth_credential_json(auth_input)
        if token is None:
            flow = self._flows.get(flow_id)
            if not flow:
                raise ValueError("OAuth 流程未开始或已过期")
            code, state = parse_authorization_input(auth_input)
            if not code:
                raise ValueError("缺少授权码")
            if not state:
                raise ValueError("缺少 state")
            if state != flow.get("state"):
                raise ValueError("state 不匹配")
            token = await exchange_authorization_code(
                code,
                str(flow.get("verifier") or ""),
                self.get_proxy(),
            )
        await self.persist_token(token)
        self._flows.pop(flow_id, None)
        return token

    async def refresh(self) -> dict[str, Any]:
        refresh_token_value = str(
            self._get_config_value("oauth_refresh_token", "oauth")
        ).strip()
        if not refresh_token_value:
            raise ValueError("当前配置没有可用的 refresh token")
        token = await refresh_access_token(refresh_token_value, self.get_proxy())
        await self.persist_token(token)
        return token

    async def persist_token(self, token: dict[str, Any]) -> None:
        updates = {
            "auth_mode": "openai_oauth",
            "oauth_provider": "openai",
            "oauth_access_token": str(token.get("access_token") or ""),
            "oauth_refresh_token": str(token.get("refresh_token") or ""),
            "oauth_expires_at": str(token.get("expires_at") or ""),
            "oauth_account_email": str(
                token.get("email")
                or self._get_config_value("oauth_account_email", "oauth")
                or ""
            ),
            "oauth_account_id": str(
                token.get("account_id")
                or self._get_config_value("oauth_account_id", "oauth")
                or ""
            ),
        }
        for key, value in updates.items():
            self._set_config_value(key, value, "oauth")
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config(dict(self.config))

    async def test_connection(self, model: str | None = None) -> dict[str, Any]:
        target_model = (model or self.get_default_model()).strip()
        if not target_model:
            raise ValueError("缺少用于测试的模型 ID")

        started_at = time.perf_counter()
        status_code, text = await self._request_test_backend_once(target_model)
        if status_code in {401, 403} and self._get_config_value(
            "oauth_refresh_token", "oauth"
        ):
            await self.refresh()
            status_code, text = await self._request_test_backend_once(target_model)

        latency_ms = max(0, round((time.perf_counter() - started_at) * 1000))
        if status_code < 200 or status_code >= 300:
            raise ValueError(self._format_backend_error(status_code, text))

        parsed = self._parse_test_backend_response(text)
        return {
            "model": target_model,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "response_id": parsed.get("response_id", ""),
            "output_text": parsed.get("output_text", ""),
        }

    async def _request_test_backend_once(self, model: str) -> tuple[int, str]:
        access_token = str(
            self._get_config_value("oauth_access_token", "oauth")
        ).strip()
        account_id = str(self._get_config_value("oauth_account_id", "oauth")).strip()
        if not access_token:
            raise ValueError("当前 OAuth_plug 配置尚未绑定 access token")
        if not account_id:
            raise ValueError("当前 OAuth_plug 配置缺少 chatgpt_account_id")

        payload: dict[str, Any] = {
            "model": model,
            "instructions": "请只回复 ok",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "ping"}],
                }
            ],
            "stream": True,
            "store": False,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        async with httpx.AsyncClient(
            proxy=self.get_proxy() or None,
            timeout=30.0,
            follow_redirects=True,
        ) as client:
            response = await client.post(
                f"{self.get_base_url()}/responses",
                headers=headers,
                json=payload,
            )
            raw_text = await response.aread()
        return response.status_code, raw_text.decode("utf-8", errors="replace")

    def _parse_test_backend_response(self, text: str) -> dict[str, str]:
        output_text_parts: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                event = json.loads(raw)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "response.output_text.delta" and event.get(
                "delta"
            ):
                output_text_parts.append(str(event["delta"]))
            if event.get("type") == "response.completed":
                response = event.get("response")
                if isinstance(response, dict):
                    return {
                        "response_id": str(response.get("id") or ""),
                        "output_text": str(
                            response.get("output_text")
                            or "".join(output_text_parts)
                        ),
                    }
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
            except Exception:
                return {"response_id": "", "output_text": "".join(output_text_parts)}
            if isinstance(data, dict):
                response = data.get("response") if data.get("response") else data
                if isinstance(response, dict):
                    return {
                        "response_id": str(response.get("id") or ""),
                        "output_text": str(
                            response.get("output_text")
                            or "".join(output_text_parts)
                        ),
                    }
        return {"response_id": "", "output_text": "".join(output_text_parts)}

    def _format_backend_error(self, status_code: int, text: str) -> str:
        body = text.strip()
        token = str(self._get_config_value("oauth_access_token", "oauth")).strip()
        if token:
            body = body.replace(token, "[REDACTED]")
        if len(body) > 600:
            body = f"{body[:600]}..."
        if not body:
            return f"Codex backend 测试失败: status={status_code}"
        return f"Codex backend 测试失败: status={status_code}, body={body}"

    def disconnect(self) -> None:
        updates = {
            "auth_mode": "manual",
            "oauth_provider": "",
            "oauth_access_token": "",
            "oauth_refresh_token": "",
            "oauth_expires_at": "",
            "oauth_account_email": "",
            "oauth_account_id": "",
        }
        if isinstance(self.config.get("oauth"), dict):
            self.config["oauth"].update(updates)
        else:
            self.config.update(updates)
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config(dict(self.config))


def set_service(service: OpenAICodexOAuthService | None) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> OpenAICodexOAuthService | None:
    return _SERVICE
