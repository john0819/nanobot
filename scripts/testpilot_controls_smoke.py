"""Real nanobot API restart + paused Job reconciliation + private idempotent input.

No paid model calls. Requires TESTPILOT_DATABASE_URL and the built fixed Runner.
"""

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

from testpilot.storage.postgres import Ledger


async def main() -> None:
    ledger = Ledger(os.environ["TESTPILOT_DATABASE_URL"])
    await ledger.migrate()
    image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", "testpilot-runner:dev"], text=True).strip()
    output = (Path(".local/testpilot-controls-smoke")/uuid4().hex).resolve()
    output.mkdir(parents=True, mode=0o700)
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        port = handle.getsockname()[1]
    token = secrets.token_urlsafe(40)
    base = f"http://127.0.0.1:{port}"
    processes: list[subprocess.Popen] = []
    operation_id = None
    command = [sys.executable, "-m", "nanobot", "testpilot", "serve", "--durable", "--no-scheduler",
               "--runner-image", image, "--port", str(port), "--output", str(output/"tasks"), "--job-poll-seconds", "0.05"]

    def start_api():
        with (output/f"api-{len(processes)}.log").open("wb") as log:
            process = subprocess.Popen(command, env=dict(os.environ, TESTPILOT_API_TOKEN=token), stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    async def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)

    try:
        api = start_api()
        async with ClientSession(headers={"Authorization": "Bearer "+token}) as client:
            async def ready():
                for _ in range(150):
                    if api.poll() is not None:
                        raise RuntimeError("API startup failed; inspect local smoke logs")
                    try:
                        async with client.get(base+"/health/ready") as response:
                            if response.status == 200:
                                return
                    except OSError:
                        pass
                    await asyncio.sleep(0.1)
                raise RuntimeError("API readiness deadline")

            async def get_task(task_id):
                async with client.get(base+f"/v1/tasks/{task_id}") as response:
                    assert response.status == 200
                    return await response.json()

            async def state(task_id, expected):
                for _ in range(300):
                    result = await get_task(task_id)
                    if result["state"] in expected:
                        return result
                    await asyncio.sleep(0.05)
                raise AssertionError("Task state deadline")

            await ready()
            async with client.post(base+"/v1/tasks", json={"mode": "retry-write-bug"}, headers={"Idempotency-Key": "controls-"+uuid4().hex}) as response:
                assert response.status == 202
                task_id = (await response.json())["task_id"]
            waiting = await state(task_id, {"WAITING_EXTERNAL"})
            operation_id = waiting["operation_id"]
            async with client.post(base+f"/v1/tasks/{task_id}/pause", json={"expected_state_version": waiting["state_version"]}) as response:
                assert response.status == 202
            note = {"client_request_id": "user-note", "text": "Inspect retry semantics using the original admitted assertions"}
            for expected in [202, 200]:
                async with client.post(base+f"/v1/tasks/{task_id}/inputs", json=note) as response:
                    assert response.status == expected
            await stop(api)
            api = start_api()
            await ready()
            restarted = await get_task(task_id)
            assert restarted["state"] == "PAUSED" and restarted["model_rounds"] == waiting["model_rounds"]
            scheduler_command = [sys.executable, "-m", "nanobot", "testpilot", "scheduler", "--runner-image", image,
                                 "--output", str(output/"tasks"), "--job-poll-seconds", "0.05"]
            with (output/"scheduler.log").open("wb") as log:
                processes.append(subprocess.Popen(scheduler_command, stdout=log, stderr=subprocess.STDOUT))
            for _ in range(200):
                row = await ledger.get(task_id, "local-reviewer")
                operation = await ledger.find_operation(row)
                if operation.state == "SUCCEEDED":
                    break
                await asyncio.sleep(0.05)
            assert operation.state == "SUCCEEDED"
            paused = await get_task(task_id)
            assert paused["state"] == "PAUSED" and paused["model_rounds"] == waiting["model_rounds"]
            async with client.post(base+f"/v1/tasks/{task_id}/resume", json={"expected_state_version": paused["state_version"]}) as response:
                assert response.status == 202
            done = await state(task_id, {"COMPLETED", "NEEDS_REVIEW"})
            assert done["state"] == "COMPLETED" and done["run_id"] == waiting["run_id"]
            async with client.get(base+f"/v1/tasks/{task_id}/report") as response:
                report = await response.json()
                assert report["report_validated"] is True and report["test_summary"]["failed"] == 1
            inputs = await ledger.inputs(await ledger.get(task_id, "local-reviewer"))
            assert len(inputs) == 1 and inputs[0]["consumed_at"] is not None
            jobs = subprocess.check_output(["docker", "container", "ls", "-aq", "--filter", "label=testpilot.operation="+operation_id], text=True)
            assert len(jobs.strip().splitlines()) == 1
            summary = {"task_id": task_id, "run_id": done["run_id"], "paused_api_restart": True,
                       "scheduler_reconciled_without_model": True, "external_job_count": 1,
                       "input_deduplicated_and_projected": True, "report_validated": True,
                       "passed": 3, "failed": 1, "model_rounds": done["model_rounds"], "model_kind": "scripted"}
            (output/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
            print(f"Task controls acceptance passed: {output/'summary.json'}")
    finally:
        for process in reversed(processes):
            await stop(process)
        if operation_id:
            await asyncio.to_thread(subprocess.run, ["docker", "rm", "-f", "testpilot-"+operation_id],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    asyncio.run(main())
