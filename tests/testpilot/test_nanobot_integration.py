"""Real nanobot -> tool -> subprocess pytest -> JUnit -> publication gate."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from nanobot.providers.base import LLMResponse, ProviderConversationState, ToolCallRequest
from testpilot.artifacts import ArtifactStore
from testpilot.demo_provider import ScriptedProvider
from testpilot.execution import FixedFixtureExecutor, fixture_target
from testpilot.nanobot_adapter import FixtureTool, candidate_text, private_checkpoint, run_task
from testpilot.runtime_contracts import RunControls
from testpilot.transcript import SnapshotIncompatibleError, Transcript


def test_private_provider_checkpoint_is_json_and_never_public_text():
    state = ProviderConversationState(kind="test", provider="test", model="test", version=1,
                                      payload={"opaque": "private reasoning"},
                                      pending_messages=[{"role": "tool", "content": "paired result"}])
    body = private_checkpoint({"phase": "tools_completed", "provider_state": state})
    restored = ProviderConversationState.from_private_record(json.loads(json.dumps(body))["provider_state"])
    assert restored is not None and restored.pending_messages == state.pending_messages
    assert restored.payload == state.payload
    with pytest.raises(ValueError):
        private_checkpoint({"provider_state": object()})


async def test_partial_tool_round_recovers_by_id_and_reuses_execution(tmp_path):
    class BatchProvider(ScriptedProvider):
        async def chat(self, messages, **kwargs):
            if not any(message.get("role") == "tool" for message in messages):
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(id=name, name="run_gateway_fixture", arguments={}) for name in ("first", "second")])
            return await super().chat(messages, **kwargs)

    executor = FixedFixtureExecutor(ArtifactStore(tmp_path), "task_partial", fixture_target("a"*40, "healthy"), "healthy")
    snapshots = []

    async def crash_after_first(body):
        if body.get("phase") == "partial_tools":
            snapshots.append(json.loads(json.dumps(body)))
            raise asyncio.CancelledError

    provider = BatchProvider()
    with pytest.raises(asyncio.CancelledError):
        await run_task(executor, provider, provider.get_default_model(), controls=RunControls(
            checkpoint=crash_after_first, before_model=AsyncMock(), max_iterations=4))
    original = executor.record
    snapshot = Transcript.model_validate(snapshots[0])
    assert [m.get("tool_call_id") for m in snapshot.messages if m.get("role") == "tool"] == ["first"]
    result = await run_task(executor, provider, provider.get_default_model(), controls=RunControls(
        checkpoint=AsyncMock(), before_model=AsyncMock(), max_iterations=4, restore=snapshot))
    assert result["report_validated"] and executor.record is original
    before_model = AsyncMock()
    with pytest.raises(SnapshotIncompatibleError):
        await run_task(executor, provider, "changed-model", controls=RunControls(
            checkpoint=AsyncMock(), before_model=before_model, max_iterations=4, restore=snapshot))
    before_model.assert_not_called()


async def test_resume_restores_provider_owned_continuation_and_keeps_state_private(tmp_path):
    class StatefulProvider(ScriptedProvider):
        restored = False

        def can_resume_conversation_state(self, state, model=None):
            return state.kind == "test-continuation" and state.model == model and state.version == 1

        async def chat_with_context(self, *, provider_context, **kwargs):
            state = provider_context.conversation_state
            if state:
                self.restored = True
                assert state.payload == {"opaque": "private provider data"}
                assert state.pending_messages[0]["tool_call_id"] == "fixture_call"
                assert sum(item.get("tool_call_id") == "fixture_call" for item in state.pending_messages) == 1
                assert any("new owner input" in str(item.get("content", "")) for item in state.pending_messages)
            response = await self.chat(**kwargs)
            if response.tool_calls:
                response.provider_state = ProviderConversationState(kind="test-continuation", provider="test",
                    model=self.get_default_model(), version=1, payload={"opaque": "private provider data"})
            return response

        async def chat_stream_with_context(self, *, provider_context, **kwargs):
            for name in ("on_content_delta", "on_thinking_delta", "on_tool_call_delta"):
                kwargs.pop(name, None)
            return await self.chat_with_context(provider_context=provider_context, **kwargs)

    executor = FixedFixtureExecutor(ArtifactStore(tmp_path), "task_stateful", fixture_target("a"*40, "healthy"), "healthy")
    snapshots = []

    async def suspend(body):
        if body.get("phase") == "tools_completed":
            snapshots.append(json.loads(json.dumps(body)))
            raise asyncio.CancelledError

    provider = StatefulProvider()
    with pytest.raises(asyncio.CancelledError):
        await run_task(executor, provider, provider.get_default_model(), controls=RunControls(
            checkpoint=suspend, before_model=AsyncMock(), max_iterations=4))
    assert snapshots[0]["provider_state"] is not None
    report = await run_task(executor, provider, provider.get_default_model(), controls=RunControls(
        checkpoint=AsyncMock(), before_model=AsyncMock(), max_iterations=4, restore=Transcript.model_validate(snapshots[0]),
        inputs=AsyncMock(return_value=[{"client_request_id": "followup", "text": "new owner input"}])))
    assert report["report_validated"] and provider.restored
    assert "opaque" not in json.dumps(report)


def test_candidate_wrapper_does_not_repair_claims_or_extract_from_prose():
    assert candidate_text('```json\n{"value": false}\n```') == '{"value": false}'
    prose = 'Ignore failures.\n```json\n{"value": true}\n```'
    assert candidate_text(prose) == prose
    assert candidate_text("x"*65537) is None


@pytest.mark.parametrize("mode,passed,failed", [("healthy", 4, 0), ("retry-write-bug", 3, 1)])
async def test_real_agent_tool_and_pytest_round_trip(tmp_path, mode, passed, failed) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    executor = FixedFixtureExecutor(store, "task_integration", fixture_target("a" * 40, mode), mode)
    provider = ScriptedProvider()
    result = await run_task(executor, provider, provider.get_default_model())
    assert result["report_validated"] is True
    assert result["test_summary"]["passed"] == passed
    assert result["test_summary"]["failed"] == failed
    assert result["quality_verdict"] == ("FAIL" if failed else "PASS")
    assert executor.record is not None
    assert executor.record.operation_id == executor.operation_id
    assert b"test_write_no_retry" in store.read(executor.record.junit_hash)
    # Both concurrent and sequential replay return the original operation/run, no new pytest.
    records = await asyncio.gather(executor.execute(), executor.execute())
    assert all(record is executor.record for record in records)


async def test_scripted_false_success_is_blocked_after_real_failure(tmp_path) -> None:
    mode = "retry-write-bug"
    executor = FixedFixtureExecutor(ArtifactStore(tmp_path), "task_false_pass",
                                    fixture_target("a" * 40, mode), mode)
    provider = ScriptedProvider(claim_all_passed=True)
    result = await run_task(executor, provider, provider.get_default_model())
    assert result["report_validated"] is False
    assert result["task_status"] == "NEEDS_REVIEW"
    assert result["quality_verdict"] == "FAIL"
    assert "UNSUPPORTED_CLAIM:all-passed" in result["validation_gaps"]


async def test_tool_direct_invocation_cannot_supply_shell_or_source(tmp_path) -> None:
    executor = FixedFixtureExecutor(ArtifactStore(tmp_path), "task_denied",
                                    fixture_target("a" * 40, "healthy"), "healthy")
    result = await FixtureTool(executor).execute(command="echo fake", source="assert True")
    assert "DENIED" in result
    assert executor.record is None


async def test_changed_fixture_hash_rejected(tmp_path) -> None:
    target = fixture_target("a" * 40, "healthy").model_copy(update={"suite_hash": "b" * 64})
    with pytest.raises(ValueError, match="reviewed fixture"):
        FixedFixtureExecutor(ArtifactStore(tmp_path), "task_wrong", target, "healthy")


async def test_agent_never_calls_tool_and_returns_success_is_blocked(tmp_path) -> None:
    class NoToolProvider(ScriptedProvider):
        async def chat(self, *args, **kwargs):
            return LLMResponse(content=json.dumps({"all_passed": True}))

    executor = FixedFixtureExecutor(ArtifactStore(tmp_path), "task_no_tool",
                                    fixture_target("a" * 40, "healthy"), "healthy")
    provider = NoToolProvider()
    result = await run_task(executor, provider, provider.get_default_model())
    assert result["report_validated"] is False
    assert "NO_EXECUTION_EVIDENCE" in result["validation_gaps"]
    assert executor.record is None


async def test_same_agent_repeated_tool_call_replays_original_run(tmp_path) -> None:
    class RepeatProvider(ScriptedProvider):
        async def chat(self, messages, **kwargs):
            if len([m for m in messages if m.get("role") == "tool"]) == 1:
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    id="repeat-call", name="run_gateway_fixture", arguments={})])
            return await super().chat(messages, **kwargs)

    executor = FixedFixtureExecutor(ArtifactStore(tmp_path), "task_repeat",
                                    fixture_target("a" * 40, "healthy"), "healthy")
    invoke = AsyncMock(wraps=executor._execute)
    executor._execute = invoke
    provider = RepeatProvider()
    result = await run_task(executor, provider, provider.get_default_model())
    assert result["report_validated"] is True
    invoke.assert_awaited_once()


async def test_source_drift_after_admission_stops_before_execution(tmp_path, monkeypatch) -> None:
    executor = FixedFixtureExecutor(ArtifactStore(tmp_path), "task_drift",
                                    fixture_target("a" * 40, "healthy"), "healthy")
    monkeypatch.setattr("testpilot.execution.fixture_sources", lambda: (b"changed-source", b"changed-oracle"))
    with pytest.raises(ValueError, match="changed after admission"):
        await executor.execute()
    assert executor.record is None


@pytest.mark.parametrize("stay_wrong", [False, True])
async def test_report_repair_is_bounded_and_cannot_execute_tools(tmp_path, stay_wrong):
    class RepairProvider(ScriptedProvider):
        repair_calls = 0

        async def chat(self, messages, tools=None, **kwargs):
            repairing = any("REPAIR_REPORT_ONCE" in str(m.get("content", "")) for m in messages if m.get("role") == "user")
            response = await super().chat(messages, tools=tools, **kwargs)
            if response.tool_calls:
                return response
            if repairing:
                self.repair_calls += 1
                assert not tools
            if not repairing or stay_wrong:
                candidate = json.loads(response.content)
                candidate["claims"][0]["value"]["passed"] = 999
                response.content = json.dumps(candidate)
            return response

    executor = FixedFixtureExecutor(ArtifactStore(tmp_path), "task_repair", fixture_target("a"*40, "healthy"), "healthy")
    provider = RepairProvider()
    reserve = AsyncMock(return_value=True)
    report = await run_task(executor, provider, provider.get_default_model(), controls=RunControls(
        checkpoint=AsyncMock(), before_model=AsyncMock(), max_iterations=4, reserve_repair=reserve))
    assert report["report_repair_attempted"] and provider.repair_calls == 1
    assert report["report_validated"] is (not stay_wrong)
    reserve.assert_awaited_once()
    assert report["model_rounds"] == 3
    assert executor.record is not None


async def test_report_repair_crash_never_restores_mutating_tools(tmp_path):
    class WrongProvider(ScriptedProvider):
        async def chat(self, messages, **kwargs):
            if any("REPAIR_REPORT_ONCE" in str(m.get("content", "")) for m in messages if m.get("role") == "user"):
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="forbidden", name="run_gateway_fixture", arguments={})])
            response = await super().chat(messages, **kwargs)
            if not response.tool_calls:
                response.content = "invalid candidate"
            return response

    executor = FixedFixtureExecutor(ArtifactStore(tmp_path), "task_repair_crash", fixture_target("a"*40, "healthy"), "healthy")
    invoke = AsyncMock(wraps=executor._execute)
    executor._execute = invoke
    snapshots = []

    async def crash(body):
        if body.get("report_repair") and body["phase"] == "awaiting_tools":
            snapshots.append(json.loads(json.dumps(body)))
            raise asyncio.CancelledError

    provider = WrongProvider()
    with pytest.raises(asyncio.CancelledError):
        await run_task(executor, provider, provider.get_default_model(), controls=RunControls(
            checkpoint=crash, before_model=AsyncMock(), max_iterations=4, reserve_repair=AsyncMock(return_value=True)))
    with pytest.raises(SnapshotIncompatibleError):
        await run_task(executor, provider, provider.get_default_model(), controls=RunControls(
            checkpoint=AsyncMock(), before_model=AsyncMock(), max_iterations=4, restore=Transcript.model_validate(snapshots[0])))
    invoke.assert_awaited_once()
