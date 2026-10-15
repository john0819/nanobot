"""One real model task: request input -> resume -> failed pytest -> immutable defect review."""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import jwt
from aiohttp import ClientSession
from cryptography.hazmat.primitives.asymmetric import rsa


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Explicit paid-model configuration, one task only")
    args = parser.parse_args()
    os.environ["TESTPILOT_DATABASE_URL"]
    root = (Path(".local/testpilot-business-smoke")/uuid4().hex).resolve()
    root.mkdir(parents=True, mode=0o700)
    tenant = "business_"+uuid4().hex
    image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", "testpilot-runner:dev"], text=True).strip()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public.update(kid="smoke", alg="RS256")
    jwks = root/"jwks.json"
    jwks.write_text(json.dumps({"keys": [public]}))
    settings = root/"settings.json"
    settings.write_text(json.dumps({"testpilot": {"tenantId": tenant, "jwtIssuer": "https://smoke.testpilot.example",
        "jwtAudience": "testpilot", "jwksFile": str(jwks), "modelLimit": 12}}))
    settings.chmod(0o600)

    def token(actor: str, roles: list[str]) -> str:
        now = int(time.time())
        return jwt.encode({"iss": "https://smoke.testpilot.example", "aud": "testpilot", "sub": actor,
            "tenant_id": tenant, "projects": ["gateway-fixture"], "roles": roles, "groups": [],
            "iat": now, "nbf": now, "exp": now+300, "jti": uuid4().hex}, key, algorithm="RS256", headers={"kid": "smoke"})

    owner, reviewer = token("alice", ["executor", "reviewer"]), token("bob", ["reviewer"])
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        port = handle.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    operation_id = None
    command = [sys.executable, "-m", "nanobot", "testpilot", "serve", "--durable", "--runner-image", image,
        "--config", str(args.config.resolve()), "--settings", str(settings), "--port", str(port),
        "--output", str(root/"api"), "--job-poll-seconds", "0.1"]
    with (root/"api.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    try:
        async with ClientSession(headers={"Authorization": "Bearer "+owner}) as client:
            for _ in range(150):
                if process.poll() is not None:
                    raise RuntimeError("nanobot startup failed; inspect private local log")
                try:
                    async with client.get(base+"/health/ready") as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("startup deadline")

            async def get_task(task_id: str) -> dict:
                async with client.get(base+f"/v1/tasks/{task_id}") as response:
                    assert response.status == 200
                    return await response.json()

            goal = ("Before executing any tests, use request_input alone to ask which reviewed contract to use, "
                    "reason MISSING_CONTEXT. Wait for the owner's response. Then execute the admitted suite without changing "
                    "target or assertions and return evidence-backed report JSON with actual failures.")
            async with client.post(base+"/v1/tasks", json={"mode": "retry-write-bug", "goal": goal}, headers={"Idempotency-Key": uuid4().hex}) as response:
                assert response.status == 202
                task_id = (await response.json())["task_id"]
            answered = False
            paused_rounds = None
            for _ in range(1400):
                task = await get_task(task_id)
                operation_id = task.get("operation_id") or operation_id
                if task["waiting_reason"] == "INPUT_REQUIRED" and not answered:
                    paused_rounds = task["model_rounds"]
                    async with client.post(base+f"/v1/tasks/{task_id}/resume", json={"expected_state_version": task["state_version"]}) as response:
                        assert response.status == 409
                    async with client.post(base+f"/v1/tasks/{task_id}/inputs", json={"client_request_id": "contract", "text": "Use the current admitted suite and its original reviewed assertions. Preserve actual failures; do not change scope or expected results."}) as response:
                        assert response.status == 202
                    current = await get_task(task_id)
                    async with client.post(base+f"/v1/tasks/{task_id}/resume", json={"expected_state_version": current["state_version"]}) as response:
                        assert response.status == 202
                    answered = True
                if task["state"] in {"COMPLETED", "NEEDS_REVIEW", "CANCELLED"}:
                    break
                await asyncio.sleep(0.05)
            assert answered and task["state"] == "COMPLETED", task
            async with client.get(base+f"/v1/tasks/{task_id}/report") as response:
                report = await response.json()
            assert report["report_validated"] and report["quality_verdict"] == "FAIL"
            assert report["test_summary"]["passed"] == 3 and report["test_summary"]["failed"] == 1
            async with client.post(base+f"/v1/tasks/{task_id}/defects", json={"run_id": task["run_id"], "case_id": report["findings"][0]["case_id"]}) as response:
                assert response.status == 201
                draft = await response.json()
            decision = {"expected_version": draft["version"], "content_hash": draft["content_hash"], "decision": "APPROVE"}
            decision_url = base+f"/v1/defects/{draft['id']}/decision"
            async with client.post(decision_url, json=decision) as response:
                assert response.status == 409  # Owner also has reviewer role; independent review still required.
            async with client.post(decision_url, json=decision, headers={"Authorization": "Bearer "+reviewer}) as response:
                assert response.status == 200 and (await response.json())["status"] == "APPROVED"
            count = subprocess.check_output(["docker", "ps", "-aq", "--filter", "label=testpilot.operation="+operation_id], text=True)
            assert len(count.splitlines()) == 1
            result = {"task_id": task_id, "model_kind": "configured-real-model", "input_required_and_resumed": answered,
                "paused_model_rounds": paused_rounds, "model_rounds": task["model_rounds"],
                "report_validated": True, "quality_verdict": "FAIL", "passed": 3, "failed": 1,
                "defect_id": draft["id"], "defect_status": "APPROVED", "external_submission": False,
                "self_review_rejected": True, "job_count": 1}
            (root/"result.json").write_text(json.dumps(result, indent=2)+"\n")
            print(json.dumps(result))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if operation_id:
            subprocess.run(["docker", "rm", "-f", "testpilot-"+operation_id], stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    asyncio.run(main())
