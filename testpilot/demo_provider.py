"""Scripted contract provider, deliberately not an LLM quality evaluation."""

import json
from typing import Any

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class ScriptedProvider(LLMProvider):
    def __init__(self, claim_all_passed: bool = False) -> None:
        super().__init__(provider_name="testpilot-scripted-contract")
        self.claim_all_passed = claim_all_passed

    def get_default_model(self) -> str:
        return "scripted-contract-v1"

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
        reasoning_effort: str | None = None, tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        if not tool_messages:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="fixture_call", name="run_gateway_fixture", arguments={},
            )])
        observation = json.loads(tool_messages[-1]["content"])
        execution = observation["execution"]
        counts = observation["counts"]
        candidate = {
            "task_id": execution["task_id"], "run_id": execution["run_id"],
            "target": execution["target"], "claims": [
                {"claim_id": "counts", "type": "TEST_COUNTS", "value": counts,
                 "evidence_ids": [execution["evidence_id"]]},
                {"claim_id": "all-passed", "type": "ALL_PASSED",
                 "value": self.claim_all_passed or counts["passed"] == counts["planned"],
                 "evidence_ids": [execution["evidence_id"]]},
            ],
        }
        return LLMResponse(content=json.dumps(candidate))
