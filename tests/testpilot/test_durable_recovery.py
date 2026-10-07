"""True PG + Docker + nanobot recovery; count Jobs at the external Docker server."""

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
from testpilot.durable_runtime import DurableExecutor
from testpilot.nanobot_adapter import run_task
from testpilot.recoverable_runner import RetainedRunner, UnknownDispatchError, docker
from testpilot.runtime_contracts import RunControls
from testpilot.storage.postgres import LeaseLostError, Ledger
from testpilot.task_api import Principal

DSN = os.environ.get("TESTPILOT_DATABASE_URL", "")
IMAGE = os.environ.get("TESTPILOT_RUNNER_IMAGE", "")
TOKEN = "c" * 40
HEADERS = {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "one"}
pytestmark = pytest.mark.skipif(not DSN or not IMAGE, reason="Requires actual PostgreSQL and isolated Runner image")


@pytest.fixture
async def ledger():
    repository = Ledger(DSN, "recovery_" + uuid4().hex)
    await repository.migrate()
    yield repository
    # Only remove this test tenant's explicitly recorded fixed Jobs; never prune shared Docker.
    async with repository.connection() as connection:
        cursor = await connection.execute("SELECT id FROM testpilot.operations WHERE tenant_id=%s", (repository.tenant_id,))
        for row in await cursor.fetchall():
            await docker("rm", "-f", "testpilot-" + row["id"], required=False)


def target(ledger, mode="healthy"):
    return container_target(FORK_BASELINE_SHA, mode, IMAGE, ledger.tenant_id)


async def expire(ledger, task):
    async with ledger.connection() as connection:
        await connection.execute("UPDATE testpilot.tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s", (ledger.tenant_id, task.id))


async def run(execution, controls):
    provider = ScriptedProvider()
    return await run_task(execution, provider, provider.get_default_model(), controls=controls)


async def wait(client, task_id):
    for _ in range(150):
        response = await client.get(f"/v1/tasks/{task_id}", headers=HEADERS)
        row = await response.json()
        if row["state"] not in {"QUEUED", "RUNNING", "WAITING_EXTERNAL", "RECONCILING", "CANCELLING"}:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("durable task stalled")


async def test_lost_ack_reconciles_same_external_job_without_redispatch(ledger, tmp_path, monkeypatch):
    await ledger.admit("alice", "one", "retry-write-bug", target(ledger, "retry-write-bug"))
    old = await ledger.claim("crashing-worker")
    operation = await ledger.operation(old)
    await ledger.reserve_round(old)
    await ledger.checkpoint(old, {"phase": "awaiting_tools", "iteration": 1})
    await ledger.dispatch(old, operation)
    store = ArtifactStore(tmp_path / "artifacts")
    backend = RetainedRunner(old, operation, store, IMAGE, tmp_path / "jobs")
    actual = await backend.dispatch()
    # Simulated crash: external completed, response/result was NOT committed in PG.
    assert (await ledger.find_operation(old)).result is None
    await expire(ledger, old)
    replacement = await ledger.claim("replacement")
    with pytest.raises(LeaseLostError):
        await ledger.outcome(old, operation, actual)

    async def forbidden(self):
        raise AssertionError("UNKNOWN dispatch must never start a new Job")

    monkeypatch.setattr(RetainedRunner, "dispatch", forbidden)
    restored_op = await ledger.operation(replacement)
    restored = DurableExecutor(ledger, replacement, restored_op, store,
                               RetainedRunner(replacement, restored_op, store, IMAGE, tmp_path / "jobs"))
    controls = RunControls(lambda body: ledger.checkpoint(replacement, body),
                           lambda: ledger.reserve_round(replacement), 4-replacement.model_rounds)
    report = await run(restored, controls)
    assert report["report_validated"] is True
    assert report["quality_verdict"] == "FAIL"
    assert report["external_run_id"] == actual.external_run_id
    assert report["run_id"] == replacement.run_id
    await ledger.outcome(replacement, restored_op, None)  # Late unknown must not erase already committed success.
    assert (await ledger.find_operation(replacement)).state == "SUCCEEDED"
    await ledger.finish(replacement, report)
    jobs = await docker("container", "ls", "-aq", "--filter", "label=testpilot.operation=" + operation.id)
    assert len(jobs.strip().splitlines()) == 1  # External server count, not Agent trace count.
    final = await ledger.get(replacement.id, "alice")
    assert final.model_rounds == 3


async def test_unknown_without_external_job_is_review_not_retry(ledger, tmp_path):
    await ledger.admit("alice", "unknown", "healthy", target(ledger))
    task = await ledger.claim("worker")
    operation = await ledger.operation(task)
    await ledger.dispatch(task, operation)
    operation = await ledger.find_operation(task)
    store = ArtifactStore(tmp_path / "store")
    backend = RetainedRunner(task, operation, store, IMAGE, tmp_path / "jobs")
    executor = DurableExecutor(ledger, task, operation, store, backend)
    with pytest.raises(UnknownDispatchError, match="absent"):
        await executor.execute()
    assert (await ledger.find_operation(task)).state == "UNKNOWN"
    assert not (await docker("container", "ls", "-aq", "--filter", "label=testpilot.operation=" + operation.id)).strip()
    with pytest.raises(UnknownDispatchError, match="late creation"):
        await backend.cancel()


async def test_completed_task_api_restart_retains_report_dedup_and_artifacts(ledger, tmp_path):
    def app():
        return create_app(tokens={TOKEN: Principal("alice", ledger.tenant_id)}, ledger=ledger,
                          root=tmp_path, image=IMAGE, target=lambda mode: target(ledger, mode), run=run, workers=1, poll_delay=0.05)

    async with TestClient(TestServer(app())) as client:
        created = await client.post("/v1/tasks", json={"mode": "healthy"}, headers=HEADERS)
        assert created.status == 202
        task_id = (await created.json())["task_id"]
        assert (await wait(client, task_id))["state"] == "COMPLETED"
        result = await client.get(f"/v1/tasks/{task_id}/report", headers=HEADERS)
        original = await result.json()
        assert original["test_summary"]["passed"] == 4
        rows = await (await client.get(f"/v1/tasks/{task_id}/events", headers=HEADERS)).json()
        assert "checkpoint.saved" in {event["type"] for event in rows["events"]}
    async with TestClient(TestServer(app())) as restarted:
        response = await restarted.post("/v1/tasks", json={"mode": "healthy"}, headers=HEADERS)
        assert response.status == 200
        assert (await response.json())["task_id"] == task_id
        result = await restarted.get(f"/v1/tasks/{task_id}/report", headers=HEADERS)
        assert await result.json() == original
        for content_hash in original["artifact_refs"]:
            result = await restarted.get(f"/v1/tasks/{task_id}/artifacts/{content_hash}", headers=HEADERS)
            assert result.status == 200 and await result.read()
        corrupted = tmp_path / ledger.tenant_id / task_id / "artifacts" / original["artifact_refs"][0]
        corrupted.write_bytes(b"tampered after publication")
        assert (await restarted.get(f"/v1/tasks/{task_id}/report", headers=HEADERS)).status == 409


async def test_database_unavailable_blocks_admission_with_503(tmp_path):
    unavailable = Ledger("postgresql://unused@127.0.0.1:1/unavailable")
    app = create_app(tokens={TOKEN: Principal("alice")}, ledger=unavailable, root=tmp_path,
                     image=IMAGE, target=lambda mode: target(unavailable, mode), run=run)
    # Start only the HTTP surface; normal startup would correctly fail readiness before listening.
    app.on_startup.clear()
    async with TestClient(TestServer(app)) as client:
        result = await client.post("/v1/tasks", json={}, headers=HEADERS)
        assert result.status == 503


async def test_cancel_unknown_absent_job_is_not_falsely_confirmed(ledger, tmp_path):
    created, _ = await ledger.admit("alice", "one", "healthy", target(ledger))
    old = await ledger.claim("old-worker")
    operation = await ledger.operation(old)
    await ledger.dispatch(old, operation)
    await ledger.cancel(created.id, "alice")
    await expire(ledger, old)
    app = create_app(tokens={TOKEN: Principal("alice", ledger.tenant_id)}, ledger=ledger,
                     root=tmp_path, image=IMAGE, target=lambda mode: target(ledger, mode), run=run, workers=1, poll_delay=0.05)
    async with TestClient(TestServer(app)) as client:
        task = await wait(client, created.id)
        assert task["state"] == "NEEDS_REVIEW"
        assert task["error"] == "CANCELLATION_UNCONFIRMED"
        assert (await ledger.find_operation(old)).state == "UNKNOWN"


async def test_reconcile_rejects_external_job_with_wrong_environment(ledger, tmp_path):
    await ledger.admit("alice", "one", "healthy", target(ledger))
    task = await ledger.claim("worker")
    operation = await ledger.operation(task)
    store = ArtifactStore(tmp_path / "store")
    backend = RetainedRunner(task, operation, store, IMAGE, tmp_path / "jobs")
    original = backend.profile.command
    backend.profile.command = lambda path: [
        "TESTPILOT_FIXTURE_MODE=retry-write-bug" if arg.startswith("TESTPILOT_FIXTURE_MODE=") else arg
        for arg in original(path)
    ]
    executor = DurableExecutor(ledger, task, operation, store, backend)
    with pytest.raises(UnknownDispatchError, match="snapshot mismatch"):
        await executor.execute()
    persisted = await ledger.find_operation(task)
    assert persisted.state == "UNKNOWN" and persisted.result is None
