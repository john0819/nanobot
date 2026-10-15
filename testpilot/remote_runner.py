"""Remote Runner client: one submit, query-only UNKNOWN recovery, verified artifact transfer."""

import asyncio
import json

from pydantic import BaseModel

from testpilot.artifacts import MAX_ARTIFACT_BYTES, ArtifactStore
from testpilot.domain import ExecutionRecord, PendingExecution
from testpilot.http_transport import IntegrationUnavailableError, SafeHTTP
from testpilot.recoverable_runner import RunnerQueryUnavailableError, UnknownDispatchError
from testpilot.storage.postgres import OperationRow, TaskRow


class RunnerReceipt(BaseModel):
    job_id: str
    operation_id: str
    state: str
    record: ExecutionRecord | None = None


class RemoteRunner:
    def __init__(self, task: TaskRow, operation: OperationRow, store: ArtifactStore,
                 endpoint: str, token: str, http: SafeHTTP) -> None:
        self.task, self.operation, self.store = task, operation, store
        self.endpoint, self.headers, self.http = endpoint.rstrip("/"), {"Authorization": "Bearer "+token}, http
        self.name = "testpilot-"+operation.id
        if operation.external_id not in {None, self.name}:
            raise ValueError("Remote Job identity mismatch")

    def note_dispatch(self) -> None:
        pass  # Dispatch certainty is durable in PG, never inferred from this client's memory.

    async def decode(self, raw: dict[str, object]) -> ExecutionRecord | PendingExecution:
        receipt = RunnerReceipt.model_validate_json(json.dumps(raw))
        if receipt.operation_id != self.operation.id or receipt.job_id != self.name:
            raise UnknownDispatchError("Runner receipt scope mismatch")
        if receipt.state == "UNKNOWN":
            raise RunnerQueryUnavailableError("Runner dispatch unresolved; query only")
        if receipt.record is None:
            if receipt.state == "CANCELLED":
                raise UnknownDispatchError("Runner cancelled without execution artifacts")
            return PendingExecution(operation_id=self.operation.id, external_run_id=self.name)
        record = receipt.record
        if (record.task_id, record.run_id, record.operation_id, record.target) != (self.task.id, self.task.run_id, self.operation.id, self.task.target):
            raise UnknownDispatchError("Runner execution target mismatch")
        for ref in (record.junit_hash, record.log_hash):
            if ref:
                raw_artifact = await self.http.request("GET", self.endpoint+f"/runner/v1/jobs/{self.name}/artifacts/{ref}", headers=self.headers, limit=MAX_ARTIFACT_BYTES)
                actual = await asyncio.to_thread(self.store.put, raw_artifact)
                if actual != ref:
                    raise UnknownDispatchError("Runner artifact integrity mismatch")
        return record.model_copy(update={"external_run_id": self.name})

    async def submit(self) -> ExecutionRecord | PendingExecution:
        try:
            body: dict[str, object] = {"task": self.task.model_dump(mode="json"), "operation": self.operation.model_dump(mode="json")}
            raw = await self.http.json("POST", self.endpoint+"/runner/v1/jobs", body=body,
                                       headers={**self.headers, "Idempotency-Key": self.name})
            return await self.decode(raw)
        except IntegrationUnavailableError:
            # ACK loss may hide an accepted Job. The durable operation identity is the only query key.
            return PendingExecution(operation_id=self.operation.id, external_run_id=self.name)
        except ValueError:
            raise UnknownDispatchError("Runner submit outcome unknown; no automatic re-submit") from None

    async def poll(self) -> ExecutionRecord | PendingExecution:
        try:
            raw = await self.http.json("GET", self.endpoint+"/runner/v1/operations/"+self.operation.id, headers=self.headers)
            return await self.decode(raw)
        except IntegrationUnavailableError:
            raise RunnerQueryUnavailableError("Runner query unavailable") from None
        except ValueError:
            raise UnknownDispatchError("Runner query invalid") from None

    async def dispatch(self) -> ExecutionRecord:
        result = await self.submit()
        return result if isinstance(result, ExecutionRecord) else await self.reconcile()

    async def reconcile(self) -> ExecutionRecord:
        for _ in range(100):
            result = await self.poll()
            if isinstance(result, ExecutionRecord):
                return result
            await asyncio.sleep(0.1)
        raise UnknownDispatchError("Runner still pending")

    async def cancel(self) -> ExecutionRecord | None:
        try:
            await self.http.json("POST", self.endpoint+f"/runner/v1/jobs/{self.name}/cancel", headers=self.headers, body={})
            for _ in range(100):
                raw = await self.http.json("GET", self.endpoint+"/runner/v1/operations/"+self.operation.id, headers=self.headers)
                if raw.get("state") == "CANCELLED" and raw.get("record") is None:
                    return None
                result = await self.decode(raw)
                if isinstance(result, ExecutionRecord):
                    return result
                await asyncio.sleep(0.1)
        except (IntegrationUnavailableError, ValueError):
            pass
        raise UnknownDispatchError("Runner cleanup unconfirmed")
