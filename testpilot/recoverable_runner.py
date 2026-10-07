"""Retained Docker Job adapter: dispatch once, reconcile by immutable job identity."""

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from testpilot.artifacts import MAX_ARTIFACT_BYTES, ArtifactStore
from testpilot.container_runner import ContainerFixtureExecutor
from testpilot.domain import ExecutionRecord, PendingExecution
from testpilot.execution import EXPECTED_CASES
from testpilot.storage.postgres import OperationRow, TaskRow


class UnknownDispatchError(RuntimeError):
    pass


class JobState(BaseModel):
    Status: Literal["created", "running", "restarting", "removing", "paused", "exited", "dead"]
    ExitCode: int


class JobConfig(BaseModel):
    Image: str
    Labels: dict[str, str]
    Env: list[str]


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore")
    Id: str
    State: JobState
    Config: JobConfig


async def docker(*arguments: str, required: bool = True) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "docker", *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 10)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode and required:
        raise UnknownDispatchError("Docker outcome unconfirmed")
    return stdout if process.returncode == 0 else b""


class RetainedRunner:
    """No existing container is ever started again. Missing/created jobs are not reissued.

    Retention is deliberate: Docker name uniqueness survives Worker crashes. If a
    privileged external actor deletes that name, UNKNOWN stays UNKNOWN until review.
    """

    def __init__(self, task: TaskRow, operation: OperationRow, store: ArtifactStore,
                 image: str, scratch: Path) -> None:
        if not re.fullmatch(r"op_[0-9a-f]{32}", operation.id):
            raise ValueError("invalid operation ID")
        self.task, self.operation, self.store = task, operation, store
        self.profile = ContainerFixtureExecutor(store, task.id, task.target, task.mode, image, scratch)
        self.profile.operation_id = operation.id
        self.profile.container_name = "testpilot-" + operation.id
        self.name = self.profile.container_name
        if operation.external_id not in {None, self.name}:
            raise ValueError("Job handle mismatch")
        self._may_have_dispatched = operation.state != "PREPARED"
        self.output = self.profile.scratch / operation.id / "artifacts"
        if self.output.is_symlink() or not self.output.resolve().is_relative_to(self.profile.scratch):
            raise ValueError("Runner output outside admitted scratch")
        self.output.mkdir(parents=True, exist_ok=True)
        self.output.chmod(0o777)

    async def inspect(self) -> Job | None:
        # An empty result may be absent or unavailable; either must never permit redispatch.
        content = await docker("container", "inspect", self.name, required=False)
        if not content:
            return None
        items = TypeAdapter(list[Job]).validate_json(content)
        if len(items) != 1:
            raise UnknownDispatchError("invalid Docker inspection")
        job = items[0]
        if self.operation.job_external_id is not None and job.Id != self.operation.job_external_id:
            raise UnknownDispatchError("external Job ID changed")
        if job.Config.Image != self.profile.image or job.Config.Labels.get("testpilot.operation") != self.operation.id:
            raise UnknownDispatchError("external Job identity mismatch")
        if f"TESTPILOT_FIXTURE_MODE={self.task.mode}" not in job.Config.Env or f"TESTPILOT_SUITE_HASH={self.task.target.suite_hash}" not in job.Config.Env:
            raise UnknownDispatchError("external Job target snapshot mismatch")
        return job

    async def dispatch(self) -> ExecutionRecord:
        await self.submit()
        return await self.reconcile()

    async def submit(self) -> ExecutionRecord | PendingExecution:
        self.note_dispatch()
        command = self.profile.command(self.output)[1:]
        command.remove("--rm")
        command.insert(1, "--detach")
        # run creates the unique name atomically; a conflict is reconciled, never started again.
        try:
            await docker(*command)
        except UnknownDispatchError:
            if await self.inspect() is None:
                raise
        return await self.poll()

    def note_dispatch(self) -> None:
        self._may_have_dispatched = True

    async def reconcile(self) -> ExecutionRecord:
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            job = await self.inspect()
            if job is None:
                raise UnknownDispatchError("Job absent/unqueryable; no automatic retry")
            if job.State.Status in {"exited", "dead"}:
                return await self._record(job)
            await asyncio.sleep(0.2)
        raise UnknownDispatchError("Job deadline reached; retained for operator reconciliation")

    async def poll(self) -> ExecutionRecord | PendingExecution:
        job = await self.inspect()
        if job is None:
            raise UnknownDispatchError("Job absent/unqueryable; no automatic retry")
        if job.State.Status in {"exited", "dead"}:
            return await self._record(job)
        return PendingExecution(operation_id=self.operation.id, external_run_id=job.Id)

    async def _record(self, job: Job) -> ExecutionRecord:
        junit = self.output / "junit.xml"
        if junit.is_symlink():
            raise UnknownDispatchError("Job artifact symlink")
        content_hash: str | None = None
        if junit.is_file():
            with junit.open("rb") as stream:
                content_hash = await asyncio.to_thread(self.store.put, stream.read(MAX_ARTIFACT_BYTES + 1))
        logs = await docker("logs", self.name)
        log_hash = await asyncio.to_thread(self.store.put, logs)
        return ExecutionRecord(
            evidence_id="ev_" + self.operation.id[3:], task_id=self.task.id, run_id=self.task.run_id,
            external_run_id=job.Id, operation_id=self.operation.id, target=self.task.target,
            state="COMPLETED", exit_code=job.State.ExitCode, expected_cases=EXPECTED_CASES,
            junit_hash=content_hash, log_hash=log_hash,
            observed_at=datetime.now(timezone.utc).isoformat(), source_system="isolated-container-fixture",
        )

    async def cancel(self) -> ExecutionRecord | None:
        job = await self.inspect()
        if job is None:
            # Confirm against a successful listing, not a failed inspect, before declaring absence.
            remaining = await docker("container", "ls", "-aq", "--filter", f"name=^/{self.name}$")
            if remaining.strip():
                raise UnknownDispatchError("cancellation unconfirmed")
            if self._may_have_dispatched:
                raise UnknownDispatchError("dispatched Job absent; cancellation cannot rule out late creation")
            return None
        if job.State.Status not in {"exited", "dead"}:
            if job.State.Status == "paused":
                await docker("unpause", self.name)
            await docker("stop", "--time", "1", self.name)
        stopped = await self.inspect()
        if stopped is None or stopped.State.Status not in {"exited", "dead"}:
            raise UnknownDispatchError("cancellation unconfirmed")
        return await self._record(stopped)
