"""Reviewed, scoped failure observations. Memory never becomes current execution evidence."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from testpilot.domain import Contract
from testpilot.history import MemoryCandidateRequest, MemoryDecision
from testpilot.storage.postgres import PROJECT, Ledger, RequestConflictError, TaskRow
from testpilot.storage_contracts import ArtifactFactory


class MemoryRow(Contract):
    tenant_id: str
    project_id: str
    id: str
    source_task_id: str
    source_run_id: str
    source_evidence_id: str
    creator_id: str
    reviewer_id: str | None
    user_scope: str | None
    type: str
    key: str
    value: dict[str, object]
    suite_hash: str
    status: Literal["CANDIDATE", "CONFIRMED", "REVOKED", "EXPIRED"]
    version: int
    expires_at: datetime
    created_at: datetime


class MemoryRepository:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    async def propose(self, task: TaskRow, request: MemoryCandidateRequest) -> MemoryRow:
        """Derive only parser-backed observations, not arbitrary natural-language diagnoses/secrets."""
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("SELECT x.report,o.result FROM testpilot.run_results x JOIN testpilot.operations o ON (o.tenant_id,o.project_id,o.run_id)=(x.tenant_id,x.project_id,x.run_id) WHERE x.tenant_id=%s AND x.project_id=%s AND x.task_id=%s AND x.run_id=%s AND x.state='COMPLETED' AND o.state='SUCCEEDED'", (self.ledger.tenant_id, PROJECT, task.id, request.source_run_id))
            source = await cursor.fetchone()
            if source is None or source["report"].get("report_validated") is not True:
                raise RequestConflictError("validated failure source required")
            report: dict[str, Any] = source["report"]
            matching = [item for item in report.get("findings", []) if item.get("case_id") == request.case_id and item.get("claim_status") == "VERIFIED"]
            if not matching:
                raise RequestConflictError("case is not a verified failure")
            execution: dict[str, Any] = source["result"]
            evidence_id = str(execution["evidence_id"])
            if evidence_id not in matching[0].get("evidence_ids", []):
                raise RequestConflictError("failure evidence scope conflict")
            memory_id = "memory_"+uuid4().hex
            value = {"case_id": request.case_id, "observation": "Failure observed in reviewed fixture execution; root cause is unconfirmed.",
                     "source_quality_verdict": report["quality_verdict"]}
            cursor = await connection.execute("INSERT INTO testpilot.memory_records (tenant_id,project_id,id,source_task_id,source_run_id,source_evidence_id,creator_id,user_scope,key,value,suite_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,project_id,source_run_id,key) DO NOTHING RETURNING *", (self.ledger.tenant_id, PROJECT, memory_id, task.id, request.source_run_id, evidence_id, task.actor_id, None if request.shared else task.actor_id, request.case_id, Jsonb(value), execution["target"]["suite_hash"]))
            row = await cursor.fetchone()
            if row is None:
                cursor = await connection.execute("SELECT * FROM testpilot.memory_records WHERE tenant_id=%s AND project_id=%s AND source_run_id=%s AND key=%s", (self.ledger.tenant_id, PROJECT, request.source_run_id, request.case_id))
                row = await cursor.fetchone()
                assert row is not None
                if row["user_scope"] != (None if request.shared else task.actor_id):
                    raise RequestConflictError("existing memory sharing scope differs")
            else:
                await connection.execute("INSERT INTO testpilot.memory_events VALUES (%s,%s,%s,1,%s,'CANDIDATE',clock_timestamp())", (self.ledger.tenant_id, PROJECT, memory_id, task.actor_id))
            return MemoryRow.model_validate(row)

    async def get(self, memory_id: str, actor: str, reviewer: bool = False) -> MemoryRow | None:
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("SELECT m.*,CASE WHEN m.status<>'REVOKED' AND m.expires_at<=clock_timestamp() THEN 'EXPIRED' ELSE m.status END AS status FROM testpilot.memory_records m WHERE tenant_id=%s AND project_id=%s AND id=%s AND (creator_id=%s OR %s OR (user_scope IS NULL AND status='CONFIRMED'))", (self.ledger.tenant_id, PROJECT, memory_id, actor, reviewer))
            row = await cursor.fetchone()
            return MemoryRow.model_validate(row) if row else None

    async def list_entries(self, actor: str, review: bool = False) -> list[MemoryRow]:
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("SELECT m.*,CASE WHEN m.status<>'REVOKED' AND m.expires_at<=clock_timestamp() THEN 'EXPIRED' ELSE m.status END AS status FROM testpilot.memory_records m WHERE tenant_id=%s AND project_id=%s AND ((%s AND status='CANDIDATE' AND expires_at>clock_timestamp()) OR (NOT %s AND (creator_id=%s OR (user_scope IS NULL AND status='CONFIRMED' AND expires_at>clock_timestamp())))) ORDER BY created_at DESC LIMIT 100", (self.ledger.tenant_id, PROJECT, review, review, actor))
            return [MemoryRow.model_validate(row) for row in await cursor.fetchall()]

    async def events(self, memory_id: str) -> list[dict[str, Any]]:
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("SELECT version,actor_id,status,created_at FROM testpilot.memory_events WHERE tenant_id=%s AND project_id=%s AND memory_id=%s ORDER BY version LIMIT 100", (self.ledger.tenant_id, PROJECT, memory_id))
            return await cursor.fetchall()

    async def expire(self) -> int:
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("SELECT id FROM testpilot.memory_records WHERE tenant_id=%s AND project_id=%s AND status IN ('CANDIDATE','CONFIRMED') AND expires_at<=clock_timestamp() LIMIT 100 FOR UPDATE SKIP LOCKED", (self.ledger.tenant_id, PROJECT))
            rows = await cursor.fetchall()
            for row in rows:
                cursor = await connection.execute("UPDATE testpilot.memory_records SET status='EXPIRED',version=version+1 WHERE tenant_id=%s AND project_id=%s AND id=%s RETURNING version", (self.ledger.tenant_id, PROJECT, row["id"]))
                updated = await cursor.fetchone()
                assert updated is not None
                await connection.execute("INSERT INTO testpilot.memory_events VALUES (%s,%s,%s,%s,'system:ttl','EXPIRED',clock_timestamp())", (self.ledger.tenant_id, PROJECT, row["id"], updated["version"]))
            return len(rows)

    async def decide(self, memory_id: str, actor: str, decision: MemoryDecision) -> MemoryRow:
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("SELECT * FROM testpilot.memory_records WHERE tenant_id=%s AND project_id=%s AND id=%s FOR UPDATE", (self.ledger.tenant_id, PROJECT, memory_id))
            row = await cursor.fetchone()
            if row is None:
                raise RequestConflictError("memory not found")
            item = MemoryRow.model_validate(row)
            if item.creator_id == actor or item.version != decision.expected_version:
                raise RequestConflictError("independent reviewer/current version required")
            cursor = await connection.execute("UPDATE testpilot.memory_records SET status=%s,version=version+1,reviewer_id=%s WHERE tenant_id=%s AND project_id=%s AND id=%s AND status IN ('CANDIDATE','CONFIRMED') AND expires_at>clock_timestamp() RETURNING *", ("CONFIRMED" if decision.decision == "CONFIRM" else "REVOKED", actor, self.ledger.tenant_id, PROJECT, memory_id))
            updated = await cursor.fetchone()
            if updated is None or (decision.decision == "CONFIRM" and item.status != "CANDIDATE"):
                raise RequestConflictError("memory state/expiry conflict")
            fresh = MemoryRow.model_validate(updated)
            await connection.execute("INSERT INTO testpilot.memory_events VALUES (%s,%s,%s,%s,%s,%s,clock_timestamp())", (self.ledger.tenant_id, PROJECT, memory_id, fresh.version, actor, fresh.status))
            return fresh

    async def retrieve(self, task: TaskRow) -> list[dict[str, object]]:
        if task.tenant_id != self.ledger.tenant_id or task.project_id != PROJECT:
            return []
        async with self.ledger.connection() as connection:
            cursor = await connection.execute("SELECT m.* FROM testpilot.memory_records m JOIN testpilot.run_results x ON (x.tenant_id,x.project_id,x.run_id)=(m.tenant_id,m.project_id,m.source_run_id) JOIN testpilot.task_runs r ON (r.tenant_id,r.project_id,r.id)=(m.tenant_id,m.project_id,m.source_run_id) WHERE m.tenant_id=%s AND m.project_id=%s AND m.suite_hash=%s AND r.target->>'env_snapshot_id'=%s AND r.target->>'commit_sha'=%s AND m.status='CONFIRMED' AND m.expires_at>clock_timestamp() AND (m.user_scope IS NULL OR m.user_scope=%s) AND x.report->>'report_validated'='true' ORDER BY m.created_at DESC LIMIT 8", (self.ledger.tenant_id, PROJECT, task.target.suite_hash, task.target.env_snapshot_id, task.target.commit_sha, task.actor_id))
            rows = [MemoryRow.model_validate(row) for row in await cursor.fetchall()]
            return [{"memory_id": row.id, "version": row.version, "value": row.value, "source_run_id": row.source_run_id,
                     "source_evidence_id": row.source_evidence_id, "expires_at": row.expires_at.isoformat(),
                     "trust_level": "REVIEWED_HISTORICAL_OBSERVATION", "current_execution_evidence": False} for row in rows]


async def verify_source(ledger: Ledger, root: Path, artifacts: ArtifactFactory, item: MemoryRow) -> None:
    if item.tenant_id != ledger.tenant_id or item.project_id != PROJECT:
        raise RequestConflictError("memory source scope denied")
    task = await ledger.get(item.source_task_id, item.creator_id)
    if task is None:
        raise RequestConflictError("memory source unavailable")
    report = await ledger.run_report(task, item.source_run_id)
    if report is None or report.get("report_validated") is not True:
        raise RequestConflictError("memory source unavailable")
    refs = TypeAdapter(list[str]).validate_python(report.get("artifact_refs", []), strict=True)
    if not refs:
        raise RequestConflictError("memory source artifacts unavailable")
    store = artifacts(root, ledger.tenant_id, task.id)
    for ref in refs:
        await asyncio.to_thread(store.read, ref)
