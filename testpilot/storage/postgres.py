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

from testpilot.domain import Contract, ExecutionRecord, Target
from testpilot.execution import FixtureMode

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
    state: Literal["QUEUED", "RUNNING", "CANCELLING", "COMPLETED", "NEEDS_REVIEW", "CANCELLED"]
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

    def snapshot(self, operation_id: str | None = None) -> dict[str, object]:
        return {"task_id": self.id, "run_id": self.run_id, "state": self.state, "mode": self.mode,
                "created_at": self.created_at.isoformat(), "report_ready": self.report is not None,
                "error": self.error, "operation_id": operation_id, "lease_epoch": self.lease_epoch,
                "model_rounds": self.model_rounds}


class OperationRow(Contract):
    tenant_id: str
    project_id: str
    id: str
    task_id: str
    run_id: str
    intent_ref: str
    state: Literal["PREPARED", "DISPATCHING", "UNKNOWN", "SUCCEEDED", "CANCELLED"]
    result: dict[str, object] | None
    external_id: str | None
    lease_epoch: int

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
        source = Path(__file__).with_name("001_ledger.sql").read_text(encoding="utf-8")
        checksum = hashlib.sha256(source.encode()).hexdigest()
        async with self.connection() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(8920001)")
            await connection.execute("CREATE SCHEMA IF NOT EXISTS testpilot")
            await connection.execute("CREATE TABLE IF NOT EXISTS testpilot.migrations (version integer PRIMARY KEY, checksum text NOT NULL)")
            cursor = await connection.execute("SELECT checksum FROM testpilot.migrations WHERE version=1")
            previous = await cursor.fetchone()
            if previous:
                if previous["checksum"] != checksum:
                    raise ValueError("migration checksum drift; add a new migration")
                return
            await connection.execute(source.encode())
            await connection.execute("INSERT INTO testpilot.migrations VALUES (1,%s)", (checksum,))

    async def ready(self) -> None:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT checksum FROM testpilot.migrations WHERE version=1")
            row = await cursor.fetchone()
            expected = hashlib.sha256(Path(__file__).with_name("001_ledger.sql").read_text(encoding="utf-8").encode()).hexdigest()
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
                    max_active: int = 10) -> tuple[TaskRow, bool]:
        if target.tenant_id != self.tenant_id or target.project_id != PROJECT:
            raise ValueError("target scope mismatch")
        async with self.connection() as connection:
            # Serialize admission in this tenant/project; quotas and dedup share one transaction.
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.tenant_id + ":" + PROJECT,))
            cursor = await connection.execute("SELECT mode,task_id FROM testpilot.request_dedup WHERE tenant_id=%s AND project_id=%s AND actor_id=%s AND request_key=%s", (self.tenant_id, PROJECT, actor, key))
            previous = await cursor.fetchone()
            if previous:
                if previous["mode"] != mode:
                    raise RequestConflictError("key bound to another request")
                task = await self._get(connection, previous["task_id"], actor)
                assert task is not None
                return task, False
            cursor = await connection.execute("SELECT count(*) AS count FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s AND state IN ('QUEUED','RUNNING','CANCELLING')", (self.tenant_id, PROJECT))
            count = await cursor.fetchone()
            if count is None or count["count"] >= max_active:
                raise CapacityReachedError("queue capacity reached")
            task_id, run_id = "task_" + uuid4().hex, "run_" + uuid4().hex
            cursor = await connection.execute(
                "INSERT INTO testpilot.tasks (tenant_id,project_id,id,actor_id,mode,run_id,target) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (self.tenant_id, PROJECT, task_id, actor, mode, run_id, Jsonb(target.model_dump())),
            )
            row = await cursor.fetchone()
            assert row is not None
            task = TaskRow.model_validate(row)
            await connection.execute("INSERT INTO testpilot.task_runs (tenant_id,project_id,id,task_id,target) VALUES (%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, run_id, task_id, Jsonb(target.model_dump())))
            await connection.execute("INSERT INTO testpilot.request_dedup VALUES (%s,%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, actor, key, mode, task_id))
            await self._event(connection, task, "task.created")
            return task, True

    async def _get(self, connection: AsyncConnection[dict[str, Any]], task_id: str, actor: str) -> TaskRow | None:
        cursor = await connection.execute("SELECT * FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s AND id=%s AND actor_id=%s", (self.tenant_id, PROJECT, task_id, actor))
        row = await cursor.fetchone()
        return TaskRow.model_validate(row) if row else None

    async def get(self, task_id: str, actor: str) -> TaskRow | None:
        async with self.connection() as connection:
            return await self._get(connection, task_id, actor)

    async def claim(self, owner: str, lease_seconds: int = 30) -> TaskRow | None:
        if not 1 <= lease_seconds <= 300:
            raise ValueError("invalid lease duration")
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s "
                "AND (state='QUEUED' OR (state IN ('RUNNING','CANCELLING') AND lease_until < clock_timestamp())) "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1", (self.tenant_id, PROJECT),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            cursor = await connection.execute(
                "UPDATE testpilot.tasks SET state=CASE WHEN cancel_requested THEN 'CANCELLING' ELSE 'RUNNING' END,"
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
            "AND state IN ('RUNNING','CANCELLING') FOR UPDATE",
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
            if fresh.cancel_requested or fresh.model_rounds >= 4:
                raise BudgetExhaustedError("cancelled or model round budget exhausted")
            await connection.execute("UPDATE testpilot.tasks SET model_rounds=model_rounds+1 WHERE tenant_id=%s AND project_id=%s AND id=%s", (self.tenant_id, PROJECT, task.id))

    async def checkpoint(self, task: TaskRow, body: dict[str, Any]) -> None:
        if len(json.dumps(body).encode()) > 131072:
            raise ValueError("checkpoint exceeds bounded fragment limit")
        async with self.connection() as connection:
            await self._fence(connection, task)
            sequence = await self._event(connection, task, "checkpoint.saved", {"phase": str(body.get("phase", "unknown"))})
            await connection.execute("INSERT INTO testpilot.checkpoints (tenant_id,project_id,task_id,run_id,event_seq,body) VALUES (%s,%s,%s,%s,%s,%s)", (self.tenant_id, PROJECT, task.id, task.run_id, sequence, Jsonb(body)))

    async def operation(self, task: TaskRow) -> OperationRow:
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            if fresh.cancel_requested:
                raise BudgetExhaustedError("cancel requested")
            await connection.execute("INSERT INTO testpilot.operations (tenant_id,project_id,id,task_id,run_id,lease_epoch) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,project_id,run_id,intent_ref) DO NOTHING", (self.tenant_id, PROJECT, "op_" + uuid4().hex, task.id, task.run_id, task.lease_epoch))
            cursor = await connection.execute("SELECT * FROM testpilot.operations WHERE tenant_id=%s AND project_id=%s AND run_id=%s", (self.tenant_id, PROJECT, task.run_id))
            row = await cursor.fetchone()
            assert row is not None
            return OperationRow.model_validate(row)

    async def find_operation(self, task: TaskRow) -> OperationRow | None:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT * FROM testpilot.operations WHERE tenant_id=%s AND project_id=%s AND run_id=%s", (self.tenant_id, PROJECT, task.run_id))
            row = await cursor.fetchone()
            return OperationRow.model_validate(row) if row else None

    async def dispatch(self, task: TaskRow, operation: OperationRow) -> None:
        self._operation_scope(task, operation)
        async with self.connection() as connection:
            fresh = await self._fence(connection, task)
            if fresh.cancel_requested:
                raise BudgetExhaustedError("cancel requested")
            cursor = await connection.execute("UPDATE testpilot.operations SET state='DISPATCHING',lease_epoch=%s,external_id=%s WHERE tenant_id=%s AND project_id=%s AND id=%s AND state='PREPARED' RETURNING id", (task.lease_epoch, "testpilot-" + operation.id, self.tenant_id, PROJECT, operation.id))
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
            cursor = await connection.execute("UPDATE testpilot.operations SET state=%s,result=%s WHERE tenant_id=%s AND project_id=%s AND id=%s AND state IN ('DISPATCHING','UNKNOWN') RETURNING id", (state, Jsonb(record.model_dump(mode="json")) if record else None, self.tenant_id, PROJECT, operation.id))
            if await cursor.fetchone() is None:
                return  # A terminal result must never be overwritten or announced as UNKNOWN by a late attempt.
            if record:
                await connection.execute("UPDATE testpilot.attempts SET dispatch_certainty='SENT',ended_at=clock_timestamp() WHERE tenant_id=%s AND project_id=%s AND operation_id=%s", (self.tenant_id, PROJECT, operation.id))
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
            await connection.execute("UPDATE testpilot.tasks SET state=%s,lease_owner=NULL,lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND project_id=%s AND id=%s", ("QUEUED" if not fresh.cancel_requested else "CANCELLING", self.tenant_id, PROJECT, task.id))

    async def cancel(self, task_id: str, actor: str) -> TaskRow | None:
        async with self.connection() as connection:
            await connection.execute("SELECT id FROM testpilot.tasks WHERE tenant_id=%s AND project_id=%s AND id=%s AND actor_id=%s FOR UPDATE", (self.tenant_id, PROJECT, task_id, actor))
            task = await self._get(connection, task_id, actor)
            if task and task.state in {"QUEUED", "RUNNING", "CANCELLING"}:
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

    async def events(self, task: TaskRow, after: int = 0) -> list[dict[str, Any]]:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT event_seq,type,public_payload,created_at FROM testpilot.task_events WHERE tenant_id=%s AND project_id=%s AND task_id=%s AND event_seq>%s ORDER BY event_seq LIMIT 100", (self.tenant_id, PROJECT, task.id, after))
            return await cursor.fetchall()
