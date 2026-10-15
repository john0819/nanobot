"""Private versioned transcript and deterministic whole-tool-group projection."""

import json
from typing import Any

from pydantic import BaseModel, Field, JsonValue

from testpilot.artifacts import digest
from testpilot.context import ContextBudgetError, tokens


class SnapshotIncompatibleError(RuntimeError):
    pass


class Transcript(BaseModel):
    schema_version: int = 2
    phase: str
    runtime_fingerprint: str
    messages: list[dict[str, Any]]
    projected_inputs: list[str] = Field(default_factory=list)
    provider_state: dict[str, Any] | None = None
    report_repair: bool = False

    def check_version(self, fingerprint: str) -> None:
        if self.schema_version != 2 or self.runtime_fingerprint != fingerprint:
            raise SnapshotIncompatibleError("Runtime/model/tool/prompt snapshot changed; explicit new Run required")


def complete_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    position = 0
    while position < len(messages):
        message = messages[position]
        group = [message]
        calls = message.get("tool_calls", [])
        position += 1
        if calls:
            ids = {call["id"] for call in calls}
            if len(ids) != len(calls) or any(not isinstance(item, str) or not item for item in ids):
                raise SnapshotIncompatibleError("Duplicate/invalid tool call identity")
            while position < len(messages) and messages[position].get("role") == "tool":
                group.append(messages[position])
                position += 1
            if {item.get("tool_call_id") for item in group[1:]} != ids or len(group)-1 != len(ids):
                raise SnapshotIncompatibleError("Incomplete/duplicate tool protocol group")
        elif message.get("role") == "tool":
            raise SnapshotIncompatibleError("Orphan tool result")
        groups.append(group)
    return groups


def project(messages: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, Any]], dict[str, object] | None]:
    """Preserve system/task, notes, latest full round and all negative/partial evidence groups."""
    groups = complete_groups(messages)
    if tokens(json.dumps(messages, ensure_ascii=False)) <= budget:
        return messages, None
    protected = {0, 1, len(groups)-1}
    for index, group in enumerate(groups):
        def negative(value: JsonValue) -> bool:
            if isinstance(value, dict):
                return any((key in {"failed", "errors", "error", "not_run", "failed_cases", "validation_gaps", "conflict"} and bool(item))
                           or (key in {"status", "trust_level"} and isinstance(item, str) and item in {"PENDING", "UNSUPPORTED", "UNTRUSTED_USER_NOTE"})
                           or negative(item) for key, item in value.items())
            if isinstance(value, list):
                return any(negative(item) for item in value)
            return False
        preserve = False
        for message in group:
            try:
                preserve = preserve or negative(json.loads(message.get("content", "")))
            except (TypeError, ValueError):
                raw = str(message.get("content", "")).lower()
                preserve = preserve or any(marker in raw for marker in ("error:", "failed", "conflict"))
        if preserve:
            protected.add(index)
    chosen = set(range(len(groups)))
    removed: list[int] = []
    for index in range(2, max(2, len(groups)-1)):
        current = [message for group_index, group in enumerate(groups) if group_index in chosen for message in group]
        if tokens(json.dumps(current, ensure_ascii=False)) <= budget:
            break
        if index not in protected:
            chosen.remove(index)
            removed.append(index)
    result = [message for index, group in enumerate(groups) if index in chosen for message in group]
    if tokens(json.dumps(result, ensure_ascii=False)) > budget:
        raise ContextBudgetError("Protected facts/tool protocol exceed input budget")
    return result, {"projection_version": "whole-groups-v1", "source_hash": digest(json.dumps(messages, sort_keys=True).encode()),
                    "omitted_groups": removed, "verified_facts": [], "hypotheses": [],
                    "limitation": "Low-priority complete groups omitted; original private transcript remains immutable."}
