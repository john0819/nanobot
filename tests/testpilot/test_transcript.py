"""Recovery protocol and large-artifact pressure must retain negative evidence."""

import json

import pytest

from testpilot.artifacts import ArtifactStore
from testpilot.context import ContextBudgetError
from testpilot.junit import MAX_JUNIT_BYTES, parse_junit
from testpilot.transcript import SnapshotIncompatibleError, Transcript, complete_groups, project


def group(call_id, observation):
    return [{"role": "assistant", "tool_calls": [{"id": call_id, "function": {"name": "read_artifact", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps(observation)}]


def test_projection_keeps_negative_facts_and_complete_tool_groups(tmp_path):
    # Raw 10 MiB log remains verifiable; only bounded slices belong in model context.
    store = ArtifactStore(tmp_path)
    raw = b"noise\n"*(10*1024*1024//6)+b"FAILED test_payment\n"
    ref = store.put(raw)
    assert store.read(ref) == raw
    messages = [{"role": "system", "content": "fixed policy"}, {"role": "user", "content": "fixed target"}]
    for i in range(12):
        messages.extend(group(str(i), {"content": "noise"*4000, "artifact_hash": ref}))
    negative = group("failure", {"failed_cases": ["test_payment"], "counts": {"failed": 1, "not_run": 2}})
    messages.extend(negative)
    messages.extend(group("latest", {"counts": {"failed": 0, "not_run": 0}}))
    original = json.dumps(messages)
    projected, summary = project(messages, 1500)
    assert summary and summary["omitted_groups"] and summary["verified_facts"] == []
    assert all(message in projected for message in negative)
    assert json.dumps(messages) == original
    complete_groups(projected)
    with pytest.raises(ContextBudgetError):
        project(messages[:2]+group("large-negative", {"failed_cases": ["x"*20000]}), 300)


@pytest.mark.parametrize("messages", [
    [{"role": "tool", "tool_call_id": "orphan"}],
    group("one", {})[:1],
    group("one", {})+[group("one", {})[1]],
])
def test_incomplete_or_duplicate_protocol_cannot_enter_projection(messages):
    with pytest.raises(SnapshotIncompatibleError):
        complete_groups(messages)


def test_recovery_requires_unchanged_runtime_snapshot():
    transcript = Transcript(phase="partial_tools", runtime_fingerprint="one", messages=[])
    transcript.check_version("one")
    with pytest.raises(SnapshotIncompatibleError):
        transcript.check_version("different-model-or-tools")
    with pytest.raises(SnapshotIncompatibleError):
        transcript.model_copy(update={"schema_version": 999}).check_version("one")


def test_large_log_limit_does_not_expand_xml_parser_limit():
    with pytest.raises(ValueError, match="JUnit exceeds"):
        parse_junit(b" "*(MAX_JUNIT_BYTES+1), ())
