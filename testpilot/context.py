"""Traceable context blocks and protected input budget; no tool protocol truncation."""

import json
from typing import Literal

import tiktoken
from pydantic import Field

from testpilot.domain import Contract


class ContextBudgetError(RuntimeError):
    pass


class ContextBlock(Contract):
    block_id: str
    kind: Literal["TASK", "PLAN", "KNOWLEDGE", "OBSERVATION"]
    text: str
    source_refs: tuple[str, ...] = ()
    priority: int = Field(default=0, ge=0, le=100)
    pinned: bool = False
    trust_level: Literal["SERVER_FACT", "UNTRUSTED"] = "UNTRUSTED"


def tokens(text: str) -> int:
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def select(blocks: tuple[ContextBlock, ...], budget: int) -> tuple[str, tuple[str, ...]]:
    chosen: list[ContextBlock] = []
    dropped: list[str] = []
    for block in sorted(blocks, key=lambda item: (not item.pinned, -item.priority, item.block_id)):
        trial = json.dumps([item.model_dump(mode="json") for item in chosen+[block]], ensure_ascii=False)
        if tokens(trial) <= budget:
            chosen.append(block)
        elif block.pinned:
            raise ContextBudgetError("Protected context exceeds budget")
        else:
            dropped.append(block.block_id)
    return json.dumps([block.model_dump(mode="json") for block in chosen], ensure_ascii=False), tuple(dropped)


def input_budget(window: int, output_reserved: int, team_cap: int = 24000) -> int:
    value = min(team_cap, window-output_reserved-max(1024, window//20))
    if value < 1024:
        raise ContextBudgetError("Model window cannot contain protected task/tool context")
    return value
