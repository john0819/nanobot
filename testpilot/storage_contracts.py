"""Task-scoped artifact factory independent of storage backend."""

from collections.abc import Callable
from pathlib import Path

from testpilot.artifacts import ArtifactStore

ArtifactFactory = Callable[[Path, str, str], ArtifactStore]


def local_artifacts(root: Path, tenant_id: str, task_id: str) -> ArtifactStore:
    return ArtifactStore(root / tenant_id / task_id / "artifacts")
