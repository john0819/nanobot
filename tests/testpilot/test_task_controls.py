"""Pause keeps external facts; resume never resets Run/budget or repeats a Job."""

import asyncio
import os
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer

from testpilot.artifacts import ArtifactStore
from testpilot.baseline import FORK_BASELINE_SHA
from testpilot.container_runner import container_target
from testpilot.demo_provider import ScriptedProvider
from testpilot.durable_api import create_app
from testpilot.nanobot_adapter import run_task
from testpilot.recoverable_runner import RetainedRunner, docker
from testpilot.scheduler import JobScheduler
from testpilot.storage.postgres import (
    BudgetExhaustedError,
    CapacityReachedError,
    LeaseLostError,
    Ledger,
    RequestConflictError,
)
from testpilot.task_api import Principal

DSN = os.environ.get("TESTPILOT_DATABASE_URL", "")
IMAGE = os.environ.get("TESTPILOT_RUNNER_IMAGE", "")
TOKEN = "p"*40
HEADERS = {"Authorization": "Bearer "+TOKEN, "Idempotency-Key": "one"}
pytestmark = pytest.mark.skipif(not DSN or not IMAGE, reason="Requires PostgreSQL and immutable Docker Runner")


@pytest.fixture
async def ledger():
    repository = Ledger(DSN, "controls_"+uuid4().hex)
    await repository.migrate()
    yield repository
    async with repository.connection() as connection:
        cursor = await connection.execute("SELECT id FROM testpilot.operations WHERE tenant_id=%s", (repository.tenant_id,))
        for row in await cursor.fetchall():
            await docker("rm", "-f", "testpilot-"+row["id"], required=False)


def target(ledger, mode="healthy"):
    return container_target(FORK_BASELINE_SHA, mode, IMAGE, ledger.tenant_id)


async def run(executor, controls):
    provider = ScriptedProvider()
    return await run_task(executor, provider, provider.get_default_model(), controls=controls)


def app(ledger, root, **kwargs):
    return create_app(tokens={TOKEN: Principal("alice", ledger.tenant_id), "o"*40: Principal("other", ledger.tenant_id)},
                      ledger=ledger, root=root, image=IMAGE, target=lambda mode: target(ledger, mode),
                      run=kwargs.pop("run", run), poll_delay=0.05, **kwargs)


async def wait_task(ledger, task_id, states):
    for _ in range(300):
        task = await ledger.get(task_id, "alice")
        if task.state in states:
            return task
        await asyncio.sleep(0.03)
    raise AssertionError("task state deadline")


async def test_pause_fences_worker_and_resume_preserves_budget_and_scope(ledger):
    task, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    lease = await ledger.claim("worker")
    operation = await ledger.operation(lease)
    await ledger.reserve_round(lease)
    current = await ledger.get(task.id, "alice")
    paused = await ledger.control(task.id, "alice", current.state_version, False)
    assert paused.state == "PAUSED" and paused.lease_owner is None
    assert await ledger.claim("agent", kind="agent") is None
    for mutation in [ledger.guard_action(lease), ledger.checkpoint(lease, {"phase": "stale"}), ledger.dispatch(lease, operation)]:
        with pytest.raises(LeaseLostError):
            await mutation
    with pytest.raises(RequestConflictError):
        await ledger.control(task.id, "alice", current.state_version, True)
    resumed = await ledger.control(task.id, "alice", paused.state_version, True)
    assert resumed.run_id == task.run_id and resumed.model_rounds == 1
    assert resumed.activated_at == current.activated_at
    new = await ledger.claim("new-agent", kind="agent")
    assert (await ledger.operation(new)).id == operation.id


async def test_inputs_dedup_capacity_private_events_and_no_scope_change(ledger):
    task, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    responses = await asyncio.gather(*[ledger.add_input(task.id, "alice", "note", "Please inspect retry semantics") for _ in range(4)])
    assert sum(created for _, created in responses) == 1
    with pytest.raises(RequestConflictError):
        await ledger.add_input(task.id, "alice", "note", "different")
    with pytest.raises(RequestConflictError):
        await ledger.add_input(task.id, "other", "new", "private")
    for index in range(7):
        await ledger.add_input(task.id, "alice", f"note-{index}", "Private source detail")
    with pytest.raises(CapacityReachedError):
        await ledger.add_input(task.id, "alice", "overflow", "text")
    lease = await ledger.claim("worker")
    await ledger.consume_inputs(lease, ["note"])
    await ledger.consume_inputs(lease, ["note"])
    rows = await ledger.events(lease)
    assert sum(row["type"] == "input.accepted" for row in rows) == 8
    assert sum(row["type"] == "input.projected" for row in rows) == 1
    assert "Private source detail" not in str(rows)
    assert (await ledger.get(task.id, "alice")).target == task.target


async def test_pause_does_not_extend_deadline_and_cancel_remains_available(ledger):
    task, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    paused = await ledger.control(task.id, "alice", (await ledger.get(task.id, "alice")).state_version, False)
    async with ledger.connection() as connection:
        await connection.execute("UPDATE testpilot.tasks SET activated_at=clock_timestamp()-interval '121 seconds' WHERE tenant_id=%s AND id=%s", (ledger.tenant_id, task.id))
    with pytest.raises(BudgetExhaustedError):
        await ledger.control(task.id, "alice", paused.state_version, True)
    assert await ledger.expire_paused() == 1
    assert (await ledger.get(task.id, "alice")).error == "TASK_DEADLINE"
    other, _ = await ledger.admit("alice", "two", "healthy", target(ledger))
    paused = await ledger.control(other.id, "alice", (await ledger.get(other.id, "alice")).state_version, False)
    assert (await ledger.cancel(other.id, "alice")).state == "CANCELLING"
    lease = await ledger.claim("scheduler", kind="external")
    await ledger.finish(lease, cancelled=True)
    assert (await ledger.get(other.id, "alice")).state == "CANCELLED"


async def test_paused_approval_still_expires_without_starting_model(ledger):
    task, _ = await ledger.admit("alice", "approval", "healthy", target(ledger), approval_required=True)
    current = await ledger.get(task.id, "alice")
    await ledger.control(task.id, "alice", current.state_version, False)
    async with ledger.connection() as connection:
        await connection.execute("UPDATE testpilot.approvals SET expires_at=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND run_id=%s", (ledger.tenant_id, task.run_id))
    assert await ledger.expire_approvals() == 1
    expired = await ledger.get(task.id, "alice")
    assert expired.state == "NEEDS_REVIEW" and expired.error == "APPROVAL_EXPIRED"
    assert expired.model_rounds == 0 and await ledger.find_operation(expired) is None


async def test_resume_refuses_changed_snapshot(ledger, tmp_path):
    task, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    current = await ledger.get(task.id, "alice")
    paused = await ledger.control(task.id, "alice", current.state_version, False)
    service = create_app(tokens={TOKEN: Principal("alice", ledger.tenant_id)}, ledger=ledger, root=tmp_path,
                         image=IMAGE, target=lambda mode: container_target(FORK_BASELINE_SHA, mode, "sha256:"+"b"*64, ledger.tenant_id),
                         run=run)
    async with TestClient(TestServer(service)) as client:
        response = await client.post(f"/v1/tasks/{task.id}/resume", headers=HEADERS, json={"expected_state_version": paused.state_version})
        assert response.status == 409
        assert (await ledger.get(task.id, "alice")).state == "PAUSED"


async def test_pause_during_model_prevents_job_and_projects_input_on_resume(ledger, tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    observed = []

    class HeldProvider(ScriptedProvider):
        async def chat(self, messages, **kwargs):
            observed.extend(message.get("content", "") for message in messages if message.get("role") == "user")
            if not started.is_set():
                started.set()
                await release.wait()
            return await super().chat(messages, **kwargs)

    provider = HeldProvider()

    async def held(executor, controls):
        return await run_task(executor, provider, provider.get_default_model(), controls=controls)

    async with TestClient(TestServer(app(ledger, tmp_path, run=held))) as client:
        response = await client.post("/v1/tasks", headers=HEADERS, json={})
        task_id = (await response.json())["task_id"]
        await asyncio.wait_for(started.wait(), 5)
        current = await ledger.get(task_id, "alice")
        response = await client.post(f"/v1/tasks/{task_id}/pause", headers=HEADERS, json={"expected_state_version": current.state_version})
        assert response.status == 202
        paused = await response.json()
        await asyncio.sleep(0.1)
        operation = await ledger.find_operation(await ledger.get(task_id, "alice"))
        assert operation.state == "PREPARED"
        assert not (await docker("container", "ls", "-aq", "--filter", "label=testpilot.operation="+operation.id)).strip()
        note = {"client_request_id": "followup", "text": "Inspect the admitted retry assertions; do not infer missing results"}
        assert (await client.post(f"/v1/tasks/{task_id}/inputs", headers=HEADERS, json=note)).status == 202
        assert (await client.post(f"/v1/tasks/{task_id}/inputs", headers=HEADERS, json=note)).status == 200
        assert (await client.post(f"/v1/tasks/{task_id}/inputs", headers=HEADERS, json={**note, "target": "evil"})).status == 422
        other_headers = {"Authorization": "Bearer "+"o"*40}
        assert (await client.get(f"/v1/tasks/{task_id}/inputs", headers=other_headers)).status == 404
        current = await ledger.get(task_id, "alice")
        release.set()
        assert (await client.post(f"/v1/tasks/{task_id}/resume", headers=HEADERS, json={"expected_state_version": current.state_version})).status == 202
        done = await wait_task(ledger, task_id, {"COMPLETED","NEEDS_REVIEW"})
        assert done.state == "COMPLETED" and done.run_id == paused["run_id"]
        assert any(note["text"] in str(content) for content in observed)
        assert (await ledger.inputs(done))[0]["consumed_at"] is not None


async def test_paused_job_finishes_and_survives_scheduler_restart_without_model(ledger, tmp_path):
    task, _ = await ledger.admit("alice", "one", "retry-write-bug", target(ledger, "retry-write-bug"), model_limit=12)
    lease = await ledger.claim("agent", kind="agent")
    operation = await ledger.operation(lease)
    backend = RetainedRunner(lease, operation, ArtifactStore(tmp_path/ledger.tenant_id/task.id/"artifacts"), IMAGE, tmp_path/"jobs"/ledger.tenant_id)
    await ledger.reserve_round(lease)
    await ledger.dispatch(lease, operation)
    backend.note_dispatch()
    pending = await backend.submit()
    await docker("pause", backend.name)
    await ledger.park(lease, operation, pending, 0)
    current = await ledger.get(task.id, "alice")
    paused = await ledger.control(task.id, "alice", current.state_version, False)
    scheduler = JobScheduler(ledger, tmp_path, IMAGE, poll_delay=0.05)
    claim = await ledger.claim("scheduler", kind="external")
    await scheduler.reconcile(claim)
    assert (await ledger.get(task.id, "alice")).state == "PAUSED"
    await docker("unpause", backend.name)
    for _ in range(150):
        claim = await ledger.claim("restarted-scheduler", kind="external")
        if claim:
            await JobScheduler(ledger, tmp_path, IMAGE, poll_delay=0.05).reconcile(claim)
        current = await ledger.get(task.id, "alice")
        if (await ledger.find_operation(current)).state == "SUCCEEDED":
            break
        await asyncio.sleep(0.03)
    assert current.state == "PAUSED" and current.model_rounds == paused.model_rounds == 1
    assert await ledger.claim("no-model", kind="agent") is None
    assert len((await docker("container", "ls", "-aq", "--filter", "label=testpilot.operation="+operation.id)).strip().splitlines()) == 1
    await ledger.control(task.id, "alice", current.state_version, True)
    async with TestClient(TestServer(app(ledger, tmp_path))):
        done = await wait_task(ledger, task.id, {"COMPLETED","NEEDS_REVIEW"})
        assert done.state == "COMPLETED" and done.report["test_summary"]["failed"] == 1
        assert (await ledger.find_operation(done)).id == operation.id
