"""Canonical approval and plan contracts; model instructions cannot expand capabilities."""

import json
from typing import Literal

from pydantic import Field

from testpilot.artifacts import digest
from testpilot.domain import Contract, Target


class ApprovalDeniedError(RuntimeError):
    pass


def execution_hash(task_id: str, run_id: str, target: Target, goal: str, mode: str) -> str:
    return digest(json.dumps({"task_id": task_id, "run_id": run_id, "target": target.model_dump(),
                              "goal": goal, "mode": mode, "policy": "fixed-runner-v1",
                              "profile": {"uid": 10001, "network": "none", "image": target.env_snapshot_id}},
                             sort_keys=True, separators=(",", ":")).encode())


class ApprovalDecision(Contract):
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["APPROVE", "DENY"]


class PlanStep(Contract):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    action: Literal["search_knowledge", "run_gateway_fixture", "inspect_result", "publish_report"]
    rationale: str = Field(min_length=1, max_length=500)


class TaskPlan(Contract):
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=8)
    limitations: tuple[str, ...] = Field(default=(), max_length=8)

    def validate_scope(self) -> None:
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("duplicate plan step")
        if sum(step.action == "run_gateway_fixture" for step in self.steps) > 1:
            raise ValueError("plan cannot increase admitted Job budget")
        if self.steps[-1].action != "publish_report":
            raise ValueError("plan must end at the evidence gate")
