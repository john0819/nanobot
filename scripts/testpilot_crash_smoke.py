"""Kill a real nanobot service mid-Job, restart against PG, and count external Jobs."""

import asyncio
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from testpilot.storage.postgres import Ledger


def request(port: int, token: str, method: str, path: str, body: dict[str, str] | None = None,
            key: str = "") -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        connection.request(method, path, json.dumps(body) if body else None, headers)
        response = connection.getresponse()
        content = response.read()
        return response.status, json.loads(content) if content else None
    finally:
        connection.close()


def main() -> None:
    dsn = os.environ["TESTPILOT_DATABASE_URL"]
    ledger = Ledger(dsn)
    asyncio.run(ledger.migrate())
    image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", "testpilot-runner:dev"], text=True).strip()
    output = Path(".local/testpilot-crash-smoke") / ("session_" + uuid4().hex)
    output = output.resolve()
    output.mkdir(parents=True)
    key, token = "crash_" + uuid4().hex, secrets.token_urlsafe(40)
    with socket.socket() as socket_handle:
        socket_handle.bind(("127.0.0.1", 0))
        port = socket_handle.getsockname()[1]
    command = [sys.executable, "-m", "nanobot", "testpilot", "serve", "--durable", "--lease-seconds", "3",
               "--runner-image", image, "--port", str(port), "--output", str(output / "tasks")]
    env = dict(os.environ, TESTPILOT_API_TOKEN=token)
    process: subprocess.Popen[bytes] | None = None
    operation_id: str | None = None

    def start(log_path: Path) -> subprocess.Popen[bytes]:
        with log_path.open("wb") as log:
            service = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(100):
            if service.poll() is not None:
                raise RuntimeError("service startup failed; inspect local log")
            try:
                if request(port, token, "GET", "/health/ready")[0] == 200:
                    return service
            except OSError:
                pass
            time.sleep(0.1)
        service.terminate()
        service.wait()
        raise RuntimeError("service startup deadline")

    try:
        process = start(output / "before-kill.log")
        status, created = request(port, token, "POST", "/v1/tasks", {"mode": "retry-write-bug"}, key)
        assert status == 202 and isinstance(created, dict)
        task_id = created["task_id"]
        external_id: str | None = None
        first_epoch = 0
        for _ in range(200):
            task = asyncio.run(ledger.get(task_id, "local-reviewer"))
            assert task is not None
            operation = asyncio.run(ledger.find_operation(task))
            if operation is not None:
                operation_id = operation.id
                content = subprocess.run(["docker", "container", "inspect", "testpilot-" + operation_id],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
                if content.returncode == 0:
                    external_id = json.loads(content.stdout)[0]["Id"]
                    first_epoch = task.lease_epoch
                    process.kill()  # Real SIGKILL: no finally/checkpoint/cleanup gets to run.
                    process.wait(timeout=10)
                    break
            time.sleep(0.02)
        assert external_id and operation_id, "did not observe a real external Job"
        killed_task = asyncio.run(ledger.get(task_id, "local-reviewer"))
        assert killed_task is not None and killed_task.state in {"RUNNING", "QUEUED"}, "missed pre-publication kill boundary"
        killed_operation = asyncio.run(ledger.find_operation(killed_task))
        process = start(output / "after-restart.log")
        final: dict = {}
        for _ in range(200):
            _, snapshot = request(port, token, "GET", f"/v1/tasks/{task_id}")
            assert isinstance(snapshot, dict)
            final = snapshot
            if final["state"] not in {"QUEUED", "RUNNING"}:
                break
            time.sleep(0.1)
        assert final.get("state") == "COMPLETED", final
        assert final["lease_epoch"] > first_epoch
        status, report = request(port, token, "GET", f"/v1/tasks/{task_id}/report")
        assert status == 200 and isinstance(report, dict) and report["report_validated"] is True
        assert report["external_run_id"] == external_id
        assert report["test_summary"]["passed"] == 3 and report["test_summary"]["failed"] == 1
        count = subprocess.check_output(["docker", "container", "ls", "-aq", "--filter",
                                         "label=testpilot.operation=" + operation_id], text=True)
        assert len(count.strip().splitlines()) == 1
        status, replay = request(port, token, "POST", "/v1/tasks", {"mode": "retry-write-bug"}, key)
        assert status == 200 and isinstance(replay, dict) and replay["task_id"] == task_id
        summary = {"task_id": task_id, "operation_id": operation_id, "external_run_id": external_id,
                   "kill_operation_state": killed_operation.state if killed_operation else None,
                   "external_job_count": 1, "report_validated": True, "quality_verdict": report["quality_verdict"],
                   "counts": report["test_summary"], "epoch_before": first_epoch, "epoch_after": final["lease_epoch"],
                   "model_rounds": final["model_rounds"], "request_replayed_after_restart": True}
        (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary), flush=True)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if operation_id:
            # Evidence is now committed; clean only this smoke job's explicitly known container.
            subprocess.run(["docker", "rm", "-f", "testpilot-" + operation_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
