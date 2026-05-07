from __future__ import annotations

from typing import Any

from .service import OAUTH_PLACEHOLDER_KEY, PROVIDER_TYPE

DEFAULT_PROVIDER_CONFIG: dict[str, Any] = {
    "id": "oauth_plug_openai_codex/gpt-5.5",
    "type": PROVIDER_TYPE,
    "enable": False,
    "provider": "openai",
    "provider_type": "chat_completion",
    "model": "gpt-5.5",
    "key": [OAUTH_PLACEHOLDER_KEY],
    "api_base": "https://chatgpt.com/backend-api/codex",
    "proxy": "",
}


def register_provider_adapter() -> None:
    from astrbot.core.provider.entities import ProviderMetaData
    from astrbot.core.provider.register import provider_cls_map, provider_registry
    from astrbot.core.provider.entities import ProviderType

    from .provider import ProviderOAuthPlugOpenAICodex

    unregister_provider_adapter()
    metadata = ProviderMetaData(
        id="default",
        model=None,
        type=PROVIDER_TYPE,
        desc="OAuth_plug OpenAI Codex OAuth 提供商适配器",
        provider_type=ProviderType.CHAT_COMPLETION,
        cls_type=ProviderOAuthPlugOpenAICodex,
        default_config_tmpl=dict(DEFAULT_PROVIDER_CONFIG),
        provider_display_name="OAuth_plug OpenAI Codex OAuth",
    )
    provider_registry.append(metadata)
    provider_cls_map[PROVIDER_TYPE] = metadata


def unregister_provider_adapter() -> None:
    from astrbot.core.provider.register import provider_cls_map, provider_registry

    provider_cls_map.pop(PROVIDER_TYPE, None)
    provider_registry[:] = [
        metadata for metadata in provider_registry if metadata.type != PROVIDER_TYPE
    ]

