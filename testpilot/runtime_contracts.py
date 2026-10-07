"""Host-enforced controls for a bounded nanobot execution fragment."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from testpilot.domain import PendingExecution
from testpilot.governance import TaskPlan
from testpilot.knowledge import KnowledgeResult


class RuntimeYieldError(RuntimeError):
    """Host control raised only after paired tool results have been checkpointed."""

    def __init__(self, pending: PendingExecution) -> None:
        super().__init__("WAIT_EXTERNAL")
        self.pending = pending


@dataclass(frozen=True)
class RunControls:
    checkpoint: Callable[[dict[str, Any]], Awaitable[None]]
    before_model: Callable[[], Awaitable[None]]
    max_iterations: int
    progress: Callable[[list[str]], Awaitable[None]] | None = None
    plan: Callable[[TaskPlan], Awaitable[int]] | None = None
    task_context: str | None = None
    enable_planning: bool = False
    knowledge: Callable[[str], Awaitable[KnowledgeResult]] | None = None
    artifact_allowed: Callable[[str], Awaitable[bool]] | None = None
