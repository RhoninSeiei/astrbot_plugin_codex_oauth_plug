from __future__ import annotations

import copy
from typing import Any

from .service import OAUTH_PLACEHOLDER_KEY, PROVIDER_TYPE

PROVIDER_TEMPLATE_NAME = "Codex OAuth 插件 / OpenAI"

DEFAULT_PROVIDER_CONFIG: dict[str, Any] = {
    "id": "oauth_plug_openai_codex",
    "type": PROVIDER_TYPE,
    "enable": False,
    "provider": "openai",
    "provider_type": "chat_completion",
    "model": "gpt-5.6-sol",
    "key": [OAUTH_PLACEHOLDER_KEY],
    "api_base": "https://chatgpt.com/backend-api/codex",
    "proxy": "",
    "timeout": 120,
    "custom_headers": {},
    "custom_extra_body": {},
}


def _get_dashboard_provider_templates() -> dict[str, Any] | None:
    try:
        from astrbot.core.config.default import CONFIG_METADATA_2
    except Exception:
        return None

    try:
        templates = CONFIG_METADATA_2["provider_group"]["metadata"]["provider"][
            "config_template"
        ]
    except Exception:
        return None

    return templates if isinstance(templates, dict) else None


def _inject_dashboard_provider_template() -> None:
    templates = _get_dashboard_provider_templates()
    if templates is None:
        return
    templates[PROVIDER_TEMPLATE_NAME] = copy.deepcopy(DEFAULT_PROVIDER_CONFIG)


def _remove_dashboard_provider_template() -> None:
    templates = _get_dashboard_provider_templates()
    if templates is None:
        return
    for name in (PROVIDER_TEMPLATE_NAME, "OAuth_plug OpenAI Codex OAuth"):
        template = templates.get(name)
        if isinstance(template, dict) and template.get("type") == PROVIDER_TYPE:
            templates.pop(name, None)


def register_provider_adapter() -> None:
    from astrbot.core.provider.entities import ProviderMetaData, ProviderType
    from astrbot.core.provider.register import provider_cls_map, provider_registry

    from .provider import ProviderOAuthPlugOpenAICodex

    unregister_provider_adapter()
    _inject_dashboard_provider_template()
    metadata = ProviderMetaData(
        id="default",
        model=None,
        type=PROVIDER_TYPE,
        desc="Codex OAuth 插件：使用已绑定的 OpenAI 账号",
        provider_type=ProviderType.CHAT_COMPLETION,
        cls_type=ProviderOAuthPlugOpenAICodex,
        default_config_tmpl=copy.deepcopy(DEFAULT_PROVIDER_CONFIG),
        provider_display_name=PROVIDER_TEMPLATE_NAME,
    )
    provider_registry.append(metadata)
    provider_cls_map[PROVIDER_TYPE] = metadata


def unregister_provider_adapter() -> None:
    from astrbot.core.provider.register import provider_cls_map, provider_registry

    provider_cls_map.pop(PROVIDER_TYPE, None)
    provider_registry[:] = [
        metadata for metadata in provider_registry if metadata.type != PROVIDER_TYPE
    ]
    _remove_dashboard_provider_template()
