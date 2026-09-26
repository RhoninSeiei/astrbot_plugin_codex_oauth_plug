"""Read-only account information with explicit event permissions."""

OAUTH_PROVIDER_TYPES = {
    "openai_oauth_chat_completion",
    "oauth_plug_openai_codex_chat_completion",
}


class UsageToolService:
    def __init__(self, context, config, reader):
        self.context = context
        self.config = config
        self.reader = reader
        self.closed = False

    def _check(self, event):
        if self.closed:
            return "closed"
        settings = self.config.get("usage", {})
        if not isinstance(settings, dict) or settings.get("enabled", True) is not True:
            return "disabled"
        if not event.is_admin():
            return "denied"
        if not event.is_private_chat():
            allowed = settings.get("group_allowlist", [])
            if not isinstance(allowed, list) or event.unified_msg_origin not in allowed:
                return "denied"
        return None

    async def run(self, event):
        try:
            if status := self._check(event):
                return {"status": status}
            target = str(self.config.get("usage", {}).get("provider_id") or "").strip()
            if target:
                provider = self.context.get_provider_by_id(target)
                if provider is None:
                    return {"status": "provider_not_found"}
            else:
                provider = await self.context.get_using_provider_async(event.unified_msg_origin)
            if status := self._check(event):
                return {"status": status}
            config = getattr(provider, "provider_config", {})
            if not isinstance(config, dict) or config.get("type") not in OAUTH_PROVIDER_TYPES:
                return {"status": "not_oauth_provider"}
            result = await self.reader.read(provider)
            if status := self._check(event):
                return {"status": status}
            if str(self.config.get("usage", {}).get("provider_id") or "").strip() != target:
                return {"status": "authorization_changed"}
            return result
        except Exception:
            # Account identifiers and upstream bodies must never become tool output.
            return {"status": "unavailable"}

    async def close(self):
        self.closed = True
        await self.reader.close()
