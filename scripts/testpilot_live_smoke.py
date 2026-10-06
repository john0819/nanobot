"""Start the real nanobot Task API, verify container jobs, then cleanly stop it.

Use --config for a real configured model; the default is the scripted contract
provider. Secrets are inherited/generated in process environment, never printed.
"""

import argparse
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".local/testpilot-live-smoke"))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}",
                                     "testpilot-runner:dev"], text=True).strip()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_urlsafe(40)
    env = dict(os.environ, TESTPILOT_API_TOKEN=token)
    command = [sys.executable, "-m", "nanobot", "testpilot", "serve", "--runner-image", image,
               "--port", str(port), "--output", str(output / "tasks")]
    if args.config:
        command.extend(["--config", str(args.config.resolve())])

    def request(method: str, path: str, data: dict[str, str] | None = None,
                authenticated: bool = True, key: str = "") -> tuple[int, object]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {token}"
        if key:
            headers["Idempotency-Key"] = key
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None, headers)
            response = connection.getresponse()
            body = response.read()
            try:
                value = json.loads(body)
            except json.JSONDecodeError:
                value = body.decode()
            return response.status, value
        finally:
            connection.close()

    summary: list[dict[str, object]] = []
    with (output / "nanobot.log").open("w") as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            ready = False
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("nanobot service exited during startup; inspect local log")
                try:
                    if request("GET", "/health/live", authenticated=False)[0] == 200:
                        ready = True
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            assert ready, "nanobot service did not become ready"
            assert request("POST", "/v1/tasks", {}, authenticated=False)[0] == 401
            for mode, passed, failed in [("healthy", 4, 0), ("retry-write-bug", 3, 1)]:
                status, created = request("POST", "/v1/tasks", {"mode": mode}, key=mode)
                assert status == 202 and isinstance(created, dict)
                task_id = created["task_id"]
                status, replay = request("POST", "/v1/tasks", {"mode": mode}, key=mode)
                assert status == 200 and isinstance(replay, dict) and replay["task_id"] == task_id
                deadline = time.monotonic() + 100
                state: dict = {}
                while time.monotonic() < deadline:
                    _, snapshot = request("GET", f"/v1/tasks/{task_id}")
                    assert isinstance(snapshot, dict)
                    state = snapshot
                    if state["state"] not in {"QUEUED", "RUNNING"}:
                        break
                    time.sleep(0.2)
                assert state.get("state") == "COMPLETED", state
                status, report = request("GET", f"/v1/tasks/{task_id}/report")
                assert status == 200 and isinstance(report, dict)
                assert report["report_validated"] is True, report.get("validation_gaps")
                assert report["test_summary"]["passed"] == passed
                assert report["test_summary"]["failed"] == failed
                for content_hash in report["artifact_refs"]:
                    assert request("GET", f"/v1/tasks/{task_id}/artifacts/{content_hash}")[0] == 200
                operation_id = report["operation_id"]
                residual = subprocess.check_output(["docker", "container", "ls", "-aq", "--filter",
                                                    f"label=testpilot.operation={operation_id}"], text=True)
                assert not residual.strip(), "Runner container left behind"
                result = {"mode": mode, "task_id": task_id, "report_validated": True,
                          "quality_verdict": report["quality_verdict"], "counts": report["test_summary"],
                          "model_rounds": report.get("model_rounds"), "operation_id": operation_id}
                summary.append(result)
                print(json.dumps(result), flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    (output / "summary.json").write_text(json.dumps({
        "entrypoint": "nanobot testpilot serve", "runner_image": image,
        "provider_mode": "configured" if args.config else "scripted", "results": summary,
        "unauthorized_rejected": True, "idempotency_verified": True, "residual_containers": 0,
    }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
