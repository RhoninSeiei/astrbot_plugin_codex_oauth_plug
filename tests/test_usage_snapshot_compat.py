import importlib.util
import sys
import types
import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch

import test_provider  # noqa: F401 - installs the optional fake host runtime
from astrbot.core.provider.entities import TokenUsage
from oauth_plug_openai_codex import provider_runtime_compat as compat


def fallback_compat():
    module = types.ModuleType("astrbot.core.provider.usage_recorder")
    name = "oauth_plug_openai_codex._usage_compat_without_core"
    with patch.dict(sys.modules, {module.__name__: module}):
        spec = importlib.util.spec_from_file_location(name, Path(compat.__file__))
        isolated = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(isolated)
    return isolated


def usage(input_count=0, output_count=0, *, partial=False):
    try:
        return TokenUsage(input_other=input_count, output=output_count, is_partial=partial)
    except TypeError:
        result = TokenUsage(input_other=input_count, output=output_count)
        result.is_partial = partial
        return result


class UsageSnapshotCompatTests(unittest.IsolatedAsyncioTestCase):
    def test_native_usage_sink_objects_are_reused_when_available(self):
        try:
            from astrbot.core.provider import usage_recorder
        except ImportError:
            self.skipTest("Requires source-aware core recorder")
        if not hasattr(usage_recorder, "observe_usage_snapshot"):
            self.skipTest("Requires source-aware core recorder")
        self.assertIs(compat.UsageAttemptLedger, usage_recorder.UsageAttemptLedger)
        self.assertIs(compat.current_attempt_ledger, usage_recorder.current_attempt_ledger)
        self.assertIs(compat.observe_usage_snapshot, usage_recorder.observe_usage_snapshot)
        self.assertIs(compat.observe_attempt_usage, usage_recorder.observe_attempt_usage)

    async def test_snapshot_is_not_sent_to_legacy_insert_only_recorder(self):
        calls = []

        async def legacy_record(provider, db, *, usage, status, request_kind, **kwargs):
            calls.append(kwargs)

        module = types.ModuleType("astrbot.core.provider.usage_recorder")
        module.record_usage = legacy_record
        with patch.dict(sys.modules, {module.__name__: module}):
            result = await compat.record_provider_usage(
                object(), object(), usage=None, status="completed", request_kind="realtime",
                replace_snapshot=True, usage_snapshot_fields=frozenset({"output"}),
            )
        self.assertFalse(result)
        self.assertEqual(calls, [])

    async def test_snapshot_fields_and_creation_context_reach_new_recorder(self):
        calls = []

        async def record(provider, db, *, usage, status, replace_snapshot=False,
                         usage_snapshot_fields=None, _context=None):
            calls.append((replace_snapshot, usage_snapshot_fields, _context))

        context = object()
        module = types.ModuleType("astrbot.core.provider.usage_recorder")
        module.record_usage = record
        with patch.dict(sys.modules, {module.__name__: module}):
            result = await compat.record_provider_usage(
                object(), object(), usage=None, status="completed", replace_snapshot=True,
                usage_snapshot_fields=frozenset({"input"}), _context=context,
            )
        self.assertTrue(result)
        self.assertEqual(calls, [(True, frozenset({"input"}), context)])

    def test_fallback_snapshots_replace_lower_values_and_preserve_directions(self):
        isolated = fallback_compat()
        ledger = isolated.UsageAttemptLedger(provider_key=12)
        token = isolated.current_attempt_ledger.set(ledger)
        try:
            isolated.observe_usage_snapshot(usage(0, 12, partial=True), response_id="same", reported_fields={"output"})
            isolated.observe_attempt_usage(usage(0, 4, partial=True), response_id="same", reported_fields={"output"})
            isolated.observe_usage_snapshot(usage(0, 0, partial=True), response_id="same", reported_fields={"input"})
            total = ledger.combined(None)
            self.assertEqual((total.input_other, total.output), (0, 4))
            self.assertTrue(total.is_partial)
            isolated.observe_usage_snapshot(usage(2, 3), response_id="same", complete=True, reported_fields={"input", "output"})
            isolated.observe_usage_snapshot(usage(0, 99, partial=True), response_id="same", reported_fields={"output"})
            total = ledger.combined(None)
            self.assertEqual((total.input_other, total.output), (2, 3))
            self.assertFalse(total.is_partial)
        finally:
            isolated.current_attempt_ledger.reset(token)

    def test_fallback_retry_totals_zero_missing_and_recorded_errors(self):
        isolated = fallback_compat()
        ledger = isolated.UsageAttemptLedger()
        token = isolated.current_attempt_ledger.set(ledger)
        try:
            self.assertIsNone(ledger.combined(None))
            isolated.observe_attempt_usage(usage(3, 1), complete=True)
            failure = RuntimeError("synthetic")
            isolated.note_failed_attempt(failure)
            isolated.note_failed_attempt(failure)
            ledger.begin_attempt()
            zero = usage()
            isolated.observe_attempt_usage(zero, complete=True)
            total = ledger.combined(zero)
            self.assertEqual((total.input_other, total.output), (3, 1))
            self.assertFalse(total.is_partial)
            ledger.begin_attempt()
            partial = ledger.combined(None)
            self.assertEqual((partial.input_other, partial.output), (3, 1))
            self.assertTrue(partial.is_partial)
            recorded = RuntimeError("already independently recorded")
            recorded._astrbot_usage_recorded = True
            recorded._astrbot_token_usage = usage(100, 100)
            isolated.note_failed_attempt(recorded)
            self.assertEqual(ledger.combined(None, recorded).input_other, 3)
        finally:
            isolated.current_attempt_ledger.reset(token)

    async def test_fallback_nested_concurrent_ledgers_are_isolated(self):
        isolated = fallback_compat()
        parent = isolated.UsageAttemptLedger()
        token = isolated.current_attempt_ledger.set(parent)

        async def child(value):
            ledger = isolated.UsageAttemptLedger(provider_key=value)
            child_token = isolated.current_attempt_ledger.set(ledger)
            try:
                await asyncio.sleep(0)
                isolated.observe_attempt_usage(usage(value, 0, partial=True), reported_fields={"input"})
                cancelled = asyncio.CancelledError()
                total = ledger.combined(None, cancelled)
                ledger.closed = True
                isolated.observe_attempt_usage(usage(999, 999), complete=True)
                self.assertEqual(ledger.combined(None).input_other, value)
                return total
            finally:
                isolated.current_attempt_ledger.reset(child_token)

        try:
            values = await asyncio.gather(child(2), child(7))
            self.assertEqual([item.input_other for item in values], [2, 7])
            self.assertIsNone(parent.observed_usage)
            self.assertIs(isolated.current_attempt_ledger.get(), parent)
            with isolated.usage_scope(plugin_id="synthetic"):
                self.assertIsNone(isolated.capture_usage_context())
        finally:
            isolated.current_attempt_ledger.reset(token)

    def test_local_budget_rejection_is_not_an_unknown_backend_attempt(self):
        from oauth_plug_openai_codex.context_budget import ContextBudgetExceeded
        isolated = fallback_compat()
        ledger = isolated.UsageAttemptLedger()
        ledger.note_failure(ContextBudgetExceeded("protected input"))
        self.assertFalse(ledger.unknown)
        self.assertEqual(ledger.errors, set())

    async def test_legacy_retry_helper_counts_failed_attempt_without_changing_final_usage(self):
        isolated = fallback_compat()
        ledger = isolated.UsageAttemptLedger()
        token = isolated.current_attempt_ledger.set(ledger)
        calls = 0
        async def old_retry(label, factory, *, max_attempts=None):
            try:
                return await factory()
            except RuntimeError:
                return await factory()
        async def request():
            nonlocal calls
            calls += 1
            snapshot = usage(5, 2) if calls == 1 else usage(3, 1)
            isolated.observe_usage_snapshot(snapshot, complete=True)
            if calls == 1:
                raise RuntimeError("retry")
            return snapshot
        try:
            with patch.object(isolated, "_core_retry_provider_request", old_retry):
                final = await isolated.retry_provider_request("test", request)
            self.assertEqual((final.input_other, final.output), (3, 1))
            total = ledger.combined(final)
            self.assertEqual((total.input_other, total.output), (8, 3))
        finally:
            isolated.current_attempt_ledger.reset(token)
