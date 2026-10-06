"""Independent container jobs with a fixed image, bounded resources and no egress."""

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import uuid4

from testpilot.artifacts import ArtifactStore
from testpilot.domain import ExecutionRecord, Target
from testpilot.execution import EXPECTED_CASES, FixtureMode, fixture_target


def container_target(commit_sha: str, mode: FixtureMode, image: str) -> Target:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Runner requires an immutable local image ID, not a tag")
    target = fixture_target(commit_sha, mode)
    return target.model_copy(update={"env_snapshot_id": f"isolated-container-fixture:{image}:{mode}"})


class ContainerFixtureExecutor:
    """No generated code or Docker socket enters the container.

    Agent tools cannot choose images, host mounts, command lines or network targets.
    Docker authority is held only by the operator's local control process.
    This is single-process dispatch protection, not a durable job ledger.
    """

    def __init__(
        self, store: ArtifactStore, task_id: str, target: Target, mode: FixtureMode,
        image: str, scratch: Path,
    ) -> None:
        if mode not in {"healthy", "retry-write-bug"} or target != container_target(target.commit_sha, mode, image):
            raise ValueError("Runner target does not match admission")
        self.store, self.task_id, self.target = store, task_id, target
        self.mode: FixtureMode = mode
        self.image = image
        self.scratch = scratch.resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.operation_id = f"op_{uuid4().hex}"
        self.container_name = f"testpilot-{self.operation_id}"
        self.record: ExecutionRecord | None = None
        self._dispatched = False
        self._lock = asyncio.Lock()

    def command(self, artifacts: Path) -> list[str]:
        return [
            "docker", "run", "--rm", "--name", self.container_name,
            "--label", f"testpilot.operation={self.operation_id}",
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "10001:10001",
            "--pids-limit", "64", "--memory", "256m", "--cpus", "1",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
            "--mount", f"type=bind,source={artifacts},target=/artifacts",
            "--env", f"TESTPILOT_FIXTURE_MODE={self.mode}",
            "--env", f"TESTPILOT_SUITE_HASH={self.target.suite_hash}", self.image,
        ]

    async def _cleanup(self) -> None:
        process = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", self.container_name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), 10)
        # A removal error may mean absent or still running. Confirm absence separately.
        inspect = await asyncio.create_subprocess_exec(
            "docker", "container", "ls", "-aq", "--filter", f"name=^/{self.container_name}$",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(inspect.communicate(), 10)
        if inspect.returncode != 0 or output.strip():
            raise RuntimeError("Runner cleanup unconfirmed; operator reconciliation required")

    async def execute(self) -> ExecutionRecord:
        async with self._lock:
            if self.record is not None:
                return self.record
            if self._dispatched:
                raise RuntimeError("Runner dispatch is unresolved; automatic redispatch denied")
            self._dispatched = True
            self.record = await self._execute()
            return self.record

    async def _execute(self) -> ExecutionRecord:
        if self.target != container_target(self.target.commit_sha, self.mode, self.image):
            raise ValueError("Runner source drift before dispatch")
        with TemporaryDirectory(prefix="job-", dir=self.scratch) as directory:
            root = Path(directory)
            output = root / "artifacts"
            output.mkdir(mode=0o777)
            output.chmod(0o777)  # Only this job-owned empty output mount is writable by container UID.
            log_path = root / "stdout.log"
            with log_path.open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    *self.command(output), stdout=log, stderr=asyncio.subprocess.STDOUT,
                )
                state: Literal["COMPLETED", "TIMED_OUT", "CANCELLED"] = "COMPLETED"
                try:
                    await asyncio.wait_for(process.wait(), 60)
                except (TimeoutError, asyncio.CancelledError) as error:
                    try:
                        await asyncio.shield(self._cleanup())
                    finally:
                        # Always reap the local Docker client, even if external cleanup is unconfirmed.
                        if process.returncode is None:
                            process.kill()
                        await process.wait()
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    state = "TIMED_OUT"
            # A container's file is an untrusted boundary. Reject symlink output before readback.
            junit = output / "junit.xml"
            if junit.is_symlink():
                raise ValueError("Runner artifact symlink denied")
            junit_hash = self.store.put(junit.read_bytes()) if junit.is_file() else None
            return ExecutionRecord(
                evidence_id=f"ev_{uuid4().hex}", task_id=self.task_id, run_id=f"run_{uuid4().hex}",
                operation_id=self.operation_id, target=self.target, state=state,
                exit_code=process.returncode, expected_cases=EXPECTED_CASES,
                junit_hash=junit_hash, log_hash=self.store.put(log_path.read_bytes()),
                observed_at=datetime.now(timezone.utc).isoformat(), source_system="isolated-container-fixture",
            )
