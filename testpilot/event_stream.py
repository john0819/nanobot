"""Durable SSE replay with repeated authorization, bounded writes, no worker coupling."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime

from aiohttp import web
from psycopg import Error as PostgreSQLError
from pydantic import Field

from testpilot.domain import Contract
from testpilot.storage.postgres import Ledger, TaskRow


class PublicEvent(Contract):
    event_seq: int = Field(ge=1)
    run_id: str
    type: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    public_payload: dict[str, object]
    created_at: datetime


def cursor(request: web.Request) -> int:
    raw = request.headers.get("Last-Event-ID", request.query.get("after", "0"))
    try:
        value = int(raw)
        if not 0 <= value <= 9223372036854775807:
            raise ValueError
    except ValueError:
        raise web.HTTPUnprocessableEntity(text="Invalid event cursor") from None
    return value


def frame(kind: str, payload: dict[str, object], sequence: int | None = None) -> bytes:
    prefix = f"id: {sequence}\n" if sequence is not None else ""
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode()) > 65536:
        raise ValueError("public event exceeds bounded frame limit")
    return f"{prefix}event: {kind}\ndata: {data}\n\n".encode()


async def stream(
    request: web.Request, ledger: Ledger, authorize: Callable[[web.Request], Awaitable[TaskRow]],
    *, interval: float = 0.5, heartbeat_seconds: float = 20,
) -> web.StreamResponse:
    task = await authorize(request)
    if set(request.query) - {"after"}:
        raise web.HTTPUnprocessableEntity(text="Only the event cursor is accepted in query parameters")
    after = cursor(request)
    first, _ = await ledger.event_window(task)
    if after > task.next_event_seq or (first is None and after < task.next_event_seq) or (first is not None and after < first-1):
        return web.json_response({"resync_required": True, "snapshot": task.snapshot(),
                                  "next_event_seq": task.next_event_seq, "available_from": first}, status=409)
    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff",
    })
    await response.prepare(request)
    heartbeat_at = asyncio.get_running_loop().time() + heartbeat_seconds

    async def send(content: bytes) -> None:
        await asyncio.wait_for(response.write(content), timeout=5)

    try:
        while True:
            try:
                task = await authorize(request)  # Revocation/ownership changes apply to existing connections.
                rows = await ledger.events(task, after)
            except web.HTTPException:
                await send(frame("access_revoked", {"reason": "AUTHORIZATION_CHANGED"}))
                break
            except PostgreSQLError:
                await send(frame("service_unavailable", {"reason": "EVENT_STORE_UNAVAILABLE"}))
                break
            for row in rows:
                event = PublicEvent.model_validate(row)
                if event.event_seq != after + 1:
                    await send(frame("resync_required", {"snapshot": task.snapshot(), "next_event_seq": task.next_event_seq}))
                    return response
                await send(frame(event.type, {
                    "task_id": task.id, "run_id": event.run_id, "event_seq": event.event_seq,
                    "type": event.type, "timestamp": event.created_at.isoformat(),
                    "public_payload": event.public_payload,
                }, event.event_seq))
                after = event.event_seq
            if task.state in {"COMPLETED", "NEEDS_REVIEW", "CANCELLED"} and after >= task.next_event_seq:
                break
            if asyncio.get_running_loop().time() >= heartbeat_at:
                await send(b": heartbeat\n\n")  # No durable event or sequence is allocated.
                heartbeat_at = asyncio.get_running_loop().time() + heartbeat_seconds
            await asyncio.sleep(interval)
    except (ConnectionError, TimeoutError):
        pass  # Client disconnect/slow reads do not cancel jobs, workers or the task.
    finally:
        try:
            await asyncio.wait_for(response.write_eof(), timeout=5)
        except (ConnectionError, TimeoutError):
            pass
    return response
