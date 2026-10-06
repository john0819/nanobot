"""HTTP task/auth/idempotency/publication contracts using the actual nanobot adapter."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from testpilot.baseline import FORK_BASELINE_SHA
from testpilot.demo_provider import ScriptedProvider
from testpilot.execution import FixedFixtureExecutor, fixture_target
from testpilot.nanobot_adapter import run_task
from testpilot.task_api import Principal, create_app

TOKEN = "a" * 40
OTHER_TOKEN = "b" * 40
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "request-1"}


def make_app(tmp_path, run=None, **limits):
    async def execute(executor):
        provider = ScriptedProvider()
        return await run_task(executor, provider, provider.get_default_model())

    return create_app(tokens={TOKEN: Principal("owner"), OTHER_TOKEN: Principal("other")},
                      root=tmp_path,
                      executor_factory=lambda store, task_id, mode: FixedFixtureExecutor(
                          store, task_id, fixture_target(FORK_BASELINE_SHA, mode), mode),
                      run=run or execute, **limits)


async def wait_task(client, task_id):
    for _ in range(150):
        response = await client.get(f"/v1/tasks/{task_id}", headers=HEADERS)
        task = await response.json()
        if task["state"] not in {"RUNNING", "QUEUED"}:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError("task did not finish within test deadline")


async def test_http_task_real_execution_report_artifact_and_idempotency(tmp_path):
    async with TestClient(TestServer(make_app(tmp_path))) as client:
        created = await client.post("/v1/tasks", json={"mode": "retry-write-bug"}, headers=HEADERS)
        assert created.status == 202
        task = await created.json()
        replay = await client.post("/v1/tasks", json={"mode": "retry-write-bug"}, headers=HEADERS)
        assert replay.status == 200
        assert (await replay.json())["task_id"] == task["task_id"]
        conflict = await client.post("/v1/tasks", json={"mode": "healthy"}, headers=HEADERS)
        assert conflict.status == 409
        final = await wait_task(client, task["task_id"])
        assert final["state"] == "COMPLETED"
        response = await client.get(f"/v1/tasks/{task['task_id']}/report", headers=HEADERS)
        report = await response.json()
        assert report["quality_verdict"] == "FAIL"
        assert report["test_summary"]["failed"] == 1
        for content_hash in report["artifact_refs"]:
            result = await client.get(f"/v1/tasks/{task['task_id']}/artifacts/{content_hash}", headers=HEADERS)
            assert result.status == 200
            assert await result.read()
        assert (tmp_path / task["task_id"] / "execution.json").exists()


async def test_auth_identity_injection_and_unknown_contract_rejected(tmp_path):
    async with TestClient(TestServer(make_app(tmp_path))) as client:
        assert (await client.post("/v1/tasks", json={})).status == 401
        assert (await client.post("/v1/tasks", json={}, headers={"Authorization": "Bearer bad"})).status == 401
        assert (await client.post("/v1/tasks", json={}, headers={"Authorization": f"Bearer {TOKEN}"})).status == 422
        assert (await client.post("/v1/tasks", json={"tenant_id": "other"}, headers=HEADERS)).status == 422
        assert (await client.post("/v1/tasks", json={"source": "assert True"}, headers=HEADERS)).status == 422
        assert (await client.post("/v1/tasks", json={"mode": "arbitrary-target"}, headers=HEADERS)).status == 422
        assert (await client.get("/openapi.json")).status == 401
        contract = await client.get("/openapi.json", headers=HEADERS)
        spec = await contract.json()
        assert spec["openapi"] == "3.1.0"
        assert spec["components"]["schemas"]["TaskRequest"]["additionalProperties"] is False


async def test_concurrent_cancel_waits_for_same_cleanup_without_second_interrupt(tmp_path):
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_completed = asyncio.Event()

    async def wait(executor):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
            cleanup_completed.set()

    async with TestClient(TestServer(make_app(tmp_path, run=wait))) as client:
        created = await client.post("/v1/tasks", json={}, headers=HEADERS)
        task = await created.json()
        await started.wait()
        path = f"/v1/tasks/{task['task_id']}/cancel"
        first = asyncio.create_task(client.post(path, headers=HEADERS))
        await cleanup_started.wait()
        second = asyncio.create_task(client.post(path, headers=HEADERS))
        await asyncio.sleep(0.02)
        assert not cleanup_completed.is_set()
        allow_cleanup.set()
        for response in await asyncio.gather(first, second):
            assert (await response.json())["state"] == "CANCELLED"
        assert cleanup_completed.is_set()


async def test_other_principal_cannot_query_report_cancel_or_fetch_artifacts(tmp_path):
    async with TestClient(TestServer(make_app(tmp_path))) as client:
        created = await client.post("/v1/tasks", json={}, headers=HEADERS)
        task = await created.json()
        await wait_task(client, task["task_id"])
        base = f"/v1/tasks/{task['task_id']}"
        other = {"Authorization": f"Bearer {OTHER_TOKEN}"}
        for suffix in ("", "/report", "/artifacts/" + "a" * 64):
            assert (await client.get(base + suffix, headers=other)).status == 404
        assert (await client.post(base + "/cancel", headers=other)).status == 404
        assert (await client.get(base + "/artifacts/" + "a" * 64, headers=HEADERS)).status == 404


async def test_pending_capacity_and_cancel_are_bounded(tmp_path):
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def wait(executor):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async with TestClient(TestServer(make_app(tmp_path, run=wait, max_active=1))) as client:
        created = await client.post("/v1/tasks", json={}, headers=HEADERS)
        task = await created.json()
        await started.wait()
        assert (await client.get(f"/v1/tasks/{task['task_id']}/report", headers=HEADERS)).status == 409
        extra = {**HEADERS, "Idempotency-Key": "request-2"}
        assert (await client.post("/v1/tasks", json={}, headers=extra)).status == 429
        result = await client.post(f"/v1/tasks/{task['task_id']}/cancel", headers=HEADERS)
        assert (await result.json())["state"] == "CANCELLED"
        assert cleaned.is_set()
        replay = await client.post(f"/v1/tasks/{task['task_id']}/cancel", headers=HEADERS)
        assert (await replay.json())["state"] == "CANCELLED"


async def test_failure_does_not_publish_unvalidated_stack_or_model_text(tmp_path):
    async def fail(executor):
        raise RuntimeError("private-secret-stack")

    async with TestClient(TestServer(make_app(tmp_path, run=fail))) as client:
        created = await client.post("/v1/tasks", json={}, headers=HEADERS)
        task = await created.json()
        result = await wait_task(client, task["task_id"])
        assert result["state"] == "NEEDS_REVIEW"
        assert result["error"] == "TASK_EXECUTION_UNRESOLVED"
        assert "private-secret-stack" not in str(result)


def test_weak_or_empty_token_fails_startup(tmp_path):
    with pytest.raises(ValueError):
        create_app(tokens={}, root=tmp_path, executor_factory=None, run=None)
