"""Start a real API + separate no-model Scheduler; validate waiting and SSE replay."""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from aiohttp import ClientSession


async def main() -> None:
    os.environ["TESTPILOT_DATABASE_URL"]  # Fail before creating processes if unconfigured.
    image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", "testpilot-runner:dev"], text=True).strip()
    output = (Path(".local/testpilot-async-smoke") / ("session_" + uuid4().hex)).resolve()
    output.mkdir(parents=True)
    token = secrets.token_urlsafe(40)
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        port = handle.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer " + token}
    api_command = [sys.executable, "-m", "nanobot", "testpilot", "serve", "--durable", "--no-scheduler",
                   "--workers", "1", "--runner-image", image, "--port", str(port), "--output", str(output / "tasks"),
                   "--job-poll-seconds", "0.1"]
    scheduler_command = [sys.executable, "-m", "nanobot", "testpilot", "scheduler", "--runner-image", image,
                         "--output", str(output / "tasks"), "--job-poll-seconds", "0.1"]
    processes: list[subprocess.Popen[bytes]] = []
    operations: list[str] = []
    last_id = 0
    collected: list[dict[str, object]] = []
    try:
        with (output / "api.log").open("wb") as log:
            processes.append(subprocess.Popen(api_command, env=dict(os.environ, TESTPILOT_API_TOKEN=token), stdout=log, stderr=subprocess.STDOUT))
        async with ClientSession(headers=headers) as client:
            for _ in range(100):
                if processes[0].poll() is not None:
                    raise RuntimeError("API startup failed; inspect local log")
                try:
                    async with client.get(base + "/health/ready") as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("API startup deadline")

            async def wait(task_id: str, terminal: bool) -> dict:
                for _ in range(200):
                    async with client.get(base + f"/v1/tasks/{task_id}") as response:
                        row = await response.json()
                    if row["state"] == "WAITING_EXTERNAL" and not terminal:
                        return row
                    if terminal and row["state"] in {"COMPLETED", "NEEDS_REVIEW", "CANCELLED"}:
                        return row
                    await asyncio.sleep(0.05)
                raise RuntimeError("task state deadline")

            tasks: list[str] = []
            for mode in ("retry-write-bug", "healthy"):
                async with client.post(base + "/v1/tasks", json={"mode": mode},
                                       headers={"Idempotency-Key": "async_" + uuid4().hex}) as response:
                    assert response.status == 202
                    task_id = (await response.json())["task_id"]
                tasks.append(task_id)
                waiting = await wait(task_id, terminal=False)
                assert waiting["model_rounds"] == 1 and waiting["waiting_reason"] == "EXTERNAL_JOB"
                operations.append(waiting["operation_id"])
            # Both reached external wait with exactly one Agent Worker and no Scheduler yet.
            stream_path = base + f"/v1/tasks/{tasks[0]}/events/stream"
            async with client.get(stream_path) as response:
                assert response.status == 200
                current_kind = ""
                async with asyncio.timeout(5):
                    async for raw in response.content:
                        line = raw.decode().strip()
                        if line.startswith("id: "):
                            last_id = int(line[4:])
                        if line.startswith("event: "):
                            current_kind = line[7:]
                        if not line and current_kind == "task.waiting_external":
                            break
            unchanged = await wait(tasks[0], terminal=False)
            assert unchanged["model_rounds"] == 1
            scheduler_env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR", "TESTPILOT_DATABASE_URL"}}
            with (output / "scheduler.log").open("wb") as log:
                processes.append(subprocess.Popen(scheduler_command, env=scheduler_env, stdout=log, stderr=subprocess.STDOUT))
            async with client.get(stream_path, headers={"Last-Event-ID": str(last_id)}) as response:
                seen: list[int] = []
                types: list[str] = []
                async with asyncio.timeout(15):
                    async for raw in response.content:
                        line = raw.decode().strip()
                        if line.startswith("id: "):
                            seen.append(int(line[4:]))
                        if line.startswith("event: "):
                            types.append(line[7:])
                assert seen and min(seen) > last_id and seen == sorted(set(seen))
                assert "report.validated" in types and "task.completed" in types
            for task_id in tasks:
                done = await wait(task_id, terminal=True)
                assert done["state"] == "COMPLETED"
                async with client.get(base + f"/v1/tasks/{task_id}/report") as response:
                    report = await response.json()
                assert report["report_validated"] is True
                count = subprocess.check_output(["docker", "container", "ls", "-aq", "--filter",
                                                 "label=testpilot.operation=" + report["operation_id"]], text=True)
                assert len(count.strip().splitlines()) == 1
                collected.append({"task_id": task_id, "quality_verdict": report["quality_verdict"],
                                  "counts": report["test_summary"], "model_rounds": done["model_rounds"],
                                  "external_job_count": 1})
        summary = {"separate_scheduler_process": True, "agent_workers": 1, "simultaneous_waiting_tasks": 2,
                   "waiting_model_rounds": 1, "sse_replayed_from": last_id, "results": collected}
        (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary), flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for operation_id in operations:
            subprocess.run(["docker", "rm", "-f", "testpilot-" + operation_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    asyncio.run(main())
