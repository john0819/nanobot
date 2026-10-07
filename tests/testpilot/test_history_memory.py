"""Real run isolation and reviewed Memory lifecycle, with scoped PG and Docker fixtures."""

import asyncio
import os
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from psycopg.errors import RaiseException

from testpilot.baseline import FORK_BASELINE_SHA
from testpilot.container_runner import container_target
from testpilot.demo_provider import ScriptedProvider
from testpilot.durable_api import create_app
from testpilot.history import MemoryDecision
from testpilot.memory import MemoryRepository
from testpilot.nanobot_adapter import run_task
from testpilot.recoverable_runner import docker
from testpilot.storage.postgres import BudgetExhaustedError, Ledger, RequestConflictError
from testpilot.task_api import Principal

DSN = os.environ.get("TESTPILOT_DATABASE_URL", "")
IMAGE = os.environ.get("TESTPILOT_RUNNER_IMAGE", "")
pytestmark = pytest.mark.skipif(not DSN or not IMAGE, reason="Requires PostgreSQL and immutable Runner")
ALICE, BOB, CHARLIE = "a"*40, "b"*40, "c"*40


@pytest.fixture
async def ledger():
    repository = Ledger(DSN, "history_"+uuid4().hex)
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


def app(ledger, tmp_path, **kwargs):
    return create_app(tokens={ALICE: Principal("alice", ledger.tenant_id),
                              BOB: Principal("bob", ledger.tenant_id, roles=("reviewer",)),
                              CHARLIE: Principal("charlie", ledger.tenant_id)},
                      ledger=ledger, root=tmp_path, image=IMAGE, target=lambda mode: target(ledger, mode),
                      run=run, workers=1, poll_delay=0.05, **kwargs)


def headers(token=ALICE, key=""):
    return {"Authorization": "Bearer "+token, "Idempotency-Key": key}


async def terminal(ledger, task_id):
    for _ in range(300):
        task = await ledger.get(task_id, "alice")
        if task.state in {"COMPLETED", "NEEDS_REVIEW", "CANCELLED"}:
            return task
        await asyncio.sleep(0.03)
    raise AssertionError("Task deadline")


async def test_explicit_rerun_preserves_failure_and_shares_budget(ledger, tmp_path):
    async with TestClient(TestServer(app(ledger, tmp_path))) as client:
        response = await client.post("/v1/tasks", headers=headers(key="create"), json={"mode": "retry-write-bug"})
        task_id = (await response.json())["task_id"]
        first = await terminal(ledger, task_id)
        first_report = first.report
        assert first_report["test_summary"]["failed"] == 1
        body = {"expected_state_version": first.state_version, "reason": "Explicit repeatability verification"}
        requests = await asyncio.gather(*[client.post(f"/v1/tasks/{task_id}/rerun", headers=headers(key="rerun"), json=body) for _ in range(3)])
        assert sorted(response.status for response in requests) == [200, 200, 202]
        runs = [(await response.json())["requested_run_id"] for response in requests]
        assert len(set(runs)) == 1 and runs[0] != first.run_id
        assert (await client.post(f"/v1/tasks/{task_id}/rerun", headers=headers(key="rerun"), json={**body, "reason": "changed"})).status == 409
        second = await terminal(ledger, task_id)
        assert second.run_id == runs[0] and second.model_rounds > first.model_rounds
        assert second.activated_at == first.activated_at and second.model_limit == first.model_limit
        response = await client.get(f"/v1/tasks/{task_id}/runs/{first.run_id}/report", headers=headers())
        assert response.status == 200 and await response.json() == first_report
        response = await client.get(f"/v1/tasks/{task_id}/runs", headers=headers())
        history = (await response.json())["runs"]
        assert [row["sequence"] for row in history] == [1, 2]
        assert [row["active"] for row in history] == [False, True]
        assert second.report["test_summary"]["planned"] == 4  # Never 8 by merging both runs.
        old_hash = first_report["artifact_refs"][0]
        assert (await client.get(f"/v1/tasks/{task_id}/artifacts/{old_hash}?run_id={first.run_id}", headers=headers())).status == 200
        assert (await client.get(f"/v1/tasks/{task_id}/runs", headers=headers(CHARLIE))).status == 404
        async with ledger.connection() as connection:
            cursor = await connection.execute("SELECT count(*) AS count FROM testpilot.operations WHERE tenant_id=%s AND task_id=%s", (ledger.tenant_id, task_id))
            assert (await cursor.fetchone())["count"] == 2
        with pytest.raises(RaiseException):
            async with ledger.connection() as connection:
                await connection.execute("UPDATE testpilot.run_results SET report='{}'::jsonb WHERE tenant_id=%s AND run_id=%s", (ledger.tenant_id, first.run_id))


async def test_rerun_requires_fresh_approval_and_preserves_deadline(ledger):
    task, _ = await ledger.admit("alice", "create", "healthy", target(ledger), approval_required=True, model_limit=12)
    approval = await ledger.approval(task.id)
    await ledger.decide(task.id, "bob", approval["request_hash"], True)
    lease = await ledger.claim("worker")
    await ledger.finish(lease, error="safe partial")
    old = await ledger.get(task.id, "alice")
    fresh, run_id, _ = await ledger.rerun(task.id, "alice", "rerun", old.state_version, "revalidate")
    assert fresh.state == "WAITING_APPROVAL" and run_id != old.run_id
    assert await ledger.claim("worker") is None
    assert (await ledger.approval(task.id))["request_hash"] != approval["request_hash"]
    with pytest.raises(RequestConflictError):
        await ledger.decide(task.id, "bob", approval["request_hash"], True)
    await ledger.decide(task.id, "bob", (await ledger.approval(task.id))["request_hash"], True)
    assert (await ledger.get(task.id, "alice")).activated_at == old.activated_at


async def test_rerun_refuses_budget_or_unknown_operation(ledger):
    task, _ = await ledger.admit("alice", "budget", "healthy", target(ledger))
    lease = await ledger.claim("worker")
    for _ in range(4):
        await ledger.reserve_round(lease)
    await ledger.finish(lease, error="TASK_BUDGET")
    finished = await ledger.get(task.id, "alice")
    with pytest.raises(BudgetExhaustedError):
        await ledger.rerun(task.id, "alice", "rerun", finished.state_version, "repeat")
    task, _ = await ledger.admit("alice", "unknown", "healthy", target(ledger))
    lease = await ledger.claim("worker")
    operation = await ledger.operation(lease)
    await ledger.dispatch(lease, operation)
    await ledger.outcome(lease, operation, None)
    await ledger.finish(lease, error="OPERATION_UNKNOWN")
    finished = await ledger.get(task.id, "alice")
    with pytest.raises(RequestConflictError):
        await ledger.rerun(task.id, "alice", "rerun", finished.state_version, "repeat")


async def test_memory_review_ttl_scope_revoke_and_source_integrity(ledger, tmp_path):
    repository = MemoryRepository(ledger)
    async with TestClient(TestServer(app(ledger, tmp_path))) as client:
        response = await client.post("/v1/tasks", headers=headers(key="create"), json={"mode": "retry-write-bug"})
        task_id = (await response.json())["task_id"]
        source = await terminal(ledger, task_id)
        case = source.report["findings"][0]["case_id"]
        payload = {"source_run_id": source.run_id, "case_id": case, "shared": True}
        response = await client.post(f"/v1/tasks/{task_id}/memory", headers=headers(), json=payload)
        assert response.status == 201
        candidate = await response.json()
        memory_id = candidate["id"]
        consumer = source.model_copy(update={"actor_id": "charlie"})
        assert await repository.retrieve(consumer) == []
        with pytest.raises(RequestConflictError):
            await repository.decide(memory_id, "alice", MemoryDecision(expected_version=1, decision="CONFIRM"))
        assert (await client.post(f"/v1/memory/{memory_id}/decision", headers=headers(), json={"expected_version": 1, "decision": "CONFIRM"})).status == 403
        response = await client.post(f"/v1/memory/{memory_id}/decision", headers=headers(BOB), json={"expected_version": 1, "decision": "CONFIRM"})
        assert response.status == 200 and (await response.json())["version"] == 2
        entries = await repository.retrieve(consumer)
        assert entries[0]["current_execution_evidence"] is False
        assert await MemoryRepository(Ledger(DSN, "other_tenant")).retrieve(consumer) == []
        assert await repository.retrieve(consumer.model_copy(update={"target": target(ledger, "healthy")})) == []
        response = await client.post(f"/v1/memory/{memory_id}/decision", headers=headers(BOB), json={"expected_version": 1, "decision": "REVOKE"})
        assert response.status == 409
        response = await client.post(f"/v1/memory/{memory_id}/decision", headers=headers(BOB), json={"expected_version": 2, "decision": "REVOKE"})
        assert response.status == 200 and await repository.retrieve(consumer) == []
        async with ledger.connection() as connection:
            cursor = await connection.execute("SELECT status FROM testpilot.memory_events WHERE tenant_id=%s AND memory_id=%s ORDER BY version", (ledger.tenant_id, memory_id))
            assert [row["status"] for row in await cursor.fetchall()] == ["CANDIDATE", "CONFIRMED", "REVOKED"]
        # A different source run demonstrates TTL independently of revocation.
        fresh, _, _ = await ledger.rerun(source.id, "alice", "again", source.state_version, "TTL source")
        second = await terminal(ledger, fresh.id)
        response = await client.post(f"/v1/tasks/{task_id}/memory", headers=headers(), json={**payload, "source_run_id": second.run_id, "shared": False})
        assert response.status == 201
        private = await response.json()
        await repository.decide(private["id"], "bob", MemoryDecision(expected_version=1, decision="CONFIRM"))
        assert await repository.retrieve(consumer) == []  # Private memory never crosses users.
        assert len(await repository.retrieve(second)) == 1
        async with ledger.connection() as connection:
            await connection.execute("UPDATE testpilot.memory_records SET expires_at=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s", (ledger.tenant_id, private["id"]))
        assert await repository.retrieve(second) == []
        assert (await repository.get(private["id"], "alice")).status == "EXPIRED"
        assert await repository.expire() in {0, 1}  # Separate Scheduler may already have swept it.
        assert await repository.expire() == 0
        assert (await repository.events(private["id"]))[-1]["status"] == "EXPIRED"
        assert (await client.post(f"/v1/tasks/{task_id}/memory", headers=headers(), json={**payload, "case_id": "invented"})).status == 409
        assert (await client.post(f"/v1/tasks/{task_id}/memory", headers=headers(), json={**payload, "value": "password=secret"})).status == 422
        # Evidence may disappear after the candidate was registered: confirmation fails closed.
        response = await client.post("/v1/tasks", headers=headers(key="corrupt-source"), json={"mode": "retry-write-bug"})
        other_id = (await response.json())["task_id"]
        other = await terminal(ledger, other_id)
        response = await client.post(f"/v1/tasks/{other_id}/memory", headers=headers(), json={**payload, "source_run_id": other.run_id})
        other_memory = await response.json()
        junit_hash = other.report["artifact_refs"][0]
        (tmp_path/ledger.tenant_id/other_id/"artifacts"/junit_hash).write_bytes(b"tampered")
        response = await client.post(f"/v1/memory/{other_memory['id']}/decision", headers=headers(BOB), json={"expected_version": 1, "decision": "CONFIRM"})
        assert response.status == 409
        assert (await repository.get(other_memory["id"], "alice")).status == "CANDIDATE"
