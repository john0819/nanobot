"""Short PostgreSQL transactions; every worker mutation checks owner/epoch/time."""

import hashlib
import json
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from testpilot.domain import Contract, ExecutionRecord, PendingExecution, Target
from testpilot.execution import FixtureMode
from testpilot.governance import ApprovalDeniedError, TaskPlan, execution_hash
from testpilot.knowledge import KnowledgeEvidence, KnowledgeResult
from testpilot.progress import ProgressStalledError, ProgressState, advance

PROJECT = "gateway-fixture"


class LeaseLostError(RuntimeError):
    pass


class RequestConflictError(ValueError):
    pass


class CapacityReachedError(RuntimeError):
    pass


class BudgetExhaustedError(RuntimeError):
    pass


class TaskRow(Contract):
    tenant_id: str
    project_id: str
    id: str
    actor_id: str
    mode: FixtureMode
    state: Literal["QUEUED", "WAITING_APPROVAL", "RUNNING", "WAITING_EXTERNAL", "RECONCILING", "CANCELLING", "COMPLETED", "NEEDS_REVIEW", "CANCELLED"]
    run_id: str
    target: Target
    report: dict[str, object] | None
    error: str | None
    lease_owner: str | None
    lease_epoch: int
    lease_until: datetime | None
    model_rounds: int
    cancel_requested: bool
    next_event_seq: int
    state_version: int
    created_at: datetime
    updated_at: datetime
    next_wakeup_at: datetime | None
    progress_json: dict[str, object]
    goal: str
    approval_required: bool
    knowledge_required: bool
    activated_at: datetime | None
    model_limit: int

    def snapshot(self, operation_id: str | None = None) -> dict[str, object]:
        return {"task_id": self.id, "run_id": self.run_id, "state": self.state, "mode": self.mode,
                "created_at": self.created_at.isoformat(), "report_ready": self.report is not None,
                "error": self.error, "operation_id": operation_id, "lease_epoch": self.lease_epoch,
                "model_rounds": self.model_rounds, "model_limit": self.model_limit, "next_wakeup_at": self.next_wakeup_at.isoformat() if self.next_wakeup_at else None,
                "waiting_reason": "EXTERNAL_JOB" if self.state in {"WAITING_EXTERNAL", "RECONCILING"} else "APPROVAL" if self.state == "WAITING_APPROVAL" else None,
                "goal": self.goal, "approval_required": self.approval_required, "knowledge_required": self.knowledge_required}


class OperationRow(Contract):
    tenant_id: str
    project_id: str
    id: str
    task_id: str
    run_id: str
    intent_ref: str
    state: Literal["PREPARED", "DISPATCHING", "PENDING", "UNKNOWN", "SUCCEEDED", "CANCELLED"]
    result: dict[str, object] | None
    external_id: str | None
    lease_epoch: int
    dispatched_at: datetime | None
    job_external_id: str | None = None

    def execution(self) -> ExecutionRecord | None:
        return ExecutionRecord.model_validate_json(json.dumps(self.result)) if self.result else None


class Ledger:
    def __init__(self, dsn: str, tenant_id: str = "novax-demo") -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant_id):
            raise ValueError("invalid tenant scope")
        self._dsn = dsn
        self.tenant_id = tenant_id

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[AsyncConnection[dict[str, Any]], None]:
        async with await AsyncConnection[dict[str, Any]].connect(self._dsn, row_factory=dict_row, connect_timeout=5) as connection:
            await connection.execute("SET LOCAL statement_timeout = '5s'")
            await connection.execute("SET LOCAL lock_timeout = '5s'")
            yield connection

    async def migrate(self) -> None:
        async with self.connection() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(8920001)")
            await connection.execute("CREATE SCHEMA IF NOT EXISTS testpilot")
            await connection.execute("CREATE TABLE IF NOT EXISTS testpilot.migrations (version integer PRIMARY KEY, checksum text NOT NULL)")
            for version, source, checksum in self._migrations():
                cursor = await connection.execute("SELECT checksum FROM testpilot.migrations WHERE version=%s", (version,))
                previous = await cursor.fetchone()
                if previous:
                    if previous["checksum"] != checksum:
                        raise ValueError("migration checksum drift; add a new migration")
                else:
                    await connection.execute(source.encode())
                    await connection.execute("INSERT INTO testpilot.migrations VALUES (%s,%s)", (version, checksum))

    @staticmethod
    def _migrations() -> list[tuple[int, str, str]]:
        migrations: list[tuple[int, str, str]] = []
        for path in sorted(Path(__file__).parent.glob("[0-9][0-9][0-9]_*.sql")):
            source = path.read_text(encoding="utf-8")
            migrations.append((int(path.name[:3]), source, hashlib.sha256(source.encode()).hexdigest()))
        if not migrations or [row[0] for row in migrations] != list(range(1, len(migrations)+1)):
            raise ValueError("migration sequence invalid")
        return migrations

    async def ready(self) -> None:
        async with self.connection() as connection:
            for version, _, expected in self._migrations():
                cursor = await connection.execute("SELECT checksum FROM testpilot.migrations WHERE version=%s", (version,))
                row = await cursor.fetchone()
                if row is None or row["checksum"] != expected:
                    raise RuntimeError("TestPilot migration required")

    async def _event(self, connection: AsyncConnection[dict[str, Any]], task: TaskRow, kind: str,
                     payload: dict[str, object] | None = None) -> int:
        cursor = await connection.execute(
            "UPDATE testpilot.tasks SET next_event_seq=next_event_seq+1,state_version=state_version+1,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND project_id=%s AND id=%s RETURNING next_event_seq",
            (self.tenant_id, PROJECT, task.id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise LeaseLostError("task missing")
        sequence = int(row["next_event_seq"])
        values = (self.tenant_id, PROJECT, task.id, task.run_id, sequence, kind, Jsonb(payload or {}))
        await connection.execute("INSERT INTO testpilot.task_events (tenant_id,project_id,task_id,run_id,event_seq,type,public_payload) VALUES (%s,%s,%s,%s,%s,%s,%s)", values)
        await connection.execute("INSERT INTO testpilot.outbox (tenant_id,project_id,task_id,event_seq) VALUES (%s,%s,%s,%s)", values[:3] + (sequence,))
        return sequence

    async def admit(self, actor: str, key: str, mode: FixtureMode, target: Target,
                    max_active: int = 10, *, goal: str = "Validate gateway fixture",
                    approval_required: bool = False, knowledge_required: bool = False, model_limit: int = 4) -> tuple[TaskRow, bool]:
        if target.tenant_id != self.tenant_id or target.project_id != PROJECT:
            raise ValueError("target scope mismatch")
        payload_hash = hashlib.sha256(json.dumps([mode, goal, approval_required, knowledge_required]).encode()).hexdigest()
        async with self.connection() as connection:
            # Serialize admission in this tenant/project; quotas and dedup share one transaction.
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.tenant_id + ":" + PROJECT,))
            cursor = await connection.execute("SELECT mode,task_id,payload_hash FROM testpilot.request_dedup WHERE tenant_id=%s AND project_id=%s AND actor_id=%s AND request_key=%s", (self.tenant_id, PROJECT, actor, key))
            previous = await cursor.fetchone()
            if previous:
                if previous["mode"] != mode or previous["payload_hash"] not in {"", payload_hash}:
                    raise RequestConflictError("key bound to another request")
                task = await self._get(connection, previous["task_id"], actor)
                assert task is not None
                if (task.goal, task.approval_required, task.knowledge_required) != (goal, approval_required, knowledge_required):
                    raise RequestConflictError("key bound to another scope")
                return task, False
            cursor = await connection.execute("SELECT count(*) AS count FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s AND state IN ('QUEUED','WAITING_APPROVAL','RUNNING','WAITING_EXTERNAL','RECONCILING','CANCELLING')", (self.tenant_id, PROJECT))
            count = await cursor.fetchone()
            if count is None or count["count"] >= max_active:
                raise CapacityReachedError("queue capacity reached")
            task_id, run_id = "task_" + uuid4().hex, "run_" + uuid4().hex
            cursor = await connection.execute(
                "INSERT INTO testpilot.tasks (tenant_id,project_id,id,actor_id,mode,run_id,target,goal,approval_required,knowledge_required,state,activated_at,model_limit) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN NULL ELSE clock_timestamp() END,%s) RETURNING *",
                (self.tenant_id, PROJECT, task_id, actor, mode, run_id, Jsonb(target.model_dump()), goal, approval_required, knowledge_required, "WAITING_APPROVAL" if approval_required else "QUEUED", approval_required, model_limit),
            )
            row = await cursor.fetchone()
            assert row is not None
            task = TaskRow.model_validate(row)
            await connection.execute("INSERT INTO testpilot.task_runs (tenant_id,project_id,id,task_id,target) VALUES (%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, run_id, task_id, Jsonb(target.model_dump())))
            await connection.execute("INSERT INTO testpilot.request_dedup (tenant_id,project_id,actor_id,request_key,mode,task_id,payload_hash) VALUES (%s,%s,%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, actor, key, mode, task_id, payload_hash))
            await self._event(connection, task, "task.created")
            if approval_required:
                request_hash = execution_hash(task.id, task.run_id, task.target, task.goal, task.mode)
                await connection.execute("INSERT INTO testpilot.approvals (tenant_id,project_id,id,task_id,run_id,request_hash,requester_id) VALUES (%s,%s,%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, "approval_"+uuid4().hex, task.id, task.run_id, request_hash, actor))
                await self._event(connection, task, "approval.required", {"request_hash": request_hash})
            return task, True

    async def _get(self, connection: AsyncConnection[dict[str, Any]], task_id: str, actor: str) -> TaskRow | None:
        cursor = await connection.execute("SELECT * FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s AND id=%s AND actor_id=%s", (self.tenant_id, PROJECT, task_id, actor))
        row = await cursor.fetchone()
        return TaskRow.model_validate(row) if row else None

    async def get(self, task_id: str, actor: str) -> TaskRow | None:
        async with self.connection() as connection:
            return await self._get(connection, task_id, actor)

    async def claim(self, owner: str, lease_seconds: int = 30,
                    kind: Literal["all", "agent", "external"] = "all") -> TaskRow | None:
        if not 1 <= lease_seconds <= 300:
            raise ValueError("invalid lease duration")
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s "
                "AND ((%s IN ('all','agent') AND (state='QUEUED' OR (state='RUNNING' AND lease_until<clock_timestamp()))) "
                "OR (%s IN ('all','external') AND ((state='WAITING_EXTERNAL' AND next_wakeup_at<=clock_timestamp()) "
                "OR (state IN ('RECONCILING','CANCELLING') AND COALESCE(lease_until,clock_timestamp()-interval '1 second')<clock_timestamp())))) "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1", (self.tenant_id, PROJECT, kind, kind),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            cursor = await connection.execute(
                "UPDATE testpilot.tasks SET state=CASE WHEN cancel_requested THEN 'CANCELLING' WHEN state IN ('WAITING_EXTERNAL','RECONCILING') THEN 'RECONCILING' ELSE 'RUNNING' END,"
                "lease_owner=%s,lease_epoch=lease_epoch+1,lease_until=clock_timestamp()+(%s * interval '1 second') "
                "WHERE tenant_id=%s AND project_id=%s AND id=%s RETURNING *", (owner, lease_seconds, self.tenant_id, PROJECT, row["id"]),
            )
            claimed = await cursor.fetchone()
            assert claimed is not None
            task = TaskRow.model_validate(claimed)
            await self._event(connection, task, "task.claimed", {"lease_epoch": task.lease_epoch})
            return task

    async def _fence(self, connection: AsyncConnection[dict[str, Any]], task: TaskRow) -> TaskRow:
        if task.tenant_id != self.tenant_id or task.project_id != PROJECT:
            raise LeaseLostError("task scope mismatch")
        cursor = await connection.execute(
            "SELECT * FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s AND id=%s "
            "AND lease_owner=%s AND lease_epoch=%s AND lease_until>clock_timestamp() "
            "AND state IN ('RUNNING','RECONCILING','CANCELLING') FOR UPDATE",
            (self.tenant_id, PROJECT, task.id, task.lease_owner, task.lease_epoch),
        )
        row = await cursor.fetchone()
        if row is None:
            raise LeaseLostError("worker fenced")
        return TaskRow.model_validate(row)

    async def heartbeat(self, task: TaskRow, lease_seconds: int = 30) -> TaskRow:
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            await connection.execute("UPDATE testpilot.tasks SET lease_until=clock_timestamp()+(%s * interval '1 second') WHERE tenant_id=%s AND project_id=%s AND id=%s", (lease_seconds, self.tenant_id, PROJECT, task.id))
            return fresh

    async def reserve_round(self, task: TaskRow) -> None:
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            if fresh.state != "RUNNING" or fresh.cancel_requested or fresh.model_rounds >= fresh.model_limit:
                raise BudgetExhaustedError("cancelled or model round budget exhausted")
            await self._deadline(connection, fresh)
            await connection.execute("UPDATE testpilot.tasks SET model_rounds=model_rounds+1 WHERE tenant_id=%s AND project_id=%s AND id=%s", (self.tenant_id, PROJECT, task.id))

    async def checkpoint(self, task: TaskRow, body: dict[str, Any]) -> None:
        if len(json.dumps(body).encode()) > 131072:
            raise ValueError("checkpoint exceeds bounded fragment limit")
        async with self.connection() as connection:
            await self._fence(connection, task)
            sequence = await self._event(connection, task, "checkpoint.saved", {"phase": str(body.get("phase", "unknown"))})
            await connection.execute("INSERT INTO testpilot.checkpoints (tenant_id,project_id,task_id,run_id,event_seq,body) VALUES (%s,%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, task.id, task.run_id, sequence, Jsonb(body)))

    async def expire_approvals(self) -> int:
        """Bounded scheduler sweep, row-locked against concurrent decisions and cancellation."""
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT t.* FROM testpilot.tasks t JOIN testpilot.approvals a ON (a.tenant_id,a.project_id,a.run_id)=(t.tenant_id,t.project_id,t.run_id) WHERE t.tenant_id=%s AND t.project_id=%s AND t.state='WAITING_APPROVAL' AND a.status='PENDING' AND a.expires_at<=clock_timestamp() ORDER BY t.created_at LIMIT 100 FOR UPDATE OF t SKIP LOCKED", (self.tenant_id, PROJECT))
            rows = await cursor.fetchall()
            for row in rows:
                task = TaskRow.model_validate(row)
                await connection.execute("UPDATE testpilot.approvals SET status='EXPIRED' WHERE tenant_id=%s AND project_id=%s AND run_id=%s AND status='PENDING'", (self.tenant_id, PROJECT, task.run_id))
                await connection.execute("UPDATE testpilot.tasks SET state='NEEDS_REVIEW',error='APPROVAL_EXPIRED' WHERE tenant_id=%s AND project_id=%s AND id=%s", (self.tenant_id, PROJECT, task.id))
                await self._event(connection, task, "approval.expired", {"state": "NEEDS_REVIEW"})
            return len(rows)

    async def operation(self, task: TaskRow) -> OperationRow:
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            if fresh.cancel_requested:
                raise BudgetExhaustedError("cancel requested")
            await connection.execute("INSERT INTO testpilot.operations (tenant_id,project_id,id,task_id,run_id,lease_epoch) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,project_id,run_id,intent_ref) DO NOTHING", (self.tenant_id, PROJECT, "op_" + uuid4().hex, task.id, task.run_id, task.lease_epoch))
            cursor = await connection.execute("SELECT o.*,j.external_id AS job_external_id FROM testpilot.operations o LEFT JOIN testpilot.external_jobs j ON (j.tenant_id,j.project_id,j.operation_id)=(o.tenant_id,o.project_id,o.id) WHERE o.tenant_id=%s AND o.project_id=%s AND o.run_id=%s", (self.tenant_id, PROJECT, task.run_id))
            row = await cursor.fetchone()
            assert row is not None
            return OperationRow.model_validate(row)

    async def find_operation(self, task: TaskRow) -> OperationRow | None:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT o.*,j.external_id AS job_external_id FROM testpilot.operations o LEFT JOIN testpilot.external_jobs j ON (j.tenant_id,j.project_id,j.operation_id)=(o.tenant_id,o.project_id,o.id) WHERE o.tenant_id=%s AND o.project_id=%s AND o.run_id=%s", (self.tenant_id, PROJECT, task.run_id))
            row = await cursor.fetchone()
            return OperationRow.model_validate(row) if row else None

    async def dispatch(self, task: TaskRow, operation: OperationRow) -> None:
        self._operation_scope(task, operation)
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            if fresh.cancel_requested:
                raise BudgetExhaustedError("cancel requested")
            await self._deadline(connection, fresh)
            if fresh.approval_required:
                expected = execution_hash(fresh.id, fresh.run_id, fresh.target, fresh.goal, fresh.mode)
                cursor = await connection.execute("UPDATE testpilot.approvals SET consumed_operation_id=%s WHERE tenant_id=%s AND project_id=%s AND run_id=%s AND request_hash=%s AND status='APPROVED' AND reviewer_id<>requester_id AND expires_at>clock_timestamp() AND consumed_operation_id IS NULL RETURNING id", (operation.id, self.tenant_id, PROJECT, fresh.run_id, expected))
                if await cursor.fetchone() is None:
                    raise ApprovalDeniedError("Execution approval invalid/expired/consumed")
            cursor = await connection.execute("UPDATE testpilot.operations SET state='DISPATCHING',lease_epoch=%s,external_id=%s,dispatched_at=clock_timestamp() WHERE tenant_id=%s AND project_id=%s AND id=%s AND state='PREPARED' RETURNING id", (task.lease_epoch, "testpilot-" + operation.id, self.tenant_id, PROJECT, operation.id))
            if await cursor.fetchone() is None:
                raise LeaseLostError("operation already dispatched")
            await connection.execute("INSERT INTO testpilot.attempts (tenant_id,project_id,operation_id,sequence,dispatch_certainty) VALUES (%s,%s,%s,1,'UNKNOWN')", (self.tenant_id, PROJECT, operation.id))
            sequence = await self._event(connection, task, "operation.dispatching", {"operation_id": operation.id})
            await connection.execute("INSERT INTO testpilot.checkpoints (tenant_id,project_id,task_id,run_id,event_seq,body) VALUES (%s,%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, task.id, task.run_id, sequence, Jsonb({"phase": "operation_dispatch", "operation_id": operation.id})))

    async def outcome(self, task: TaskRow, operation: OperationRow, record: ExecutionRecord | None) -> None:
        self._operation_scope(task, operation)
        if record and (record.task_id != task.id or record.run_id != task.run_id or record.operation_id != operation.id or record.target != task.target):
            raise ValueError("execution scope mismatch")
        async with self.connection() as connection:
            await self._fence(connection, task)
            state = "SUCCEEDED" if record else "UNKNOWN"
            cursor = await connection.execute("UPDATE testpilot.operations SET state=%s,result=%s WHERE tenant_id=%s AND project_id=%s AND id=%s AND state IN ('DISPATCHING','PENDING','UNKNOWN') RETURNING id", (state, Jsonb(record.model_dump(mode="json")) if record else None, self.tenant_id, PROJECT, operation.id))
            if await cursor.fetchone() is None:
                return  # A terminal result must never be overwritten or announced as UNKNOWN by a late attempt.
            if record:
                await connection.execute("UPDATE testpilot.attempts SET dispatch_certainty='SENT',ended_at=clock_timestamp() WHERE tenant_id=%s AND project_id=%s AND operation_id=%s", (self.tenant_id, PROJECT, operation.id))
            await connection.execute("UPDATE testpilot.external_jobs SET state=%s WHERE tenant_id=%s AND project_id=%s AND operation_id=%s", (state, self.tenant_id, PROJECT, operation.id))
            sequence = await self._event(connection, task, "operation." + state.lower(), {"operation_id": operation.id})
            await connection.execute("INSERT INTO testpilot.checkpoints (tenant_id,project_id,task_id,run_id,event_seq,body) VALUES (%s,%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, task.id, task.run_id, sequence, Jsonb({"phase": "operation_result", "operation_id": operation.id, "state": state})))

    async def finish(self, task: TaskRow, report: dict[str, object] | None = None, error: str | None = None,
                     cancelled: bool = False) -> None:
        if report and report.get("report_validated") is True and (
            report.get("task_id") != task.id or report.get("run_id") != task.run_id
            or report.get("target") != task.target.model_dump()
        ):
            raise ValueError("report scope mismatch")
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            if fresh.cancel_requested and not cancelled and error != "CANCELLATION_UNCONFIRMED":
                raise LeaseLostError("cancel takes precedence; cleanup required")
            state = "CANCELLED" if cancelled else "COMPLETED" if report and report.get("report_validated") is True else "NEEDS_REVIEW"
            await connection.execute("UPDATE testpilot.tasks SET state=%s,report=%s,error=%s,lease_owner=NULL,lease_until=NULL WHERE tenant_id=%s AND project_id=%s AND id=%s", (state, Jsonb(report) if report else None, error, self.tenant_id, PROJECT, task.id))
            if report and report.get("report_validated") is True:
                await self._event(connection, task, "report.validated", {"quality_verdict": report.get("quality_verdict")})
            await self._event(connection, task, "task." + state.lower(), {"state": state})

    async def release(self, task: TaskRow) -> None:
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            state = "CANCELLING" if fresh.cancel_requested else "WAITING_EXTERNAL" if fresh.state == "RECONCILING" else "QUEUED"
            await connection.execute("UPDATE testpilot.tasks SET state=%s,lease_owner=NULL,lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND project_id=%s AND id=%s", (state, self.tenant_id, PROJECT, task.id))

    async def cancel(self, task_id: str, actor: str) -> TaskRow | None:
        async with self.connection() as connection:
            await connection.execute("SELECT id FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s AND id=%s AND actor_id=%s FOR UPDATE", (self.tenant_id, PROJECT, task_id, actor))
            task = await self._get(connection, task_id, actor)
            if task and task.state in {"QUEUED", "WAITING_APPROVAL", "RUNNING", "WAITING_EXTERNAL", "RECONCILING", "CANCELLING"}:
                await connection.execute("UPDATE testpilot.tasks SET cancel_requested=true,state='CANCELLING',lease_until=COALESCE(lease_until,clock_timestamp()-interval '1 second') WHERE tenant_id=%s AND project_id=%s AND id=%s", (self.tenant_id, PROJECT, task.id))
                await self._event(connection, task, "task.cancel_requested")
                return await self._get(connection, task_id, actor)
            return task

    @staticmethod
    def _operation_scope(task: TaskRow, operation: OperationRow) -> None:
        if (operation.task_id, operation.run_id, operation.tenant_id, operation.project_id) != (
            task.id, task.run_id, task.tenant_id, task.project_id
        ):
            raise LeaseLostError("operation scope mismatch")

    async def cancel_operation(self, task: TaskRow, operation: OperationRow, record: ExecutionRecord | None) -> None:
        self._operation_scope(task, operation)
        if record and (record.task_id != task.id or record.run_id != task.run_id or record.operation_id != operation.id or record.target != task.target):
            raise ValueError("cancelled execution scope mismatch")
        async with self.connection() as connection:
            await self._fence(connection, task)
            await connection.execute("UPDATE testpilot.operations SET state=CASE WHEN state='SUCCEEDED' THEN state ELSE 'CANCELLED' END,result=COALESCE(result,%s) WHERE tenant_id=%s AND project_id=%s AND id=%s", (Jsonb(record.model_dump(mode="json")) if record else None, self.tenant_id, PROJECT, operation.id))
            await self._event(connection, task, "operation.cancel_resolved", {"operation_id": operation.id})
            await connection.execute("UPDATE testpilot.external_jobs SET state=CASE WHEN state='SUCCEEDED' THEN state ELSE 'CANCELLED' END WHERE tenant_id=%s AND project_id=%s AND operation_id=%s", (self.tenant_id, PROJECT, operation.id))

    async def park(self, task: TaskRow, operation: OperationRow, pending: PendingExecution,
                   delay: float = 5) -> None:
        self._operation_scope(task, operation)
        if pending.operation_id != operation.id or not 0 <= delay <= 30:
            raise ValueError("invalid external wait contract")
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            cursor = await connection.execute(
                "INSERT INTO testpilot.external_jobs (tenant_id,project_id,operation_id,task_id,run_id,external_id,state,deadline_at,next_check_at) "
                "SELECT tenant_id,project_id,id,task_id,run_id,%s,'PENDING',"
                "LEAST(COALESCE(dispatched_at,clock_timestamp())+interval '60 seconds',%s+interval '120 seconds'),"
                "LEAST(clock_timestamp()+(%s*interval '1 second'),COALESCE(dispatched_at,clock_timestamp())+interval '60 seconds',%s+interval '120 seconds') "
                "FROM testpilot.operations WHERE tenant_id=%s AND project_id=%s AND id=%s AND state IN ('DISPATCHING','PENDING','UNKNOWN') "
                "ON CONFLICT (tenant_id,project_id,operation_id) DO UPDATE SET poll_count=testpilot.external_jobs.poll_count+1,next_check_at=LEAST(EXCLUDED.next_check_at,testpilot.external_jobs.deadline_at) "
                "WHERE testpilot.external_jobs.external_id=EXCLUDED.external_id AND testpilot.external_jobs.state='PENDING' RETURNING operation_id",
                (pending.external_run_id, task.activated_at or task.created_at, delay, task.activated_at or task.created_at, self.tenant_id, PROJECT, operation.id),
            )
            if await cursor.fetchone() is None:
                raise ValueError("Job identity/state drift")
            await connection.execute("UPDATE testpilot.operations SET state='PENDING' WHERE tenant_id=%s AND project_id=%s AND id=%s", (self.tenant_id, PROJECT, operation.id))
            state = "CANCELLING" if fresh.cancel_requested else "WAITING_EXTERNAL"
            await connection.execute("UPDATE testpilot.tasks SET state=%s,lease_owner=NULL,lease_until=NULL,next_wakeup_at=(SELECT next_check_at FROM testpilot.external_jobs WHERE tenant_id=%s AND project_id=%s AND operation_id=%s) WHERE tenant_id=%s AND project_id=%s AND id=%s", (state, self.tenant_id, PROJECT, operation.id, self.tenant_id, PROJECT, task.id))
            sequence = await self._event(connection, task, "task." + state.lower(), {"operation_id": operation.id, "reason": "EXTERNAL_JOB"})
            await connection.execute("INSERT INTO testpilot.checkpoints (tenant_id,project_id,task_id,run_id,event_seq,body) VALUES (%s,%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, task.id, task.run_id, sequence, Jsonb(pending.model_dump())))

    async def job_poll(self, task: TaskRow) -> tuple[int, bool]:
        async with self.connection() as connection:
            await self._fence(connection, task)
            cursor = await connection.execute("SELECT j.poll_count,(j.deadline_at<=clock_timestamp() OR COALESCE(t.activated_at,t.created_at)+interval '120 seconds'<=clock_timestamp()) AS expired FROM testpilot.external_jobs j JOIN testpilot.tasks t ON (t.tenant_id,t.project_id,t.id)=(j.tenant_id,j.project_id,j.task_id) WHERE j.tenant_id=%s AND j.project_id=%s AND j.task_id=%s", (self.tenant_id, PROJECT, task.id))
            row = await cursor.fetchone()
            return (int(row["poll_count"]), bool(row["expired"])) if row else (0, False)

    @staticmethod
    async def _deadline(connection: AsyncConnection[dict[str, Any]], task: TaskRow) -> None:
        cursor = await connection.execute("SELECT clock_timestamp()>%s+interval '120 seconds' AS expired", (task.activated_at or task.created_at,))
        row = await cursor.fetchone()
        if row is None or row["expired"]:
            raise BudgetExhaustedError("task deadline exhausted")

    async def requeue(self, task: TaskRow) -> None:
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            state = "CANCELLING" if fresh.cancel_requested else "QUEUED"
            await connection.execute("UPDATE testpilot.tasks SET state=%s,lease_owner=NULL,lease_until=NULL,next_wakeup_at=NULL WHERE tenant_id=%s AND project_id=%s AND id=%s", (state, self.tenant_id, PROJECT, task.id))
            await self._event(connection, task, "task.resumed", {"state": state})

    async def progress(self, task: TaskRow, fingerprints: list[str]) -> None:
        if len(fingerprints) > 16 or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in fingerprints):
            raise ValueError("invalid progress observation")
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            state = ProgressState.model_validate_json(json.dumps(fresh.progress_json))
            updated = advance(state, fingerprints)
            await connection.execute("UPDATE testpilot.tasks SET progress_json=%s WHERE tenant_id=%s AND project_id=%s AND id=%s", (Jsonb(updated.model_dump(mode="json")), self.tenant_id, PROJECT, task.id))
            if updated.stalled and not state.stalled:
                await self._event(connection, task, "progress.stalled", {"reason": "REPEATED_OR_OSCILLATING_OBSERVATIONS"})
        if updated.stalled:
            raise ProgressStalledError("Repeated or oscillating observations without progress")

    async def events(self, task: TaskRow, after: int = 0) -> list[dict[str, Any]]:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT event_seq,run_id,type,public_payload,created_at FROM testpilot.task_events WHERE tenant_id=%s AND project_id=%s AND task_id=%s AND event_seq>%s ORDER BY event_seq LIMIT 100", (self.tenant_id, PROJECT, task.id, after))
            return await cursor.fetchall()

    async def event_window(self, task: TaskRow) -> tuple[int | None, int | None]:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT min(event_seq) AS first,max(event_seq) AS last FROM testpilot.task_events WHERE tenant_id=%s AND project_id=%s AND task_id=%s", (self.tenant_id, PROJECT, task.id))
            row = await cursor.fetchone()
            assert row is not None
            return row["first"], row["last"]

    async def approval(self, task_id: str) -> dict[str, Any] | None:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT a.*,t.target,t.goal,t.mode FROM testpilot.approvals a JOIN testpilot.tasks t ON (t.tenant_id,t.project_id,t.id)=(a.tenant_id,a.project_id,a.task_id) WHERE a.tenant_id=%s AND a.project_id=%s AND a.task_id=%s", (self.tenant_id, PROJECT, task_id))
            return await cursor.fetchone()

    async def decide(self, task_id: str, reviewer: str, request_hash: str, approve: bool) -> None:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT * FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s AND id=%s FOR UPDATE", (self.tenant_id, PROJECT, task_id))
            row = await cursor.fetchone()
            if row is None:
                raise RequestConflictError("approval target not found")
            task = TaskRow.model_validate(row)
            if task.state != "WAITING_APPROVAL" or task.actor_id == reviewer:
                raise ApprovalDeniedError("independent reviewer and pending approval required")
            expected = execution_hash(task.id, task.run_id, task.target, task.goal, task.mode)
            if request_hash != expected:
                raise RequestConflictError("approval hash conflict")
            cursor = await connection.execute("UPDATE testpilot.approvals SET status=%s,reviewer_id=%s WHERE tenant_id=%s AND project_id=%s AND run_id=%s AND request_hash=%s AND status='PENDING' AND expires_at>clock_timestamp() RETURNING id", ("APPROVED" if approve else "DENIED", reviewer, self.tenant_id, PROJECT, task.run_id, request_hash))
            if await cursor.fetchone() is None:
                raise RequestConflictError("approval expired/resolved")
            await connection.execute("UPDATE testpilot.tasks SET state=%s,error=%s,activated_at=CASE WHEN %s THEN clock_timestamp() ELSE activated_at END WHERE tenant_id=%s AND project_id=%s AND id=%s", ("QUEUED" if approve else "NEEDS_REVIEW", None if approve else "APPROVAL_DENIED", approve, self.tenant_id, PROJECT, task.id))
            await self._event(connection, task, "approval.resolved", {"decision": "APPROVE" if approve else "DENY", "request_hash": request_hash})

    async def save_plan(self, task: TaskRow, plan: TaskPlan) -> int:
        plan.validate_scope()
        async with self.connection() as connection:
            await self._fence(connection, task)
            cursor = await connection.execute("SELECT COALESCE(max(version),0) AS version FROM testpilot.plans WHERE tenant_id=%s AND project_id=%s AND run_id=%s", (self.tenant_id, PROJECT, task.run_id))
            row = await cursor.fetchone()
            assert row is not None
            version = int(row["version"])+1
            if version > 3:
                raise BudgetExhaustedError("plan revision budget exhausted")
            await connection.execute("INSERT INTO testpilot.plans (tenant_id,project_id,task_id,run_id,version,body) VALUES (%s,%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, task.id, task.run_id, version, Jsonb(plan.model_dump(mode="json"))))
            await self._event(connection, task, "plan.updated", {"version": version})
            return version

    async def latest_plan(self, task: TaskRow) -> dict[str, Any] | None:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT version,body FROM testpilot.plans WHERE tenant_id=%s AND project_id=%s AND run_id=%s ORDER BY version DESC LIMIT 1", (self.tenant_id, PROJECT, task.run_id))
            return await cursor.fetchone()

    async def save_knowledge(self, task: TaskRow, item: KnowledgeEvidence) -> None:
        result = KnowledgeResult.model_validate_json(item.result_json)
        if result.insufficient_evidence or not result.citations:
            raise ValueError("No authorized knowledge evidence")
        async with self.connection() as connection:
            await self._fence(connection, task)
            await connection.execute("INSERT INTO testpilot.knowledge_evidence (tenant_id,project_id,task_id,run_id,evidence_id,query_hash,body) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (self.tenant_id, PROJECT, task.id, task.run_id, item.evidence_id, item.query_hash, Jsonb(item.model_dump(mode="json"))))
            await self._event(connection, task, "knowledge.retrieved", {"evidence_id": item.evidence_id, "citation_count": len(result.citations), "degraded": list(result.degraded)})

    async def knowledge_artifacts(self, task: TaskRow) -> list[str]:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT body->>'artifact_hash' AS hash FROM testpilot.knowledge_evidence WHERE tenant_id=%s AND project_id=%s AND run_id=%s ORDER BY evidence_id", (self.tenant_id, PROJECT, task.run_id))
            return [str(row["hash"]) for row in await cursor.fetchall()]

    async def knowledge_ids(self, task: TaskRow) -> list[str]:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT evidence_id FROM testpilot.knowledge_evidence WHERE tenant_id=%s AND project_id=%s AND run_id=%s", (self.tenant_id, PROJECT, task.run_id))
            return [str(row["evidence_id"]) for row in await cursor.fetchall()]

    async def list_tasks(self, actor: str, review_queue: bool = False) -> list[TaskRow]:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT * FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s AND ((%s AND state='WAITING_APPROVAL') OR actor_id=%s) ORDER BY created_at DESC LIMIT 100", (self.tenant_id, PROJECT, review_queue, actor))
            return [TaskRow.model_validate(row) for row in await cursor.fetchall()]
