"""Host-enforced controls for a bounded nanobot execution fragment."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from testpilot.domain import PendingExecution
from testpilot.git_source import GitSource
from testpilot.gitlab import GitLabAdapter
from testpilot.governance import TaskPlan
from testpilot.knowledge import KnowledgeResult
from testpilot.telemetry import TraceRecord
from testpilot.transcript import Transcript


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
    memory: Callable[[], Awaitable[list[dict[str, object]]]] | None = None
    before_action: Callable[[], Awaitable[None]] | None = None
    inputs: Callable[[], Awaitable[list[dict[str, object]]]] | None = None
    mark_inputs: Callable[[list[str]], Awaitable[None]] | None = None
    restore: Transcript | None = None
    trace: Callable[[TraceRecord], Awaitable[None]] | None = None
    git_source: GitSource | None = None
    gitlab: GitLabAdapter | None = None
