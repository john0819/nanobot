"""Loopback development Task API; bounded asynchronous tasks and scoped results.

    In-memory task admission is explicitly not a PostgreSQL task ledger. Process
    restart loses task lookup/idempotency; it must not be used as shared deployment.
"""

import asyncio
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from aiohttp import web
from pydantic import Field, ValidationError

from testpilot.artifacts import ArtifactStore
from testpilot.domain import Contract
from testpilot.evidence import render_markdown
from testpilot.execution import FixtureMode
from testpilot.executor_contract import FixtureExecutor

TaskState = Literal["QUEUED", "RUNNING", "COMPLETED", "NEEDS_REVIEW", "CANCELLED"]
TaskRunner = Callable[[FixtureExecutor], Awaitable[dict[str, object]]]
ExecutorFactory = Callable[[ArtifactStore, str, FixtureMode], FixtureExecutor]


class TaskRequest(Contract):
    mode: FixtureMode = "healthy"
    goal: str = Field(default="Validate gateway fixture", min_length=1, max_length=1000)
    require_approval: bool = False
    require_knowledge: bool = False


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str = "novax-demo"
    roles: tuple[str, ...] = ("executor",)
    projects: tuple[str, ...] = ("gateway-fixture",)
    groups: tuple[str, ...] = ("role:qa",)


@dataclass
class Task:
    task_id: str
    owner: Principal
    request: TaskRequest
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    state: TaskState = "QUEUED"
    report: dict[str, object] | None = None
    error: str | None = None
    executor: FixtureExecutor | None = None
    worker: asyncio.Task[None] | None = None

    def snapshot(self) -> dict[str, object]:
        return {"task_id": self.task_id, "state": self.state, "mode": self.request.mode,
                "created_at": self.created_at, "error": self.error,
                "report_ready": self.report is not None,
                "operation_id": self.executor.operation_id if self.executor else None}


def create_app(
    *, tokens: dict[str, Principal], root: Path, executor_factory: ExecutorFactory,
    run: TaskRunner, max_active: int = 2, max_tasks: int = 100,
) -> web.Application:
    if not tokens or any(len(token) < 32 or principal.tenant_id != "novax-demo" for token, principal in tokens.items()):
        raise ValueError("Local API needs strong tokens and the fixed demo tenant")
    if max_active < 1 or max_tasks < max_active:
        raise ValueError("invalid task capacity")
    tasks: dict[str, Task] = {}
    idempotency: dict[tuple[Principal, str], tuple[TaskRequest, str]] = {}
    lock = asyncio.Lock()
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    def authenticate(request: web.Request) -> Principal:
        provided = request.headers.get("Authorization", "")
        for token, principal in tokens.items():
            if hmac.compare_digest(provided.encode(), f"Bearer {token}".encode()):
                return principal
        raise web.HTTPUnauthorized(text="Authentication required")

    def authorized_task(request: web.Request) -> Task:
        principal = authenticate(request)
        task = tasks.get(request.match_info["task_id"])
        if task is None or task.owner != principal:
            raise web.HTTPNotFound(text="Task not found")
        return task

    async def work(task: Task) -> None:
        task.state = "RUNNING"
        output = root / task.task_id
        try:
            store = ArtifactStore(output / "artifacts")
            task.executor = executor_factory(store, task.task_id, task.request.mode)
            output.joinpath("operation.json").write_text(json.dumps({
                "task_id": task.task_id, "operation_id": task.executor.operation_id,
                "target": task.executor.target.model_dump(),
            }, indent=2), encoding="utf-8")
            report = await asyncio.wait_for(run(task.executor), timeout=90)
            # Safe output only; no raw model final content or internal exception is persisted here.
            output.joinpath("report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            output.joinpath("report.md").write_text(render_markdown(report), encoding="utf-8")
            if task.executor.record is not None:
                output.joinpath("execution.json").write_text(task.executor.record.model_dump_json(indent=2), encoding="utf-8")
            task.report = report
            task.state = "COMPLETED" if report.get("report_validated") is True else "NEEDS_REVIEW"
        except asyncio.CancelledError:
            # Executor must finish/confirm container cleanup before propagating cancellation.
            task.state = "CANCELLED"
            raise
        except TimeoutError:
            task.state, task.error = "NEEDS_REVIEW", "TASK_DEADLINE"
        except Exception:
            task.state, task.error = "NEEDS_REVIEW", "TASK_EXECUTION_UNRESOLVED"

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "deployment": "local-development"})

    async def openapi(request: web.Request) -> web.Response:
        authenticate(request)
        from testpilot.openapi import document

        return web.json_response(document())

    async def create(request: web.Request) -> web.Response:
        principal = authenticate(request)
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
            raise web.HTTPUnprocessableEntity(text="Idempotency-Key required")
        try:
            spec = TaskRequest.model_validate_json(await request.read())
            if spec.require_approval or spec.require_knowledge or spec.goal != "Validate gateway fixture":
                raise web.HTTPUnprocessableEntity(text="Governed tasks require the PostgreSQL durable profile")
        except ValidationError:
            raise web.HTTPUnprocessableEntity(text="Invalid task contract") from None
        async with lock:
            previous = idempotency.get((principal, key))
            if previous is not None:
                if previous[0] != spec:
                    raise web.HTTPConflict(text="Idempotency key bound to another request")
                return web.json_response(tasks[previous[1]].snapshot(), status=200)
            active = sum(task.state in {"QUEUED", "RUNNING"} for task in tasks.values())
            if active >= max_active or len(tasks) >= max_tasks:
                raise web.HTTPTooManyRequests(text="Task capacity reached")
            task = Task(f"task_{uuid4().hex}", principal, spec)
            tasks[task.task_id] = task
            idempotency[(principal, key)] = (spec, task.task_id)
            task.worker = asyncio.create_task(work(task), name=task.task_id)
            return web.json_response(task.snapshot(), status=202, headers={"Location": f"/v1/tasks/{task.task_id}"})

    async def get(request: web.Request) -> web.Response:
        return web.json_response(authorized_task(request).snapshot())

    async def get_report(request: web.Request) -> web.Response:
        task = authorized_task(request)
        if task.report is None:
            raise web.HTTPConflict(text="Report not ready")
        return web.json_response(task.report)

    async def artifact(request: web.Request) -> web.Response:
        task = authorized_task(request)
        content_hash = request.match_info["hash"]
        refs = task.report.get("artifact_refs") if task.report else None
        if not isinstance(refs, list) or content_hash not in refs:
            raise web.HTTPNotFound(text="Artifact not found")
        if task.executor is None:
            raise web.HTTPNotFound(text="Artifact not found")
        try:
            content = task.executor.store.read(content_hash)
        except (ValueError, OSError):
            raise web.HTTPConflict(text="Artifact integrity check failed") from None
        return web.Response(body=content, content_type="application/octet-stream", headers={"X-Content-Type-Options": "nosniff"})

    async def cancel(request: web.Request) -> web.Response:
        task = authorized_task(request)
        if task.state in {"QUEUED", "RUNNING"} and task.worker is not None:
            if not task.worker.cancelling():
                task.worker.cancel()
            try:
                await asyncio.shield(task.worker)
            except asyncio.CancelledError:
                if task.worker.done() and task.worker.cancelled() and task.state == "QUEUED":
                    task.state = "CANCELLED"
        return web.json_response(task.snapshot())

    async def cleanup(_app: web.Application) -> None:
        pending = [task.worker for task in tasks.values() if task.worker is not None and not task.worker.done()]
        for worker in pending:
            if not worker.cancelling():
                worker.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    app = web.Application(client_max_size=8192)
    app.router.add_get("/health/live", health)
    app.router.add_get("/openapi.json", openapi)
    app.router.add_post("/v1/tasks", create)
    app.router.add_get("/v1/tasks/{task_id}", get)
    app.router.add_get("/v1/tasks/{task_id}/report", get_report)
    app.router.add_get("/v1/tasks/{task_id}/artifacts/{hash}", artifact)
    app.router.add_post("/v1/tasks/{task_id}/cancel", cancel)
    app.on_cleanup.append(cleanup)
    return app
