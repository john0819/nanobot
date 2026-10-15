"""Read-only evidence inventory verification, suitable for post-restore checks."""

import asyncio
from pathlib import Path

from testpilot.artifacts import ArtifactUnavailableError
from testpilot.storage.postgres import Ledger, TaskRow
from testpilot.storage_contracts import ArtifactFactory


async def verify_inventory(ledger: Ledger, task: TaskRow, root: Path, artifacts: ArtifactFactory) -> dict[str, object]:
    inventory = await ledger.artifact_inventory(task)
    store = artifacts(root, task.tenant_id, task.id)
    verified: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []
    for item in inventory:
        identity: dict[str, object] = {"hash": str(item["hash"]), "kind": str(item["kind"]), "run_id": str(item["run_id"])}
        try:
            content = await asyncio.to_thread(store.read, str(item["hash"]))
            verified.append({**identity, "bytes": len(content)})
        except (OSError, ValueError, ArtifactUnavailableError):
            missing.append(identity)
    return {"task_id": task.id, "tenant_id": task.tenant_id, "project_id": task.project_id,
            "verified": verified, "unavailable": missing, "integrity_ok": not missing,
            "scope": "registered immutable artifacts only; DB backup and orphan discovery are separate operations"}
