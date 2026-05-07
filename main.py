from __future__ import annotations

from typing import Any

from quart import request

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

try:
    from .oauth_plug_openai_codex.registration import (
        register_provider_adapter,
        unregister_provider_adapter,
    )
    from .oauth_plug_openai_codex.service import (
        OpenAICodexOAuthService,
        get_service,
        set_service,
    )
except ImportError:
    from oauth_plug_openai_codex.registration import (
        register_provider_adapter,
        unregister_provider_adapter,
    )
    from oauth_plug_openai_codex.service import (
        OpenAICodexOAuthService,
        get_service,
        set_service,
    )


class OAuthPlugOpenAICodexPlugin(Star):
    """OpenAI Codex OAuth provider plugin."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.service = OpenAICodexOAuthService(self.config)

    async def initialize(self) -> None:
        set_service(self.service)
        if self.service.is_enabled():
            register_provider_adapter()
        self.context.register_web_api(
            "oauth-plug-openai-codex/start",
            self.api_start,
            ["POST"],
            "创建 OpenAI Codex OAuth PKCE 授权地址",
        )
        self.context.register_web_api(
            "oauth-plug-openai-codex/complete",
            self.api_complete,
            ["POST"],
            "完成 OpenAI Codex OAuth 绑定",
        )
        self.context.register_web_api(
            "oauth-plug-openai-codex/refresh",
            self.api_refresh,
            ["POST"],
            "刷新 OpenAI Codex OAuth token",
        )
        self.context.register_web_api(
            "oauth-plug-openai-codex/test",
            self.api_test,
            ["POST"],
            "测试 OpenAI Codex OAuth provider 连接",
        )
        self.context.register_web_api(
            "oauth-plug-openai-codex/disconnect",
            self.api_disconnect,
            ["POST"],
            "清除 OpenAI Codex OAuth 绑定信息",
        )

    async def terminate(self) -> None:
        if get_service() is self.service:
            set_service(None)
        unregister_provider_adapter()

    def get_oauth_service(self) -> OpenAICodexOAuthService:
        return self.service

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("codex_oauth_start")
    async def command_start(self, event: AstrMessageEvent):
        """生成 Codex OAuth 授权地址"""
        flow = self._create_flow_and_save()
        yield event.plain_result(
            "Codex OAuth 授权地址已生成：\n"
            f"{flow['authorize_url']}\n\n"
            "浏览器完成登录后，复制完整回调地址并发送：\n"
            "codex_oauth_complete <完整回调地址>"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("codex_oauth_complete")
    async def command_complete(self, event: AstrMessageEvent, auth_input: GreedyStr):
        """完成 Codex OAuth 绑定"""
        try:
            token = await self.service.complete_flow(str(auth_input), "default")
        except Exception as exc:
            yield event.plain_result(f"Codex OAuth 绑定失败：{exc}")
            return
        self.service.set_oauth_config_value("authorization_input", "")
        self.service.set_oauth_config_value("last_authorize_url", "")
        self.service.set_oauth_config_value("last_state", "")
        self._save_config()
        yield event.plain_result(
            "Codex OAuth 绑定成功。\n"
            f"账号邮箱：{token.get('email', '') or '未返回'}\n"
            f"过期时间：{token.get('expires_at', '') or '未返回'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("codex_oauth_refresh")
    async def command_refresh(self, event: AstrMessageEvent):
        """刷新 Codex OAuth token"""
        try:
            token = await self.service.refresh()
        except Exception as exc:
            yield event.plain_result(f"Codex OAuth 令牌刷新失败：{exc}")
            return
        yield event.plain_result(
            "Codex OAuth 令牌刷新成功。\n"
            f"账号邮箱：{token.get('email', '') or '未返回'}\n"
            f"过期时间：{token.get('expires_at', '') or '未返回'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("codex_oauth_test")
    async def command_test(self, event: AstrMessageEvent, model: str = ""):
        """测试 Codex OAuth provider 连接"""
        try:
            result = await self.service.test_connection(model or None)
        except Exception as exc:
            yield event.plain_result(f"Codex OAuth 连接测试失败：{exc}")
            return
        yield event.plain_result(
            "Codex OAuth 连接测试通过。\n"
            f"模型：{result['model']}\n"
            f"端侧延迟：{result['latency_ms']} ms"
        )

    async def api_start(self) -> dict[str, Any]:
        flow = self._create_flow_and_save()
        return {
            "status": "ok",
            "message": "OAuth_plug 授权地址已生成",
            "data": {
                "authorize_url": flow["authorize_url"],
                "state": flow["state"],
            },
        }

    async def api_complete(self) -> dict[str, Any]:
        data = await request.get_json(silent=True) or {}
        auth_input = str(data.get("input") or self.service.get_authorization_input()).strip()
        if not auth_input:
            return {
                "status": "error",
                "message": "缺少授权回调 URL、code#state 或 JSON 凭据",
            }
        try:
            token = await self.service.complete_flow(auth_input, "default")
        except Exception as exc:
            return {"status": "error", "message": f"OAuth_plug 绑定失败: {exc}"}
        self.service.set_oauth_config_value("authorization_input", "")
        self.service.set_oauth_config_value("last_authorize_url", "")
        self.service.set_oauth_config_value("last_state", "")
        self._save_config()
        return {
            "status": "ok",
            "message": "OAuth_plug 绑定成功",
            "data": {
                "email": token.get("email", ""),
                "expires_at": token.get("expires_at", ""),
                "account_id": token.get("account_id", ""),
            },
        }

    async def api_refresh(self) -> dict[str, Any]:
        try:
            token = await self.service.refresh()
        except Exception as exc:
            return {"status": "error", "message": f"OAuth_plug 刷新失败: {exc}"}
        return {
            "status": "ok",
            "message": "OAuth_plug 刷新成功",
            "data": {
                "email": token.get("email", ""),
                "expires_at": token.get("expires_at", ""),
                "account_id": token.get("account_id", ""),
            },
        }

    async def api_test(self) -> dict[str, Any]:
        data = await request.get_json(silent=True) or {}
        model = str(data.get("model") or "").strip() or None
        try:
            result = await self.service.test_connection(model)
        except Exception as exc:
            return {"status": "error", "message": f"OAuth_plug 测试失败: {exc}"}
        return {
            "status": "ok",
            "message": (
                f"OAuth_plug 测试通过，模型 {result['model']} 延迟 "
                f"{result['latency_ms']} ms"
            ),
            "data": result,
        }

    async def api_disconnect(self) -> dict[str, Any]:
        self.service.disconnect()
        return {
            "status": "ok",
            "message": "OAuth_plug 绑定信息已清除",
            "data": {},
        }

    def _save_config(self) -> None:
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config(dict(self.config))

    def _create_flow_and_save(self) -> dict[str, str]:
        flow = self.service.create_flow("default")
        self.service.set_oauth_config_value("last_authorize_url", flow["authorize_url"])
        self.service.set_oauth_config_value("last_state", flow["state"])
        self._save_config()
        return flow
