"""Real PG control-plane contracts; isolated per-test tenants, no shared-data truncation."""

import asyncio
import os
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation

from testpilot.baseline import FORK_BASELINE_SHA
from testpilot.container_runner import container_target
from testpilot.storage.postgres import (
    BudgetExhaustedError,
    CapacityReachedError,
    LeaseLostError,
    Ledger,
    RequestConflictError,
)

DSN = os.environ.get("TESTPILOT_DATABASE_URL", "")
IMAGE = os.environ.get("TESTPILOT_RUNNER_IMAGE", "sha256:" + "a" * 64)
pytestmark = pytest.mark.skipif(not DSN, reason="Requires an explicitly configured PostgreSQL test database")


@pytest.fixture
async def ledger():
    repository = Ledger(DSN, "case_" + uuid4().hex)
    await repository.migrate()
    return repository


def target(ledger, mode="healthy"):
    return container_target(FORK_BASELINE_SHA, mode, IMAGE, ledger.tenant_id)


async def test_request_dedup_survives_repository_restart_and_conflicts(ledger):
    task, created = await ledger.admit("alice", "create-1", "healthy", target(ledger))
    assert created
    restarted = Ledger(DSN, ledger.tenant_id)
    replay, created = await restarted.admit("alice", "create-1", "healthy", target(ledger))
    assert not created and replay.id == task.id
    with pytest.raises(RequestConflictError):
        await restarted.admit("alice", "create-1", "retry-write-bug", target(ledger, "retry-write-bug"))
    other, created = await restarted.admit("bob", "create-1", "healthy", target(ledger))
    assert created and other.id != task.id
    assert await ledger.get(task.id, "bob") is None
    assert await Ledger(DSN, "other_tenant").get(task.id, "alice") is None


async def test_concurrent_admission_is_unique_and_quota_atomic(ledger):
    requests = await asyncio.gather(*[ledger.admit("alice", "one", "healthy", target(ledger), max_active=1) for _ in range(5)])
    assert len({result[0].id for result in requests}) == 1
    assert sum(created for _, created in requests) == 1
    with pytest.raises(CapacityReachedError):
        await ledger.admit("bob", "other", "healthy", target(ledger), max_active=1)


async def test_skip_locked_and_expired_epoch_fence_all_mutations(ledger):
    task, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    claims = await asyncio.gather(ledger.claim("worker-a"), ledger.claim("worker-b"))
    active = [claim for claim in claims if claim]
    assert len(active) == 1
    old = active[0]
    operation = await ledger.operation(old)
    async with ledger.connection() as connection:
        await connection.execute("UPDATE testpilot.tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s", (ledger.tenant_id, task.id))
    replacement = await ledger.claim("new-worker")
    assert replacement.lease_epoch == old.lease_epoch + 1
    for mutation in (ledger.heartbeat(old), ledger.dispatch(old, operation), ledger.finish(old),
                     ledger.checkpoint(old, {"phase": "old-worker"}), ledger.reserve_round(old)):
        with pytest.raises(LeaseLostError):
            await mutation
    assert (await ledger.operation(replacement)).id == operation.id
    await ledger.dispatch(replacement, operation)


async def test_events_outbox_and_checkpoint_are_atomic_and_monotonic(ledger):
    task, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    lease = await ledger.claim("worker")
    await ledger.checkpoint(lease, {"phase": "awaiting_tools", "iteration": 1})
    rows = await ledger.events(task)
    assert [row["event_seq"] for row in rows] == [1, 2, 3]
    assert rows[-1]["type"] == "checkpoint.saved"
    async with ledger.connection() as connection:
        cursor = await connection.execute("SELECT count(*) AS count FROM testpilot.outbox WHERE tenant_id=%s", (ledger.tenant_id,))
        assert (await cursor.fetchone())["count"] == len(rows)
    replay = await ledger.events(task, after=2)
    assert [row["event_seq"] for row in replay] == [3]


async def test_budget_survives_release_and_reclaim(ledger):
    await ledger.admit("alice", "one", "healthy", target(ledger))
    lease = await ledger.claim("worker")
    for _ in range(3):
        await ledger.reserve_round(lease)
    await ledger.release(lease)
    resumed = await ledger.claim("new-worker")
    assert resumed.model_rounds == 3
    await ledger.reserve_round(resumed)
    with pytest.raises(BudgetExhaustedError):
        await ledger.reserve_round(resumed)


async def test_cross_task_operation_and_database_run_relation_rejected(ledger):
    first, _ = await ledger.admit("alice", "first", "healthy", target(ledger))
    second, _ = await ledger.admit("alice", "second", "healthy", target(ledger))
    first = await ledger.claim("one")
    second = await ledger.claim("two")
    operation = await ledger.operation(first)
    with pytest.raises(LeaseLostError, match="scope"):
        await ledger.dispatch(second, operation)
    with pytest.raises(ForeignKeyViolation):
        async with ledger.connection() as connection:
            await connection.execute("INSERT INTO testpilot.task_events (tenant_id,project_id,task_id,run_id,event_seq,type,public_payload) VALUES (%s,'gateway-fixture',%s,%s,999,'forged','{}')", (ledger.tenant_id, second.id, first.run_id))


async def test_cancel_terminal_is_monotonic_and_queued_cancel_claimable(ledger):
    task, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    cancelled = await ledger.cancel(task.id, "alice")
    assert cancelled.state == "CANCELLING"
    lease = await ledger.claim("worker")
    assert lease.cancel_requested
    with pytest.raises(BudgetExhaustedError):
        await ledger.operation(lease)
    await ledger.finish(lease, cancelled=True)
    assert (await ledger.cancel(task.id, "alice")).state == "CANCELLED"
    assert await ledger.claim("other-worker") is None
