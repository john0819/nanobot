"""Independent authenticated Runner service with persistent idempotency and Docker authority."""

import asyncio
import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any

from aiohttp import web
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from testpilot.domain import ExecutionRecord, PendingExecution
from testpilot.recoverable_runner import RetainedRunner
from testpilot.runner_contracts import RunnerFactory
from testpilot.storage.postgres import LeaseLostError, Ledger, OperationRow, TaskRow
from testpilot.storage_contracts import ArtifactFactory, local_artifacts


class Submission(BaseModel):
    task: TaskRow
    operation: OperationRow

    def request_hash(self) -> str:
        intent = {"task_id": self.task.id, "run_id": self.task.run_id, "target": self.task.target.model_dump(),
                  "mode": self.task.mode, "operation_id": self.operation.id}
        return hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()


def create_runner_app(ledger: Ledger, root: Path, image: str, token: str, *,
                      runner: RunnerFactory = RetainedRunner, artifacts: ArtifactFactory = local_artifacts,
                      max_jobs: int = 4) -> web.Application:
    if len(token) < 32:
        raise ValueError("Strong Runner service credential required")
    if not 1 <= max_jobs <= 16:
        raise ValueError("Runner slot limit out of range")
    root = root.resolve()
    active: asyncio.Task[None] | None = None

    def auth(request: web.Request) -> None:
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer "+token).encode()):
            raise web.HTTPUnauthorized(text="Runner authentication required")

    async def load(operation_id: str) -> dict[str, Any] | None:
        async with ledger.connection() as connection:
            cursor = await connection.execute("SELECT * FROM testpilot.runner_jobs WHERE tenant_id=%s AND project_id=%s AND operation_id=%s", (ledger.tenant_id, "gateway-fixture", operation_id))
            return await cursor.fetchone()

    def receipt(row: dict[str, Any]) -> dict[str, object]:
        return {"job_id": "testpilot-"+str(row["operation_id"]), "operation_id": row["operation_id"],
                "state": row["state"], "record": row["result"]}

    async def submit(request: web.Request) -> web.Response:
        auth(request)
        try:
            spec = Submission.model_validate_json(await request.read())
        except (ValueError, ValidationError):
            raise web.HTTPUnprocessableEntity(text="Invalid Runner submission") from None
        if spec.task.tenant_id != ledger.tenant_id or spec.task.project_id != "gateway-fixture" or request.headers.get("Idempotency-Key") != "testpilot-"+spec.operation.id:
            raise web.HTTPForbidden(text="Runner scope denied")
        async with ledger.connection() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("runner:"+spec.operation.id,))
            cursor = await connection.execute("SELECT * FROM testpilot.runner_jobs WHERE tenant_id=%s AND project_id=%s AND operation_id=%s", (ledger.tenant_id, spec.task.project_id, spec.operation.id))
            previous = await cursor.fetchone()
            if previous:
                if previous["request_hash"] != spec.request_hash():
                    raise web.HTTPConflict(text="Runner idempotency payload conflict")
                return web.json_response(receipt(previous))
            try:
                fresh, operation = await ledger.authorize_runner(connection, spec.task, spec.operation)
            except LeaseLostError:
                raise web.HTTPConflict(text="Runner dispatch not authorized") from None
            # Validate target/profile/source before accepting external work.
            runner(fresh, operation, artifacts(root, ledger.tenant_id, fresh.id), image, root/"jobs"/ledger.tenant_id)
            cursor = await connection.execute("INSERT INTO testpilot.runner_jobs (tenant_id,project_id,operation_id,request_hash,task_body,operation_body) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *", (ledger.tenant_id, fresh.project_id, operation.id, spec.request_hash(), Jsonb(fresh.model_dump(mode="json")), Jsonb(operation.model_dump(mode="json"))))
            row = await cursor.fetchone()
            assert row is not None
        return web.json_response(receipt(row), status=202)

    async def status(request: web.Request) -> web.Response:
        auth(request)
        operation_id = request.match_info["operation_id"]
        if operation_id.startswith("testpilot-"):
            operation_id = operation_id[len("testpilot-"):]
        row = await load(operation_id)
        if row is None:
            raise web.HTTPNotFound(text="Runner operation not found")
        if request.method == "POST":
            async with ledger.connection() as connection:
                await connection.execute("UPDATE testpilot.runner_jobs SET cancel_requested=true WHERE tenant_id=%s AND project_id=%s AND operation_id=%s", (ledger.tenant_id, "gateway-fixture", operation_id))
        return web.json_response(receipt(row))

    async def artifact(request: web.Request) -> web.Response:
        auth(request)
        row = await load(request.match_info["operation_id"].removeprefix("testpilot-"))
        if row is None or row["result"] is None:
            raise web.HTTPNotFound()
        record = ExecutionRecord.model_validate_json(json.dumps(row["result"]))
        ref = request.match_info["hash"]
        if not re.fullmatch(r"[a-f0-9]{64}", ref) or ref not in {record.junit_hash, record.log_hash}:
            raise web.HTTPNotFound()
        data = await asyncio.to_thread(artifacts(root, ledger.tenant_id, record.task_id).read, ref)
        return web.Response(body=data, content_type="application/octet-stream")

    async def loop() -> None:
        while True:
            try:
                epoch = 0
                async with ledger.connection() as connection:
                    await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("runner-slots:"+ledger.tenant_id,))
                    cursor = await connection.execute("SELECT * FROM testpilot.runner_jobs j WHERE j.tenant_id=%s AND j.project_id='gateway-fixture' AND j.state IN ('ACCEPTED','DISPATCHING','RUNNING','UNKNOWN') AND COALESCE(j.lease_until,clock_timestamp())<=clock_timestamp() AND (j.state<>'ACCEPTED' OR j.cancel_requested OR (SELECT count(*) FROM testpilot.runner_jobs a WHERE a.tenant_id=j.tenant_id AND a.project_id=j.project_id AND a.state IN ('DISPATCHING','RUNNING','UNKNOWN'))<%s) ORDER BY j.lease_until NULLS FIRST,j.created_at LIMIT 1 FOR UPDATE SKIP LOCKED", (ledger.tenant_id, max_jobs))
                    row = await cursor.fetchone()
                    if row:
                        cursor = await connection.execute("UPDATE testpilot.runner_jobs SET state='DISPATCHING',lease_epoch=lease_epoch+1,lease_until=clock_timestamp()+interval '15 seconds' WHERE tenant_id=%s AND project_id=%s AND operation_id=%s RETURNING lease_epoch", (ledger.tenant_id, row["project_id"], row["operation_id"]))
                        claimed = await cursor.fetchone()
                        assert claimed is not None
                        epoch = claimed["lease_epoch"]
                if row is None:
                    await asyncio.sleep(0.1)
                    continue
                task = TaskRow.model_validate_json(json.dumps(row["task_body"]))
                operation = OperationRow.model_validate_json(json.dumps(row["operation_body"]))
                backend = runner(task, operation, artifacts(root, ledger.tenant_id, task.id), image, root/"jobs"/ledger.tenant_id)
                if row["state"] != "ACCEPTED":
                    backend.note_dispatch()
                result: ExecutionRecord | PendingExecution | None = None
                state = "UNKNOWN"
                try:
                    # Re-check PG immediately before any external action. An accepted queue item
                    # cannot outlive cancellation, a newer Run, or the original execution deadline.
                    async with ledger.connection() as connection:
                        cursor = await connection.execute("SELECT (t.cancel_requested OR t.run_id<>%s OR t.state IN ('COMPLETED','NEEDS_REVIEW','CANCELLED') OR clock_timestamp()>COALESCE(o.dispatched_at,%s)+interval '60 seconds' OR clock_timestamp()>COALESCE(t.activated_at,t.created_at)+interval '120 seconds') AS stop FROM testpilot.tasks t JOIN testpilot.operations o ON (o.tenant_id,o.project_id,o.task_id)=(t.tenant_id,t.project_id,t.id) WHERE t.tenant_id=%s AND t.project_id=%s AND o.id=%s", (task.run_id, row["created_at"], ledger.tenant_id, task.project_id, operation.id))
                        admitted = await cursor.fetchone()
                    if row["cancel_requested"] or admitted is None or admitted["stop"]:
                        result = None if row["state"] == "ACCEPTED" else await backend.cancel()
                        state = "CANCELLED"
                    else:
                        result = await backend.submit() if row["state"] == "ACCEPTED" else await backend.poll()
                        state = "RUNNING" if isinstance(result, PendingExecution) else "FINISHED"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    state = "UNKNOWN"  # No automatic invoke after an ambiguous side effect.
                record = result.model_copy(update={"external_run_id": "testpilot-"+operation.id}) if isinstance(result, ExecutionRecord) else None
                async with ledger.connection() as connection:
                    await connection.execute("UPDATE testpilot.runner_jobs SET state=%s,result=%s,lease_until=clock_timestamp()+interval '0.2 seconds' WHERE tenant_id=%s AND project_id=%s AND operation_id=%s AND lease_epoch=%s", (state, Jsonb(record.model_dump(mode="json")) if record else None, ledger.tenant_id, task.project_id, operation.id, epoch))
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.5)

    async def start(app: web.Application) -> None:
        nonlocal active
        await ledger.ready()
        active = asyncio.create_task(loop())

    async def stop(app: web.Application) -> None:
        if active:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)

    app = web.Application(client_max_size=262144)
    app.router.add_post("/runner/v1/jobs", submit)
    app.router.add_get("/runner/v1/operations/{operation_id}", status)
    app.router.add_get("/runner/v1/jobs/{operation_id}", status)
    app.router.add_post("/runner/v1/jobs/{operation_id}/cancel", status)
    app.router.add_get("/runner/v1/jobs/{operation_id}/artifacts/{hash}", artifact)
    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app
