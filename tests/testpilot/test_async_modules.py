"""Real PG/Docker/AgentRunner waiting, scheduler, cancellation and SSE contracts."""

import asyncio
import json
import os
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanobot.providers.base import LLMResponse, ToolCallRequest
from testpilot.artifacts import ArtifactStore
from testpilot.baseline import FORK_BASELINE_SHA
from testpilot.container_runner import container_target
from testpilot.demo_provider import ScriptedProvider
from testpilot.domain import PendingExecution
from testpilot.durable_api import create_app
from testpilot.nanobot_adapter import run_task
from testpilot.progress import ProgressStalledError
from testpilot.recoverable_runner import RetainedRunner, docker
from testpilot.storage.postgres import Ledger
from testpilot.task_api import Principal

DSN = os.environ.get("TESTPILOT_DATABASE_URL", "")
IMAGE = os.environ.get("TESTPILOT_RUNNER_IMAGE", "")
TOKEN = "s" * 40
HEADERS = {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "one"}
pytestmark = pytest.mark.skipif(not DSN or not IMAGE, reason="Requires PG and a real Docker Runner image")
ACTIVE = {"QUEUED", "RUNNING", "WAITING_EXTERNAL", "RECONCILING", "CANCELLING"}


@pytest.fixture
async def ledger():
    repository = Ledger(DSN, "async_" + uuid4().hex)
    await repository.migrate()
    yield repository
    async with repository.connection() as connection:
        cursor = await connection.execute("SELECT id FROM testpilot.operations WHERE tenant_id=%s", (repository.tenant_id,))
        for row in await cursor.fetchall():
            await docker("rm", "-f", "testpilot-" + row["id"], required=False)


def target(ledger, mode="healthy"):
    return container_target(FORK_BASELINE_SHA, mode, IMAGE, ledger.tenant_id)


async def run(execution, controls):
    provider = ScriptedProvider()
    return await run_task(execution, provider, provider.get_default_model(), controls=controls)


def app(ledger, tmp_path, **kwargs):
    return create_app(tokens={TOKEN: Principal("alice", ledger.tenant_id)}, ledger=ledger,
                      root=tmp_path, image=IMAGE, target=lambda mode: target(ledger, mode),
                      run=kwargs.pop("run", run), workers=1, poll_delay=0.05, **kwargs)


async def await_state(ledger, task_id, states):
    for _ in range(200):
        task = await ledger.get(task_id, "alice")
        if task.state in states:
            return task
        await asyncio.sleep(0.03)
    raise AssertionError("task state deadline")


async def pause_bug(self):
    await ORIGINAL_SUBMIT(self)
    if self.task.mode == "retry-write-bug":
        await docker("pause", self.name)
    return await self.poll()


ORIGINAL_SUBMIT = RetainedRunner.submit


async def test_waiting_job_releases_only_agent_worker_and_saves_paired_checkpoint(ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(RetainedRunner, "submit", pause_bug)
    async with TestClient(TestServer(app(ledger, tmp_path))) as client:
        result = await client.post("/v1/tasks", json={"mode": "retry-write-bug"}, headers=HEADERS)
        first = (await result.json())["task_id"]
        waiting = await await_state(ledger, first, {"WAITING_EXTERNAL"})
        assert waiting.lease_owner is None and waiting.lease_until is None
        assert waiting.model_rounds == 1  # No model polling while external state is unchanged.
        operation = await ledger.find_operation(waiting)
        async with ledger.connection() as connection:
            cursor = await connection.execute("SELECT body FROM testpilot.checkpoints WHERE tenant_id=%s AND task_id=%s ORDER BY event_seq", (ledger.tenant_id, first))
            bodies = [row["body"] for row in await cursor.fetchall()]
        complete = next(body for body in bodies if body.get("phase") == "tools_completed")
        assert complete["pending_tool_calls"] == []
        assert complete["completed_tool_results"][0]["tool_call_id"] == complete["assistant_message"]["tool_calls"][0]["id"]
        assert json.loads(complete["completed_tool_results"][0]["content"])["status"] == "PENDING"
        second_headers = {**HEADERS, "Idempotency-Key": "second"}
        response = await client.post("/v1/tasks", json={"mode": "healthy"}, headers=second_headers)
        second = (await response.json())["task_id"]
        assert (await await_state(ledger, second, {"COMPLETED", "NEEDS_REVIEW"})).state == "COMPLETED"
        still_waiting = await ledger.get(first, "alice")
        assert still_waiting.state in {"WAITING_EXTERNAL", "RECONCILING"} and still_waiting.model_rounds == 1
        await docker("unpause", "testpilot-" + operation.id)
        done = await await_state(ledger, first, {"COMPLETED", "NEEDS_REVIEW"})
        assert done.state == "COMPLETED" and done.report["quality_verdict"] == "FAIL"
        jobs = await docker("container", "ls", "-aq", "--filter", "label=testpilot.operation=" + operation.id)
        assert len(jobs.strip().splitlines()) == 1


async def test_cancel_waiting_job_is_cleaned_by_scheduler_without_model_calls(ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(RetainedRunner, "submit", pause_bug)
    async with TestClient(TestServer(app(ledger, tmp_path))) as client:
        result = await client.post("/v1/tasks", json={"mode": "retry-write-bug"}, headers=HEADERS)
        task_id = (await result.json())["task_id"]
        await await_state(ledger, task_id, {"WAITING_EXTERNAL"})
        response = await client.post(f"/v1/tasks/{task_id}/cancel", headers=HEADERS)
        assert response.status == 202
        done = await await_state(ledger, task_id, {"CANCELLED", "NEEDS_REVIEW"})
        assert done.state == "CANCELLED" and done.model_rounds == 1
        operation = await ledger.find_operation(done)
        job = await RetainedRunner(done, operation, ArtifactStore(tmp_path / "store"), IMAGE, tmp_path / "jobs" / ledger.tenant_id).inspect()
        assert job.State.Status in {"exited", "dead"}


async def test_progress_state_survives_owner_change_and_emits_stalled_event(ledger):
    await ledger.admit("alice", "one", "healthy", target(ledger))
    old = await ledger.claim("first")
    fingerprint = "a" * 64
    await ledger.progress(old, [fingerprint, fingerprint])
    await ledger.release(old)
    new = await ledger.claim("second")
    with pytest.raises(ProgressStalledError):
        await ledger.progress(new, [fingerprint])
    assert (await ledger.get(old.id, "alice")).progress_json["stalled"] is True
    assert "progress.stalled" in {row["type"] for row in await ledger.events(new)}


async def test_real_agent_loop_stalls_but_keeps_execution_evidence(ledger, tmp_path):
    class ForeverProvider(ScriptedProvider):
        async def chat(self, *args, **kwargs):
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="repeat-" + uuid4().hex, name="run_gateway_fixture", arguments={})])

    async def repeat(execution, controls):
        provider = ForeverProvider()
        return await run_task(execution, provider, provider.get_default_model(), controls=controls)

    async with TestClient(TestServer(app(ledger, tmp_path, run=repeat))) as client:
        result = await client.post("/v1/tasks", json={}, headers=HEADERS)
        task_id = (await result.json())["task_id"]
        done = await await_state(ledger, task_id, {"NEEDS_REVIEW"})
        assert done.error == "PROGRESS_STALLED"
        assert done.report["test_summary"]["passed"] == 4
        assert done.model_rounds <= 4
        operation = await ledger.find_operation(done)
        assert len((await docker("container", "ls", "-aq", "--filter", "label=testpilot.operation=" + operation.id)).strip().splitlines()) == 1


async def read_frame(response):
    lines = []
    async with asyncio.timeout(3):
        while True:
            line = await response.content.readline()
            if not line:
                return lines
            text = line.decode().strip()
            if not text:
                return lines
            lines.append(text)


async def test_sse_replays_ids_disconnect_does_not_cancel_and_revocation_applies(ledger, tmp_path):
    created, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    lease = await ledger.claim("manual")
    await ledger.checkpoint(lease, {"phase": "tools_completed", "private_prompt": "NEVER_EXPOSE_THIS"})
    tokens = {TOKEN: Principal("alice", ledger.tenant_id)}
    service = create_app(tokens=tokens, ledger=ledger, root=tmp_path, image=IMAGE,
                         target=lambda mode: target(ledger, mode), run=run,
                         stream_interval=0.02, heartbeat_seconds=0.05)
    service.on_startup.clear()  # Keep the task running so transport behavior is tested independently.
    async with TestClient(TestServer(service)) as client:
        path = f"/v1/tasks/{created.id}/events/stream"
        response = await client.get(path, headers=HEADERS)
        assert response.status == 200
        first = await read_frame(response)
        assert "id: 1" in first
        response.close()
        assert (await ledger.get(created.id, "alice")).state == "RUNNING"
        replay = await client.get(path, headers={**HEADERS, "Last-Event-ID": "1"})
        second, third = await read_frame(replay), await read_frame(replay)
        assert "id: 2" in second and "id: 3" in third
        assert "NEVER_EXPOSE_THIS" not in str(third)
        tokens.clear()
        frames = []
        for _ in range(3):
            frames += await read_frame(replay)
            if any("access_revoked" in line for line in frames):
                break
        assert any("access_revoked" in line for line in frames)
        replay.close()


async def test_sse_future_cursor_resync_and_cross_principal_denied(ledger, tmp_path):
    created, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    service = app(ledger, tmp_path)
    service.on_startup.clear()
    async with TestClient(TestServer(service)) as client:
        path = f"/v1/tasks/{created.id}/events/stream"
        assert (await client.get(path)).status == 401
        response = await client.get(path, headers={**HEADERS, "Last-Event-ID": "999"})
        assert response.status == 409 and (await response.json())["resync_required"]
        assert (await client.get(path + "?after=-1", headers=HEADERS)).status == 422
        assert (await client.get(path + "?token=secret", headers=HEADERS)).status == 422
        foreign, _ = await ledger.admit("bob", "two", "healthy", target(ledger))
        assert (await client.get(f"/v1/tasks/{foreign.id}/events/stream", headers=HEADERS)).status == 404


async def test_sse_heartbeat_does_not_allocate_sequence_and_old_window_requires_resync(ledger, tmp_path):
    created, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    service = app(ledger, tmp_path, stream_interval=0.01, heartbeat_seconds=0.02)
    service.on_startup.clear()
    async with TestClient(TestServer(service)) as client:
        path = f"/v1/tasks/{created.id}/events/stream"
        response = await client.get(path, headers=HEADERS)
        assert "id: 1" in await read_frame(response)
        assert ": heartbeat" in await read_frame(response)
        assert (await ledger.get(created.id, "alice")).next_event_seq == 1
        response.close()
        lease = await ledger.claim("manual")
        await ledger.checkpoint(lease, {"phase": "tools_completed"})
        async with ledger.connection() as connection:
            await connection.execute("DELETE FROM testpilot.outbox WHERE tenant_id=%s AND task_id=%s AND event_seq<3", (ledger.tenant_id, created.id))
            await connection.execute("DELETE FROM testpilot.task_events WHERE tenant_id=%s AND task_id=%s AND event_seq<3", (ledger.tenant_id, created.id))
        old = await client.get(path, headers={**HEADERS, "Last-Event-ID": "1"})
        assert old.status == 409 and (await old.json())["available_from"] == 3


async def test_waiting_job_survives_api_restart_with_same_external_id(ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(RetainedRunner, "submit", pause_bug)
    async with TestClient(TestServer(app(ledger, tmp_path, start_scheduler=False))) as client:
        response = await client.post("/v1/tasks", json={"mode": "retry-write-bug"}, headers=HEADERS)
        task_id = (await response.json())["task_id"]
        waiting = await await_state(ledger, task_id, {"WAITING_EXTERNAL"})
        operation = await ledger.find_operation(waiting)
        job_id = operation.job_external_id
    async with TestClient(TestServer(app(ledger, tmp_path))) as restarted:
        response = await restarted.get(f"/v1/tasks/{task_id}", headers=HEADERS)
        assert (await response.json())["state"] in {"WAITING_EXTERNAL", "RECONCILING"}
        await docker("unpause", "testpilot-" + operation.id)
        done = await await_state(ledger, task_id, {"COMPLETED", "NEEDS_REVIEW"})
        assert done.state == "COMPLETED" and done.report["external_run_id"] == job_id
        assert len((await docker("container", "ls", "-aq", "--filter", "label=testpilot.operation=" + operation.id)).strip().splitlines()) == 1


async def test_scheduler_deadline_stops_job_without_spending_model_budget(ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(RetainedRunner, "submit", pause_bug)
    async with TestClient(TestServer(app(ledger, tmp_path))) as client:
        response = await client.post("/v1/tasks", json={"mode": "retry-write-bug"}, headers=HEADERS)
        task_id = (await response.json())["task_id"]
        waiting = await await_state(ledger, task_id, {"WAITING_EXTERNAL"})
        async with ledger.connection() as connection:
            await connection.execute("UPDATE testpilot.external_jobs SET deadline_at=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND task_id=%s", (ledger.tenant_id, task_id))
        done = await await_state(ledger, task_id, {"NEEDS_REVIEW"})
        assert done.error == "JOB_DEADLINE" and done.model_rounds == 1
        operation = await ledger.find_operation(waiting)
        backend = RetainedRunner(done, operation, ArtifactStore(tmp_path / "store"), IMAGE, tmp_path / "jobs" / ledger.tenant_id)
        assert (await backend.inspect()).State.Status in {"exited", "dead"}


async def test_poll_backoff_does_not_push_wakeup_beyond_original_job_deadline(ledger):
    await ledger.admit("alice", "one", "healthy", target(ledger))
    lease = await ledger.claim("worker")
    operation = await ledger.operation(lease)
    await ledger.dispatch(lease, operation)
    async with ledger.connection() as connection:
        await connection.execute("UPDATE testpilot.operations SET dispatched_at=clock_timestamp()-interval '59 seconds' WHERE tenant_id=%s AND id=%s", (ledger.tenant_id, operation.id))
    await ledger.park(lease, operation, PendingExecution(operation_id=operation.id, external_run_id="deadline-fixture"), delay=30)
    async with ledger.connection() as connection:
        cursor = await connection.execute("SELECT t.next_wakeup_at<=j.deadline_at AS bounded,j.next_check_at<=j.deadline_at AS check_bounded FROM testpilot.tasks t JOIN testpilot.external_jobs j ON (t.tenant_id,t.id)=(j.tenant_id,j.task_id) WHERE t.tenant_id=%s AND t.id=%s", (ledger.tenant_id, lease.id))
        row = await cursor.fetchone()
        assert row["bounded"] and row["check_bounded"]
