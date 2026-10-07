"""Real PG/S3/JWT approval + optional qa-kb-service MCP + nanobot/container acceptance.

Requires TESTPILOT_DATABASE_URL and TESTPILOT_S3_* credentials. Defaults to two
scripted-model tasks. --config opts into paid model calls; --repeats makes this a
batch evaluator. IdP is explicitly a local test double; RAG and Runner are real.
"""

import argparse
import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import jwt
from aiohttp import ClientSession, web
from cryptography.hazmat.primitives.asymmetric import rsa


def free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def private_json(path: Path, value: object) -> None:
    with open(path, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(value, stream)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Opt in to real configured model calls")
    parser.add_argument("--kb-project", type=Path, help="Start the real qa-kb-service authenticated MCP server")
    parser.add_argument("--repeats", type=int, default=1, help="Two tasks per repeat; paid when --config supplied")
    parser.add_argument("--mode", choices=["healthy", "retry-write-bug"], help="Limit acceptance to one scenario")
    parser.add_argument("--exercise-history-memory", action="store_true", help="Also review failure memory and explicitly rerun the defect task")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error("repeats must be 1..100")
    os.environ["TESTPILOT_DATABASE_URL"]
    image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", "testpilot-runner:dev"], text=True).strip()
    output = (Path(".local/testpilot-governed-smoke") / uuid4().hex).resolve()
    output.mkdir(parents=True, mode=0o700)
    api_port, kb_port, idp_port = free_port(), free_port(), free_port()
    base, kb_url = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{kb_port}/mcp"
    issuer = f"http://127.0.0.1:{idp_port}"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public.update(kid="smoke-key", alg="RS256")
    private_json(output / "jwks.json", {"keys": [public]})

    def token(actor: str, role: str) -> str:
        now = int(time.time())
        return jwt.encode({"iss": issuer, "aud": "testpilot", "sub": actor, "tenant_id": "novax-demo",
                           "projects": ["gateway-fixture"], "roles": [role], "groups": ["role:qa"],
                           "iat": now, "nbf": now, "exp": now+7200, "jti": uuid4().hex},
                          key, algorithm="RS256", headers={"kid": "smoke-key"})

    actor = "smoke_"+uuid4().hex
    creator_token, reviewer_token = token(actor, "executor"), token("independent-reviewer", "reviewer")
    kb_token, introspection_secret = secrets.token_urlsafe(40), secrets.token_urlsafe(40)

    async def introspect(request: web.Request) -> web.Response:
        supplied = (await request.post()).get("token")
        expected_auth = base64.b64encode(("qakb-smoke:"+introspection_secret).encode()).decode()
        if request.headers.get("Authorization") != "Basic "+expected_auth:
            return web.json_response({"active": False}, status=401)
        return web.json_response({"active": supplied == kb_token, "sub": actor, "client_id": "testpilot-smoke",
                                  "aud": kb_url, "scope": "kb.search", "groups": ["role:qa"],
                                  "tenant_id": "novax-demo", "iss": issuer, "exp": int(time.time())+7200})

    idp = web.Application()
    idp.router.add_post("/introspect", introspect)
    idp_runner = web.AppRunner(idp, access_log=None)
    await idp_runner.setup()
    await web.TCPSite(idp_runner, "127.0.0.1", idp_port).start()
    settings = {"development": True, "requireApproval": True, "modelLimit": 24 if args.exercise_history_memory else 12,
                "jwtIssuer": issuer, "jwtAudience": "testpilot", "jwksFile": str(output / "jwks.json"),
                "s3Endpoint": os.environ["TESTPILOT_S3_ENDPOINT"], "s3Region": "garage",
                "s3Bucket": os.environ["TESTPILOT_S3_BUCKET"],
                "s3AccessKey": os.environ["TESTPILOT_S3_ACCESS_KEY"], "s3SecretKey": os.environ["TESTPILOT_S3_SECRET_KEY"]}
    if args.kb_project:
        settings.update(knowledgeEndpoint=kb_url, knowledgeActorTokens={actor: kb_token})
    private_json(output / "settings.json", {"testpilot": settings})
    processes: list[subprocess.Popen] = []
    operations: list[str] = []
    records = []
    outcome = "FAILED"
    historical_acceptance = None
    try:
        if args.kb_project:
            env = dict(os.environ, QAKB_MCP_TRANSPORT="streamable-http", QAKB_MCP_PORT=str(kb_port),
                       QAKB_MCP_AUTH_MODE="introspection", QAKB_MCP_ISSUER_URL=issuer,
                       QAKB_MCP_RESOURCE_SERVER_URL=kb_url, QAKB_MCP_INTROSPECTION_URL=issuer+"/introspect",
                       QAKB_MCP_INTROSPECTION_CLIENT_ID="qakb-smoke", QAKB_MCP_INTROSPECTION_CLIENT_SECRET=introspection_secret,
                       QAKB_EMBEDDING_PROVIDER="fake", QAKB_RERANK_PROVIDER="fake", QAKB_MCP_REQUIRE_TENANT="false")
            with (output / "kb.log").open("wb") as log:
                processes.append(subprocess.Popen([str(args.kb_project.resolve()/".venv/bin/qakb-mcp")], cwd=args.kb_project,
                                                  env=env, stdout=log, stderr=subprocess.STDOUT))
        command = [sys.executable, "-m", "nanobot", "testpilot", "serve", "--durable", "--runner-image", image,
                   "--port", str(api_port), "--settings", str(output/"settings.json"), "--output", str(output/"tasks"),
                   "--job-poll-seconds", "0.1"]
        if args.config:
            command.extend(["--config", str(args.config.resolve())])
        with (output/"api.log").open("wb") as log:
            processes.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT))
        async with ClientSession() as client:
            async def request(method, path, value=None, reviewer=False, idempotency=None):
                headers = {"Authorization": "Bearer "+(reviewer_token if reviewer else creator_token)}
                if idempotency:
                    headers["Idempotency-Key"] = idempotency
                async with client.request(method, base+path, json=value, headers=headers) as response:
                    body = await response.read()
                    return response.status, json.loads(body) if response.content_type == "application/json" else body

            for _ in range(150):
                if any(process.poll() is not None for process in processes):
                    raise RuntimeError("Service startup failed; inspect private smoke logs")
                try:
                    async with client.get(base+"/health/ready") as response:
                        ready = response.status == 200
                    if ready and args.kb_project:
                        async with client.get(f"http://127.0.0.1:{kb_port}/health/live") as response:
                            ready = response.status == 200
                    if ready:
                        break
                except OSError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Service readiness deadline")
            for path in ("/console", "/console/app.js", "/console/style.css"):
                async with client.get(base+path) as response:
                    assert response.status == 200 and "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            async with client.get(base+"/v1/tasks") as response:
                assert response.status == 401
            for repeat in range(args.repeats):
                for mode, expected in [("healthy", (4, 0)), ("retry-write-bug", (3, 1))]:
                    if args.mode and args.mode != mode:
                        continue
                    started = time.monotonic()
                    payload = {"mode": mode, "goal": "Validate gateway retry semantics with an evidence-backed plan",
                               "require_knowledge": bool(args.kb_project)}
                    status, created = await request("POST", "/v1/tasks", payload, idempotency=f"{repeat}-{mode}")
                    assert status == 202, (status, created)
                    task_id = created["task_id"]
                    assert created["state"] == "WAITING_APPROVAL"
                    assert (await request("POST", "/v1/tasks", payload, idempotency=f"{repeat}-{mode}"))[0] == 200
                    await asyncio.sleep(0.3)
                    _, waiting = await request("GET", f"/v1/tasks/{task_id}")
                    assert waiting["operation_id"] is None and waiting["model_rounds"] == 0
                    _, approval = await request("GET", f"/v1/tasks/{task_id}/approval", reviewer=True)
                    decision = {"decision": "APPROVE", "request_hash": approval["request_hash"]}
                    assert (await request("POST", f"/v1/tasks/{task_id}/approval", decision))[0] == 403
                    assert (await request("POST", f"/v1/tasks/{task_id}/approval", decision, reviewer=True))[0] == 200
                    for _ in range(600):
                        _, state = await request("GET", f"/v1/tasks/{task_id}")
                        if state["state"] in {"COMPLETED", "NEEDS_REVIEW", "CANCELLED"}:
                            break
                        await asyncio.sleep(0.2)
                    if state.get("operation_id"):
                        operations.append(state["operation_id"])
                    assert state["state"] == "COMPLETED", state
                    status, report = await request("GET", f"/v1/tasks/{task_id}/report")
                    assert status == 200 and report["report_validated"] is True
                    assert (report["test_summary"]["passed"], report["test_summary"]["failed"]) == expected
                    if args.kb_project:
                        assert report["knowledge_evidence_ids"]
                    assert (await request("GET", f"/v1/tasks/{task_id}/plan"))[1]["version"] >= 1
                    for content_hash in report["artifact_refs"]:
                        assert (await request("GET", f"/v1/tasks/{task_id}/artifacts/{content_hash}"))[0] == 200
                    records.append({"task_id": task_id, "mode": mode, "state": state["state"],
                                    "passed": expected[0], "failed": expected[1], "model_rounds": state["model_rounds"],
                                    "knowledge_evidence_count": len(report["knowledge_evidence_ids"]),
                                    "latency_seconds": round(time.monotonic()-started, 2), "report_validated": True})
                    if args.exercise_history_memory and mode == "retry-write-bug" and repeat == 0:
                        old_run = state["run_id"]
                        case_id = report["findings"][0]["case_id"]
                        status, memory = await request("POST", f"/v1/tasks/{task_id}/memory", {"source_run_id": old_run, "case_id": case_id, "shared": True})
                        assert status == 201 and memory["status"] == "CANDIDATE"
                        status, confirmed = await request("POST", f"/v1/memory/{memory['id']}/decision", {"expected_version": memory["version"], "decision": "CONFIRM"}, reviewer=True)
                        assert status == 200 and confirmed["status"] == "CONFIRMED"
                        rerun_body = {"expected_state_version": state["state_version"], "reason": "Explicit revalidation with reviewed failure observation"}
                        status, revalidation = await request("POST", f"/v1/tasks/{task_id}/rerun", rerun_body, idempotency="revalidate")
                        assert status == 202 and revalidation["state"] == "WAITING_APPROVAL"
                        assert (await request("POST", f"/v1/tasks/{task_id}/rerun", rerun_body, idempotency="revalidate"))[0] == 200
                        _, pending = await request("GET", f"/v1/tasks/{task_id}/approval", reviewer=True)
                        assert pending["run_id"] != old_run and pending["consumed_operation_id"] is None
                        assert (await request("POST", f"/v1/tasks/{task_id}/approval", {"request_hash": pending["request_hash"], "decision": "APPROVE"}, reviewer=True))[0] == 200
                        for _ in range(600):
                            _, second = await request("GET", f"/v1/tasks/{task_id}")
                            if second["state"] in {"COMPLETED", "NEEDS_REVIEW", "CANCELLED"}:
                                break
                            await asyncio.sleep(0.2)
                        if second.get("operation_id"):
                            operations.append(second["operation_id"])
                        assert second["state"] == "COMPLETED", second
                        _, archived = await request("GET", f"/v1/tasks/{task_id}/runs/{old_run}/report")
                        assert archived == report
                        _, current = await request("GET", f"/v1/tasks/{task_id}/report")
                        assert current["test_summary"]["planned"] == 4 and current["test_summary"]["failed"] == 1
                        from testpilot.storage.postgres import Ledger
                        ledger = Ledger(os.environ["TESTPILOT_DATABASE_URL"])
                        async with ledger.connection() as connection:
                            cursor = await connection.execute("SELECT body FROM testpilot.checkpoints WHERE tenant_id=%s AND run_id=%s AND body->>'phase'='memory_read'", (ledger.tenant_id, second["run_id"]))
                            reads = await cursor.fetchall()
                            assert any(ref["memory_id"] == memory["id"] for row in reads for ref in row["body"]["refs"])
                        assert (await request("POST", f"/v1/memory/{memory['id']}/decision", {"expected_version": confirmed["version"], "decision": "REVOKE"}, reviewer=True))[0] == 200
                        historical_acceptance = {"task_id": task_id, "first_run": old_run, "rerun": second["run_id"],
                                                 "original_report_preserved": True, "model_rounds_total": second["model_rounds"],
                                                 "memory_read_verified": True, "memory_revoked": True}
        outcome = "PASSED"
        print(f"Governed acceptance passed: {len(records)} tasks; results: {output/'summary.json'}")
    finally:
        (output/"summary.json").write_text(json.dumps({"outcome": outcome,
                                                     "model_kind": "configured" if args.config else "scripted",
                                                     "idp_kind": "local-test-double", "rag": bool(args.kb_project),
                                                     "records": records, "history_memory": historical_acceptance}, indent=2)+"\n")
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
        for operation in operations:
            await asyncio.to_thread(subprocess.run, ["docker", "rm", "-f", "testpilot-"+operation],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        await idp_runner.cleanup()
        (output/"settings.json").unlink(missing_ok=True)  # Contains S3/KB secrets; do not retain in results.


if __name__ == "__main__":
    asyncio.run(main())
