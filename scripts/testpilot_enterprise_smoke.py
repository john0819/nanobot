"""Actual nanobot CLI processes: Git commit -> remote Runner -> fenced report -> inventory."""

import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from aiohttp import ClientSession


def port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


async def main() -> None:
    os.environ["TESTPILOT_DATABASE_URL"]
    image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", "testpilot-runner:dev"], text=True).strip()
    root = (Path(".local/testpilot-enterprise-smoke")/("session_"+uuid4().hex)).resolve()
    repo = root/"repo"
    repo.mkdir(parents=True)
    content = b"import time\ndef test_contract():\n    time.sleep(2)\n    assert 2 + 2 == 4\n"
    (repo/"test_contract.py").write_bytes(content)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "test_contract.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=TestPilot", "-c", "user.email=testpilot@example.invalid", "commit", "-qm", "Reviewed immutable suite"], check=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    manifest = root/"manifest.json"
    manifest.write_text(json.dumps({"commit_sha": commit, "files": {"test_contract.py": hashlib.sha256(content).hexdigest()},
        "expected_cases": ["test_contract::test_contract"], "test_paths": ["test_contract.py"]}))
    # Dirty worktree must never alter the admitted execution.
    (repo/"test_contract.py").write_text("raise RuntimeError('unreviewed worktree')")
    runner_port, api_port = port(), port()
    runner_token, api_token = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    settings = root/"settings.json"
    settings.write_text(json.dumps({"testpilot": {"tenantId": "smoke_"+uuid4().hex, "gitRepository": str(repo), "gitManifest": str(manifest),
        "runnerEndpoint": f"http://127.0.0.1:{runner_port}", "runnerServiceToken": runner_token}}))
    settings.chmod(0o600)
    base = f"http://127.0.0.1:{api_port}"
    prefix = [sys.executable, "-m", "nanobot", "testpilot"]
    common = ["--runner-image", image, "--settings", str(settings)]
    runner_command = prefix+["runner", *common, "--port", str(runner_port), "--output", str(root/"runner")]
    processes: list[subprocess.Popen[bytes]] = []
    operation_id = None
    load_operations: list[str] = []

    def start(command: list[str], name: str) -> subprocess.Popen[bytes]:
        with (root/name).open("ab") as log:
            process = subprocess.Popen(command, env=dict(os.environ, TESTPILOT_API_TOKEN=api_token), stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    try:
        runner_process = start(runner_command, "runner.log")
        api_process = start(prefix+["serve", "--durable", *common, "--port", str(api_port),
            "--output", str(root/"api"), "--job-poll-seconds", "0.1"], "api.log")
        async with ClientSession(headers={"Authorization": "Bearer "+api_token}) as client:
            for _ in range(150):
                if api_process.poll() is not None or runner_process.poll() is not None:
                    raise RuntimeError("CLI process startup failed; inspect private local logs")
                try:
                    async with client.get(base+"/health/ready") as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Startup deadline")
            async with client.post(base+"/v1/tasks", json={"mode": "healthy"}, headers={"Idempotency-Key": uuid4().hex}) as response:
                assert response.status == 202
                task_id = (await response.json())["task_id"]
            restarted = False
            for _ in range(700):
                async with client.get(base+f"/v1/tasks/{task_id}") as response:
                    task = await response.json()
                operation_id = task.get("operation_id") or operation_id
                if task["state"] == "WAITING_EXTERNAL" and not restarted:
                    existing = subprocess.check_output(["docker", "ps", "-aq", "--filter", "label=testpilot.operation="+operation_id], text=True)
                    if existing.strip():
                        runner_process.kill()  # Kill only after the external Job actually exists.
                        runner_process.wait(timeout=5)
                        runner_process = start(runner_command, "runner.log")
                        restarted = True
                if task["state"] in {"COMPLETED", "NEEDS_REVIEW", "CANCELLED"}:
                    break
                await asyncio.sleep(0.05)
            assert restarted and task["state"] == "COMPLETED", task
            async with client.get(base+f"/v1/tasks/{task_id}/report") as response:
                report = await response.json()
            assert report["report_validated"] and report["target"]["commit_sha"] == commit
            async with client.get(base+f"/v1/tasks/{task_id}/traces") as response:
                trace = await response.json()
            assert any(item["kind"] == "MODEL_ROUND" for item in trace["traces"])
            assert any(item["kind"] == "TOOL" for item in trace["traces"])
            count = subprocess.check_output(["docker", "ps", "-aq", "--filter", "label=testpilot.operation="+operation_id], text=True)
            assert len(count.splitlines()) == 1
            verification = subprocess.check_output(prefix+["verify-artifacts", task_id, "local-reviewer", "--settings", str(settings), "--output", str(root/"api")], text=True)
            inventory = json.loads(verification)
            assert inventory["integrity_ok"] and len(inventory["verified"]) >= 3
            result = {"task_id": task_id, "report_validated": True, "commit_sha": commit,
                "runner_process_restart": True, "job_count": 1, "trace_count": len(trace["traces"]),
                "artifacts_verified": len(inventory["verified"]), "evaluation_kind": "scripted-provider-real-git-remote-runner"}
            if "--load10" in sys.argv:
                started = time.monotonic()

                async def create_load_task() -> str:
                    async with client.post(base+"/v1/tasks", json={"mode": "healthy"}, headers={"Idempotency-Key": uuid4().hex}) as response:
                        assert response.status == 202
                        return (await response.json())["task_id"]

                task_ids = await asyncio.gather(*[create_load_task() for _ in range(10)])
                admission_ms = (time.monotonic()-started)*1000

                async def await_report(identifier: str) -> dict:
                    for _ in range(1000):
                        async with client.get(base+f"/v1/tasks/{identifier}") as response:
                            row = await response.json()
                        if row.get("operation_id") and row["operation_id"] not in load_operations:
                            load_operations.append(row["operation_id"])
                        if row["state"] in {"COMPLETED", "NEEDS_REVIEW", "CANCELLED"}:
                            assert row["state"] == "COMPLETED", row
                            async with client.get(base+f"/v1/tasks/{identifier}/report") as response:
                                final = await response.json()
                            assert final["report_validated"] and final["quality_verdict"] == "PASS"
                            return final
                        await asyncio.sleep(0.05)
                    raise RuntimeError("10-concurrent-task deadline")

                reports = await asyncio.gather(*[await_report(identifier) for identifier in task_ids])
                assert len({report["operation_id"] for report in reports}) == 10
                for external in load_operations:
                    count = subprocess.check_output(["docker", "ps", "-aq", "--filter", "label=testpilot.operation="+external], text=True)
                    assert len(count.splitlines()) == 1
                result.update(concurrent_tasks=10, concurrent_reports_validated=10,
                              admission_ms=round(admission_ms, 2), concurrent_elapsed_seconds=round(time.monotonic()-started, 2))
            (root/"result.json").write_text(json.dumps(result, indent=2)+"\n")
            print(json.dumps(result))
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for external in load_operations+([operation_id] if operation_id else []):
            subprocess.run(["docker", "rm", "-f", "testpilot-"+external], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    asyncio.run(main())
