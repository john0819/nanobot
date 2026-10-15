"""Immutable failure drafts and independent content-bound review. No external writes."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import Field, TypeAdapter

from testpilot.artifacts import digest
from testpilot.domain import Contract
from testpilot.storage.postgres import PROJECT, Ledger, RequestConflictError, TaskRow
from testpilot.storage_contracts import ArtifactFactory


def content_hash(body: dict[str, Any]) -> str:
    return digest(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


class DraftRequest(Contract):
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    case_id: str = Field(min_length=1, max_length=300)


class DraftDecision(Contract):
    expected_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["APPROVE", "DENY"]


class Draft(Contract):
    tenant_id: str
    project_id: str
    id: str
    task_id: str
    run_id: str
    case_id: str
    creator_id: str
    content: dict[str, Any]
    content_hash: str
    status: Literal["DRAFT", "APPROVED", "DENIED"]
    reviewer_id: str | None
    version: int
    created_at: datetime
    decided_at: datetime | None


class Defects:
    def __init__(self, ledger: Ledger, root: Path, artifacts: ArtifactFactory) -> None:
        self.ledger, self.root, self.artifacts = ledger, root, artifacts

    async def verify(self, draft: Draft) -> None:
        task = await self.ledger.get(draft.task_id, draft.creator_id)
        report = await self.ledger.run_report(task, draft.run_id) if task else None
        if not report or report.get("report_validated") is not True or content_hash(report) != draft.content.get("report_hash") or content_hash(draft.content) != draft.content_hash:
            raise RequestConflictError("draft/source integrity conflict")
        refs = TypeAdapter(list[str]).validate_python(report.get("artifact_refs", []), strict=True)
        if not refs:
            raise RequestConflictError("draft evidence unavailable")
        store = self.artifacts(self.root, draft.tenant_id, draft.task_id)
        for ref in refs:
            await asyncio.to_thread(store.read, ref)

    async def propose(self, task: TaskRow, spec: DraftRequest) -> Draft:
        report = await self.ledger.run_report(task, spec.run_id)
        if report is None or report.get("report_validated") is not True or report.get("quality_verdict") != "FAIL":
            raise RequestConflictError("validated failing report required")
        findings = TypeAdapter(list[dict[str, Any]]).validate_python(report.get("findings", []))
        finding = next((item for item in findings if item.get("case_id") == spec.case_id and item.get("claim_status") == "VERIFIED"), None)
        if finding is None:
            raise RequestConflictError("case is not a verified failure")
        refs = TypeAdapter(list[str]).validate_python(report.get("artifact_refs", []), strict=True)
        if not refs:
            raise RequestConflictError("failure evidence unavailable")
        store = self.artifacts(self.root, task.tenant_id, task.id)
        for ref in refs:
            await asyncio.to_thread(store.read, ref)
        body = {"title": "Observed test failure: "+spec.case_id, "case_id": spec.case_id,
                "target": report["target"], "run_id": spec.run_id, "report_hash": content_hash(report),
                "evidence_ids": finding["evidence_ids"], "artifact_refs": report["artifact_refs"],
                "observation": "Test failure observed; product defect and root cause are unconfirmed.",
                "submission_supported": False}
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("INSERT INTO testpilot.defect_drafts (tenant_id,project_id,id,task_id,run_id,case_id,creator_id,content,content_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,project_id,run_id,case_id) DO NOTHING RETURNING *", (self.ledger.tenant_id, PROJECT, "defect_"+uuid4().hex, task.id, spec.run_id, spec.case_id, task.actor_id, Jsonb(body), content_hash(body)))
            row = await cursor.fetchone()
            if row is None:
                cursor = await connection.execute("SELECT * FROM testpilot.defect_drafts WHERE tenant_id=%s AND project_id=%s AND run_id=%s AND case_id=%s AND creator_id=%s", (self.ledger.tenant_id, PROJECT, spec.run_id, spec.case_id, task.actor_id))
                row = await cursor.fetchone()
            else:
                await connection.execute("INSERT INTO testpilot.defect_events (tenant_id,project_id,defect_id,version,actor_id,type,content_hash) VALUES (%s,%s,%s,1,%s,'draft.created',%s)", (self.ledger.tenant_id, PROJECT, row["id"], task.actor_id, row["content_hash"]))
            if row is None:
                raise RequestConflictError("draft scope conflict")
            return Draft.model_validate(row)

    async def get(self, identifier: str, actor: str, reviewer: bool = False) -> Draft | None:
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("SELECT * FROM testpilot.defect_drafts WHERE tenant_id=%s AND project_id=%s AND id=%s AND (creator_id=%s OR %s)", (self.ledger.tenant_id, PROJECT, identifier, actor, reviewer))
            row = await cursor.fetchone()
            return Draft.model_validate(row) if row else None

    async def list(self, actor: str, reviewer: bool = False) -> list[Draft]:
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("SELECT * FROM testpilot.defect_drafts WHERE tenant_id=%s AND project_id=%s AND (creator_id=%s OR %s) ORDER BY created_at DESC LIMIT 100", (self.ledger.tenant_id, PROJECT, actor, reviewer))
            return [Draft.model_validate(row) for row in await cursor.fetchall()]

    async def decide(self, identifier: str, actor: str, spec: DraftDecision) -> Draft:
        if spec.decision == "APPROVE":
            initial = await self.get(identifier, actor, reviewer=True)
            if initial is None:
                raise RequestConflictError("draft not found")
            await self.verify(initial)
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("SELECT * FROM testpilot.defect_drafts WHERE tenant_id=%s AND project_id=%s AND id=%s FOR UPDATE", (self.ledger.tenant_id, PROJECT, identifier))
            row = await cursor.fetchone()
            if row is None or row["creator_id"] == actor or row["version"] != spec.expected_version or row["content_hash"] != spec.content_hash or row["status"] != "DRAFT":
                raise RequestConflictError("draft reviewer/version/content/state conflict")
            cursor = await connection.execute("UPDATE testpilot.defect_drafts SET status=%s,reviewer_id=%s,version=version+1,decided_at=clock_timestamp() WHERE tenant_id=%s AND project_id=%s AND id=%s RETURNING *", ("APPROVED" if spec.decision == "APPROVE" else "DENIED", actor, self.ledger.tenant_id, PROJECT, identifier))
            fresh = await cursor.fetchone()
            assert fresh is not None
            await connection.execute("INSERT INTO testpilot.defect_events (tenant_id,project_id,defect_id,version,actor_id,type,content_hash) VALUES (%s,%s,%s,%s,%s,%s,%s)", (self.ledger.tenant_id, PROJECT, identifier, fresh["version"], actor, "draft."+fresh["status"].lower(), spec.content_hash))
            return Draft.model_validate(fresh)
