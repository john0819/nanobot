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
        available = {tool["function"]["name"] for tool in tools or []}
        used = {message.get("name") for message in tool_messages}
        if "search_memory" in available and "search_memory" not in used:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="memory", name="search_memory", arguments={})])
        if "propose_plan" in available and "propose_plan" not in used:
            steps = [{"id": "test", "action": "run_gateway_fixture", "rationale": "Use fixed independently reviewed assertions"},
                     {"id": "report", "action": "publish_report", "rationale": "Only parser-backed execution claims"}]
            if "search_knowledge" in available:
                steps.insert(0, {"id": "knowledge", "action": "search_knowledge", "rationale": "Retrieve current versioned gateway contract"})
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="plan", name="propose_plan", arguments={"steps": steps, "limitations": ["Fixed fixture validation only"]})])
        if "search_knowledge" in available and "search_knowledge" not in used:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="knowledge", name="search_knowledge", arguments={"query": "NovaX 网关 写请求 重试 路由 超时"})])
        executed = [message for message in tool_messages if message.get("name") == "run_gateway_fixture"]
        if not executed:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="fixture_call", name="run_gateway_fixture", arguments={},
            )])
        observation = json.loads(executed[-1]["content"])
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
