"""Final payload protection on the standalone provider in ordinary AstrBot."""
import copy
from unittest.mock import AsyncMock

import pytest
from tests.test_provider_stream_core import _make_provider
from oauth_plug_openai_codex.context_budget import ContextBudgetExceeded, resolve_context_budget


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_protected_oversize_input_never_reaches_transport(stream):
    provider = _make_provider({"max_context_tokens": 1024})
    provider.get_model_context_metadata = AsyncMock(return_value={"context_window": 1024})
    provider._request_backend = AsyncMock()
    entered = []
    async def events(params):
        entered.append(params)
        yield {}
    provider._stream_backend_events = events
    try:
        with pytest.raises(ContextBudgetExceeded):
            if stream:
                async for _ in provider._query_stream({"messages": [{"role": "user", "content": "x" * 10000}]}, None):
                    pass
            else:
                await provider._query({"messages": [{"role": "user", "content": "x" * 10000}]}, None)
        provider._request_backend.assert_not_awaited()
        assert not entered
    finally:
        await provider.terminate()


@pytest.mark.asyncio
async def test_trims_old_turn_only_and_retains_raw_response_usage():
    provider = _make_provider({"max_context_tokens": 1024})
    provider.get_model_context_metadata = AsyncMock(return_value={"context_window": 1024})
    provider._request_backend = AsyncMock(return_value={"id": "r1", "output_text": "ok", "usage": {"input_tokens": 7, "output_tokens": 0}})
    payload = {"messages": [{"role": "system", "content": "protect"}, {"role": "user", "content": "x" * 10000}, {"role": "assistant", "content": "old"}, {"role": "user", "content": "latest"}]}
    original = copy.deepcopy(payload)
    try:
        response = await provider._query(payload, None)
        sent = provider._request_backend.await_args.args[0]
        assert "x" * 10000 not in str(sent)
        assert "latest" in str(sent) and "protect" in str(sent)
        assert payload == original
        assert response.usage.input_other == 7 and response.usage.output == 0
    finally:
        await provider.terminate()


@pytest.mark.asyncio
async def test_framework_writeback_does_not_replace_saved_auto_mode():
    provider = _make_provider({"max_context_tokens": 0})
    provider.get_model_context_metadata = AsyncMock(return_value={"context_window": 272000, "max_context_window": 872000})
    provider.provider_config["max_context_tokens"] = 1050000
    try:
        budget = await resolve_context_budget(provider)
        assert budget.context_window == 272000
        assert provider.provider_config["max_context_tokens"] == 1050000
    finally:
        await provider.terminate()


@pytest.mark.asyncio
async def test_stream_close_keeps_usage_sink_until_transport_cleanup():
    from astrbot.core.provider.entities import LLMResponse
    from oauth_plug_openai_codex.provider_runtime_compat import current_attempt_ledger
    provider = _make_provider()
    provider._record_provider_stat = AsyncMock()
    async def stream(*args, **kwargs):
        try:
            response = LLMResponse("assistant")
            response.is_chunk = True
            yield response
        finally:
            provider._observe_backend_usage({"id": "cleanup", "usage": {"input_tokens": 7, "output_tokens": 3}}, complete=True)
    provider._query_stream = stream
    iterator = provider.text_chat_stream(prompt="test")
    try:
        await anext(iterator)
        assert current_attempt_ledger.get() is None
        await iterator.aclose()
        usage = provider._record_provider_stat.await_args.kwargs["usage"]
        assert (usage.input_other, usage.output) == (7, 3)
        assert current_attempt_ledger.get() is None
    finally:
        await provider.terminate()
