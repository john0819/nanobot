"""Same local HTTP contracts, PostgreSQL-owned task lifecycle and worker queue."""

import hmac
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web
from psycopg import Error as PostgreSQLError
from pydantic import TypeAdapter, ValidationError

from testpilot.artifacts import ArtifactStore
from testpilot.domain import Target
from testpilot.durable_runtime import DurableTaskRunner, WorkerPool
from testpilot.execution import FixtureMode
from testpilot.openapi import document
from testpilot.storage.postgres import CapacityReachedError, Ledger, RequestConflictError, TaskRow
from testpilot.task_api import Principal, TaskRequest


def create_app(
    *, tokens: dict[str, Principal], ledger: Ledger, root: Path, image: str,
    target: Callable[[FixtureMode], Target], run: DurableTaskRunner,
    workers: int = 2, lease_seconds: int = 30,
) -> web.Application:
    if not tokens or any(len(token) < 32 or owner.tenant_id != ledger.tenant_id for token, owner in tokens.items()):
        raise ValueError("strong local identity tokens matching DB scope required")
    root = root.resolve()
    pool = WorkerPool(ledger, root, image, run, workers, lease_seconds)

    def auth(request: web.Request) -> Principal:
        provided = request.headers.get("Authorization", "").encode()
        for token, owner in tokens.items():
            if hmac.compare_digest(provided, ("Bearer " + token).encode()):
                return owner
        raise web.HTTPUnauthorized(text="Authentication required")

    async def get_task(request: web.Request) -> TaskRow:
        owner = auth(request)
        task = await ledger.get(request.match_info["task_id"], owner.user_id)
        if task is None:
            raise web.HTTPNotFound(text="Task not found")
        return task

    @web.middleware
    async def database_errors(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        try:
            return await handler(request)
        except PostgreSQLError:
            raise web.HTTPServiceUnavailable(text="Task store unavailable; no new dispatch") from None

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "deployment": "postgres-local-development"})

    async def ready(_request: web.Request) -> web.Response:
        await ledger.ready()
        return web.json_response({"status": "ready"})

    async def openapi(request: web.Request) -> web.Response:
        auth(request)
        return web.json_response(document(durable=True))

    async def create(request: web.Request) -> web.Response:
        owner = auth(request)
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
            raise web.HTTPUnprocessableEntity(text="Idempotency-Key required")
        try:
            spec = TaskRequest.model_validate_json(await request.read())
        except ValidationError:
            raise web.HTTPUnprocessableEntity(text="Invalid task contract") from None
        try:
            task, created = await ledger.admit(owner.user_id, key, spec.mode, target(spec.mode))
        except RequestConflictError:
            raise web.HTTPConflict(text="Idempotency key bound to another request") from None
        except CapacityReachedError:
            raise web.HTTPTooManyRequests(text="Task capacity reached") from None
        return web.json_response(task.snapshot(), status=202 if created else 200)

    async def get(request: web.Request) -> web.Response:
        task = await get_task(request)
        operation = await ledger.find_operation(task)
        return web.json_response(task.snapshot(operation.id if operation else None))

    async def report(request: web.Request) -> web.Response:
        task = await get_task(request)
        if task.report is None:
            raise web.HTTPConflict(text="Report not ready")
        store = ArtifactStore(root / ledger.tenant_id / task.id / "artifacts")
        try:
            refs = TypeAdapter(list[str]).validate_python(task.report.get("artifact_refs", []), strict=True)
            for content_hash in refs:
                store.read(content_hash)
        except (ValueError, OSError):
            raise web.HTTPConflict(text="Report evidence unavailable or corrupted; review required") from None
        return web.json_response(task.report)

    async def artifact(request: web.Request) -> web.Response:
        task = await get_task(request)
        refs = task.report.get("artifact_refs") if task.report else None
        content_hash = request.match_info["hash"]
        if not isinstance(refs, list) or content_hash not in refs:
            raise web.HTTPNotFound(text="Artifact not found")
        try:
            content = ArtifactStore(root / ledger.tenant_id / task.id / "artifacts").read(content_hash)
        except (ValueError, OSError):
            raise web.HTTPConflict(text="Artifact integrity check failed") from None
        return web.Response(body=content, content_type="application/octet-stream", headers={"X-Content-Type-Options": "nosniff"})

    async def cancel(request: web.Request) -> web.Response:
        owner = auth(request)
        task = await ledger.cancel(request.match_info["task_id"], owner.user_id)
        if task is None:
            raise web.HTTPNotFound(text="Task not found")
        return web.json_response(task.snapshot(), status=202 if task.state == "CANCELLING" else 200)

    async def events(request: web.Request) -> web.Response:
        task = await get_task(request)
        try:
            after = int(request.query.get("after", "0"))
            if not 0 <= after <= 9223372036854775807:
                raise ValueError
        except ValueError:
            raise web.HTTPUnprocessableEntity(text="Invalid event cursor") from None
        rows = await ledger.events(task, after)
        for row in rows:
            row["created_at"] = row["created_at"].isoformat()
        return web.json_response({"events": rows})

    async def startup(_app: web.Application) -> None:
        await pool.start()

    async def shutdown(_app: web.Application) -> None:
        await pool.stop()  # Suspend workers; retained jobs continue and will be reconciled.

    app = web.Application(client_max_size=8192, middlewares=[database_errors])
    app.router.add_get("/health/live", health)
    app.router.add_get("/health/ready", ready)
    app.router.add_get("/openapi.json", openapi)
    app.router.add_post("/v1/tasks", create)
    app.router.add_get("/v1/tasks/{task_id}", get)
    app.router.add_get("/v1/tasks/{task_id}/report", report)
    app.router.add_get("/v1/tasks/{task_id}/artifacts/{hash}", artifact)
    app.router.add_post("/v1/tasks/{task_id}/cancel", cancel)
    app.router.add_get("/v1/tasks/{task_id}/events", events)
    app.on_startup.append(startup)
    app.on_cleanup.append(shutdown)
    return app
