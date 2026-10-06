"""Real nanobot -> tool -> subprocess pytest -> JUnit -> publication gate."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from nanobot.providers.base import LLMResponse, ToolCallRequest
from testpilot.artifacts import ArtifactStore
from testpilot.demo_provider import ScriptedProvider
from testpilot.execution import FixedFixtureExecutor, fixture_target
from testpilot.nanobot_adapter import FixtureTool, run_task


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
