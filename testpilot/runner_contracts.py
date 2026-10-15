"""Backend contract shared by local Docker and remote lifecycle reconciliation."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

from testpilot.artifacts import ArtifactStore
from testpilot.domain import ExecutionRecord, PendingExecution
from testpilot.storage.postgres import OperationRow, TaskRow


class RunnerProfile(Protocol):
    image: str
    scratch: Path
    operation_id: str
    container_name: str
    expected_cases: tuple[str, ...]
    source_system: Literal["isolated-container-fixture", "isolated-git-runner"]
    def command(self, output: Path, /) -> list[str]: ...


class RunnerBackend(Protocol):
    def note_dispatch(self) -> None: ...
    async def submit(self) -> ExecutionRecord | PendingExecution: ...
    async def dispatch(self) -> ExecutionRecord: ...
    async def poll(self) -> ExecutionRecord | PendingExecution: ...
    async def reconcile(self) -> ExecutionRecord: ...
    async def cancel(self) -> ExecutionRecord | None: ...


RunnerFactory = Callable[[TaskRow, OperationRow, ArtifactStore, str, Path], RunnerBackend]
