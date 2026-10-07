"""Same local HTTP contracts, PostgreSQL-owned task lifecycle and worker queue."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web
from psycopg import Error as PostgreSQLError
from pydantic import TypeAdapter, ValidationError

from testpilot.artifacts import ArtifactUnavailableError
from testpilot.console import register as register_console
from testpilot.domain import Target
from testpilot.durable_runtime import DurableTaskRunner, WorkerPool
from testpilot.event_stream import stream
from testpilot.execution import FixtureMode
from testpilot.governance import ApprovalDecision, ApprovalDeniedError
from testpilot.history import MemoryCandidateRequest, MemoryDecision, RerunRequest
from testpilot.identity import AuthenticationError, IdentityProvider, StaticIdentity, require_role
from testpilot.knowledge import MCPKnowledgeClient
from testpilot.memory import MemoryRepository, verify_source
from testpilot.openapi import document
from testpilot.scheduler import JobScheduler
from testpilot.storage.postgres import (
    BudgetExhaustedError,
    CapacityReachedError,
    Ledger,
    RequestConflictError,
    TaskRow,
)
from testpilot.storage_contracts import ArtifactFactory, local_artifacts
from testpilot.task_api import Principal, TaskRequest


def create_app(
    *, tokens: dict[str, Principal], ledger: Ledger, root: Path, image: str,
    target: Callable[[FixtureMode], Target], run: DurableTaskRunner,
    workers: int = 2, lease_seconds: int = 30, poll_delay: float = 5,
    stream_interval: float = 0.5, heartbeat_seconds: float = 20,
    start_scheduler: bool = True,
    artifacts: ArtifactFactory = local_artifacts,
    identity: IdentityProvider | None = None,
    require_approval: bool = False,
    knowledge: MCPKnowledgeClient | None = None,
    model_limit: int = 12,
) -> web.Application:
    if identity is None and (not tokens or any(len(token) < 32 or owner.tenant_id != ledger.tenant_id for token, owner in tokens.items())):
        raise ValueError("strong local identity tokens matching DB scope required")
    provider = identity or StaticIdentity(tokens)
    root = root.resolve()
    pool = WorkerPool(ledger, root, image, run, workers, lease_seconds, poll_delay, artifacts, knowledge)
    scheduler = JobScheduler(ledger, root, image, lease_seconds, poll_delay, artifacts)

    async def auth(request: web.Request) -> Principal:
        try:
            principal = await provider.authenticate(request.headers.get("Authorization", ""))
            if principal.tenant_id != ledger.tenant_id or "gateway-fixture" not in principal.projects:
                raise AuthenticationError("Scope denied")
            if not set(principal.roles).intersection({"executor", "reviewer", "viewer", "admin"}):
                raise AuthenticationError("Role denied")
            return principal
        except AuthenticationError:
            raise web.HTTPUnauthorized(text="Authentication required") from None

    def permission(owner: Principal, role: str) -> None:
        try:
            require_role(owner, role)
        except PermissionError:
            raise web.HTTPForbidden(text="Role/project permission required") from None

    async def get_task(request: web.Request) -> TaskRow:
        owner = await auth(request)
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
        except ArtifactUnavailableError:
            raise web.HTTPServiceUnavailable(text="Object store unavailable") from None

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "deployment": "postgres-local-development"})

    async def ready(_request: web.Request) -> web.Response:
        await ledger.ready()
        return web.json_response({"status": "ready"})

    async def openapi(request: web.Request) -> web.Response:
        await auth(request)
        return web.json_response(document(durable=True))

    async def create(request: web.Request) -> web.Response:
        owner = await auth(request)
        permission(owner, "executor")
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
            raise web.HTTPUnprocessableEntity(text="Idempotency-Key required")
        try:
            spec = TaskRequest.model_validate_json(await request.read())
        except ValidationError:
            raise web.HTTPUnprocessableEntity(text="Invalid task contract") from None
        try:
            task, created = await ledger.admit(owner.user_id, key, spec.mode, target(spec.mode), goal=spec.goal,
                                               approval_required=require_approval or spec.require_approval,
                                               knowledge_required=spec.require_knowledge, model_limit=model_limit)
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
        store = artifacts(root, ledger.tenant_id, task.id)
        try:
            refs = TypeAdapter(list[str]).validate_python(task.report.get("artifact_refs", []), strict=True)
            for content_hash in refs:
                await asyncio.to_thread(store.read, content_hash)
        except (ValueError, OSError):
            raise web.HTTPConflict(text="Report evidence unavailable or corrupted; review required") from None
        return web.json_response(task.report)

    async def artifact(request: web.Request) -> web.Response:
        task = await get_task(request)
        selected_report = await ledger.run_report(task, request.query["run_id"]) if "run_id" in request.query else task.report
        refs = selected_report.get("artifact_refs") if selected_report else None
        content_hash = request.match_info["hash"]
        if not isinstance(refs, list) or content_hash not in refs:
            raise web.HTTPNotFound(text="Artifact not found")
        try:
            content = await asyncio.to_thread(artifacts(root, ledger.tenant_id, task.id).read, content_hash)
        except (ValueError, OSError):
            raise web.HTTPConflict(text="Artifact integrity check failed") from None
        return web.Response(body=content, content_type="application/octet-stream", headers={"X-Content-Type-Options": "nosniff"})

    async def cancel(request: web.Request) -> web.Response:
        owner = await auth(request)
        permission(owner, "executor")
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

    async def event_stream(request: web.Request) -> web.StreamResponse:
        return await stream(request, ledger, get_task, interval=stream_interval, heartbeat_seconds=heartbeat_seconds)

    async def list_tasks(request: web.Request) -> web.Response:
        owner = await auth(request)
        review = request.query.get("review", "false") == "true"
        if review:
            permission(owner, "reviewer")
        return web.json_response({"tasks": [task.snapshot() for task in await ledger.list_tasks(owner.user_id, review)]})

    async def approval(request: web.Request) -> web.Response:
        owner = await auth(request)
        item = await ledger.approval(request.match_info["task_id"])
        if item is None:
            raise web.HTTPNotFound(text="Approval not found")
        if item["requester_id"] != owner.user_id:
            permission(owner, "reviewer")
        item["expires_at"] = item["expires_at"].isoformat()
        item["created_at"] = item["created_at"].isoformat()
        return web.json_response(item)

    async def decide(request: web.Request) -> web.Response:
        owner = await auth(request)
        permission(owner, "reviewer")
        try:
            decision = ApprovalDecision.model_validate_json(await request.read())
            await ledger.decide(request.match_info["task_id"], owner.user_id, decision.request_hash, decision.decision == "APPROVE")
        except ValidationError:
            raise web.HTTPUnprocessableEntity(text="Invalid approval contract") from None
        except ApprovalDeniedError:
            raise web.HTTPForbidden(text="Independent reviewer required") from None
        except RequestConflictError:
            raise web.HTTPConflict(text="Approval version/hash/state conflict") from None
        return web.json_response({"decision": decision.decision})

    async def plan(request: web.Request) -> web.Response:
        task = await get_task(request)
        item = await ledger.latest_plan(task)
        if item is None:
            raise web.HTTPNotFound(text="Plan not yet proposed")
        return web.json_response(item)

    async def runs(request: web.Request) -> web.Response:
        task = await get_task(request)
        rows = await ledger.runs(task)
        for row in rows:
            row["active"] = row["run_id"] == task.run_id
            row["finished_at"] = row["finished_at"].isoformat() if row["finished_at"] else None
            candidate = row.pop("report")
            row["report_ready"] = candidate is not None
            row["quality_verdict"] = candidate.get("quality_verdict") if candidate else None
            row["state"] = row["state"] or (task.state if row["active"] else "NEEDS_REVIEW")
        return web.json_response({"runs": rows})

    async def historical_report(request: web.Request) -> web.Response:
        task = await get_task(request)
        body = await ledger.run_report(task, request.match_info["run_id"])
        if body is None:
            raise web.HTTPNotFound(text="Run report not found")
        try:
            store = artifacts(root, ledger.tenant_id, task.id)
            for ref in TypeAdapter(list[str]).validate_python(body.get("artifact_refs", []), strict=True):
                await asyncio.to_thread(store.read, ref)
        except (ValueError, OSError):
            raise web.HTTPConflict(text="Run evidence unavailable") from None
        return web.json_response(body)

    async def rerun(request: web.Request) -> web.Response:
        task = await get_task(request)
        owner = await auth(request)
        permission(owner, "executor")
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
            raise web.HTTPUnprocessableEntity(text="Idempotency-Key required")
        try:
            spec = RerunRequest.model_validate_json(await request.read())
            fresh, run_id, created = await ledger.rerun(task.id, owner.user_id, key, spec.expected_state_version, spec.reason)
        except ValidationError:
            raise web.HTTPUnprocessableEntity(text="Invalid rerun contract") from None
        except (RequestConflictError, BudgetExhaustedError):
            raise web.HTTPConflict(text="Rerun state/version/budget or unresolved operation conflict") from None
        except CapacityReachedError:
            raise web.HTTPTooManyRequests(text="Task capacity reached") from None
        return web.json_response({**fresh.snapshot(), "requested_run_id": run_id}, status=202 if created else 200)

    memory_repository = MemoryRepository(ledger)

    async def memory_candidate(request: web.Request) -> web.Response:
        task = await get_task(request)
        permission(await auth(request), "executor")
        try:
            spec = MemoryCandidateRequest.model_validate_json(await request.read())
            item = await memory_repository.propose(task, spec)
            await verify_source(ledger, root, artifacts, item)
        except ValidationError:
            raise web.HTTPUnprocessableEntity(text="Invalid memory source contract") from None
        except (RequestConflictError, ValueError, OSError):
            raise web.HTTPConflict(text="Verified failure evidence required") from None
        return web.json_response(item.model_dump(mode="json"), status=201)

    async def memory_list(request: web.Request) -> web.Response:
        owner = await auth(request)
        review = request.query.get("review", "false") == "true"
        if review:
            permission(owner, "reviewer")
        return web.json_response({"records": [item.model_dump(mode="json") for item in await memory_repository.list_entries(owner.user_id, review)]})

    async def memory_decide(request: web.Request) -> web.Response:
        owner = await auth(request)
        permission(owner, "reviewer")
        try:
            spec = MemoryDecision.model_validate_json(await request.read())
            item = await memory_repository.get(request.match_info["memory_id"], owner.user_id, reviewer=True)
            if item is None:
                raise web.HTTPNotFound(text="Memory not found")
            if spec.decision == "CONFIRM":
                await verify_source(ledger, root, artifacts, item)
            fresh = await memory_repository.decide(item.id, owner.user_id, spec)
        except ValidationError:
            raise web.HTTPUnprocessableEntity(text="Invalid memory decision contract") from None
        except (RequestConflictError, ValueError, OSError):
            raise web.HTTPConflict(text="Memory version/reviewer/source/state conflict") from None
        return web.json_response(fresh.model_dump(mode="json"))

    async def memory_events(request: web.Request) -> web.Response:
        owner = await auth(request)
        reviewer = bool(set(owner.roles).intersection({"reviewer", "admin"}))
        item = await memory_repository.get(request.match_info["memory_id"], owner.user_id, reviewer)
        if item is None:
            raise web.HTTPNotFound(text="Memory not found")
        rows = await memory_repository.events(item.id)
        for row in rows:
            row["created_at"] = row["created_at"].isoformat()
        return web.json_response({"memory_id": item.id, "events": rows})

    async def startup(_app: web.Application) -> None:
        await pool.start()
        if start_scheduler:
            await scheduler.start()

    async def shutdown(_app: web.Application) -> None:
        await pool.stop()  # Suspend workers; retained jobs continue and will be reconciled.
        await scheduler.stop()

    app = web.Application(client_max_size=8192, middlewares=[database_errors])
    register_console(app)
    app.router.add_get("/v1/tasks/{task_id}/runs", runs)
    app.router.add_get("/v1/tasks/{task_id}/runs/{run_id}/report", historical_report)
    app.router.add_post("/v1/tasks/{task_id}/rerun", rerun)
    app.router.add_post("/v1/tasks/{task_id}/memory", memory_candidate)
    app.router.add_get("/v1/memory", memory_list)
    app.router.add_post("/v1/memory/{memory_id}/decision", memory_decide)
    app.router.add_get("/v1/memory/{memory_id}/events", memory_events)
    app.router.add_get("/health/live", health)
    app.router.add_get("/health/ready", ready)
    app.router.add_get("/openapi.json", openapi)
    app.router.add_post("/v1/tasks", create)
    app.router.add_get("/v1/tasks", list_tasks)
    app.router.add_get("/v1/tasks/{task_id}", get)
    app.router.add_get("/v1/tasks/{task_id}/report", report)
    app.router.add_get("/v1/tasks/{task_id}/artifacts/{hash}", artifact)
    app.router.add_post("/v1/tasks/{task_id}/cancel", cancel)
    app.router.add_get("/v1/tasks/{task_id}/events", events)
    app.router.add_get("/v1/tasks/{task_id}/events/stream", event_stream)
    app.router.add_get("/v1/tasks/{task_id}/approval", approval)
    app.router.add_post("/v1/tasks/{task_id}/approval", decide)
    app.router.add_get("/v1/tasks/{task_id}/plan", plan)
    app.on_startup.append(startup)
    app.on_cleanup.append(shutdown)
    return app
