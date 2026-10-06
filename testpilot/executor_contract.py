"""Business adapter contract independent of local process/container implementations."""

from typing import Protocol

from testpilot.artifacts import ArtifactStore
from testpilot.domain import ExecutionRecord, Target


class FixtureExecutor(Protocol):
    store: ArtifactStore
    task_id: str
    target: Target
    record: ExecutionRecord | None
    operation_id: str

    async def execute(self) -> ExecutionRecord: ...
