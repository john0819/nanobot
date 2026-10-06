"""Development executor for the reviewed fixture only, not a production sandbox."""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import uuid4

from testpilot.artifacts import ArtifactStore, digest
from testpilot.domain import ExecutionRecord, Target

FixtureMode = Literal["healthy", "retry-write-bug"]
CASE_NAMES = ("test_read_retry", "test_write_no_retry", "test_route_priority", "test_rate_limit")
EXPECTED_CASES = tuple(f"fixed_fixture.test_gateway::{name}" for name in CASE_NAMES)


def fixture_sources() -> tuple[bytes, bytes]:
    root = Path(__file__).parent / "fixtures"
    return (root.joinpath("gateway.py").read_bytes(), root.joinpath("suite.py").read_bytes())


def fixture_target(commit_sha: str, mode: FixtureMode) -> Target:
    source, suite = fixture_sources()
    return Target(
        tenant_id="novax-demo", project_id="gateway-fixture", commit_sha=commit_sha,
        env_snapshot_id=f"trusted-local-fixture:{mode}:python-{sys.version_info.major}.{sys.version_info.minor}",
        suite_hash=digest(source + b"\0" + suite),
    )


class FixedFixtureExecutor:
    """One operation per instance; repeated calls replay the same result, no implicit rerun.

    Only reviewed source bundled with this package is executed. No user paths, shell,
    flags, generated source, credentials or external targets are accepted.
    Persistence and cross-process recovery are intentionally not provided yet.
    """

    def __init__(self, store: ArtifactStore, task_id: str, target: Target, mode: FixtureMode) -> None:
        if mode not in {"healthy", "retry-write-bug"}:
            raise ValueError("unknown fixture mode")
        if target != fixture_target(target.commit_sha, mode):
            raise ValueError("target does not match reviewed fixture")
        self.store = store
        self.task_id = task_id
        self.target = target
        self.mode = mode
        self.operation_id = f"op_{uuid4().hex}"
        self.record: ExecutionRecord | None = None
        self._lock = asyncio.Lock()

    async def execute(self) -> ExecutionRecord:
        async with self._lock:
            if self.record is None:
                self.record = await self._execute()
            return self.record

    async def _execute(self) -> ExecutionRecord:
        source, suite = fixture_sources()
        if digest(source + b"\0" + suite) != self.target.suite_hash:
            raise ValueError("fixture changed after admission")
        with TemporaryDirectory(prefix="testpilot-fixture-") as directory:
            root = Path(directory)
            package = root / "fixed_fixture"
            package.mkdir()
            package.joinpath("__init__.py").write_bytes(b"")
            package.joinpath("gateway.py").write_bytes(source)
            package.joinpath("test_gateway.py").write_bytes(suite)
            # Do not inherit API keys, PYTHONPATH, or pytest plugin configuration.
            env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH") if key in os.environ}
            env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", TESTPILOT_FIXTURE_MODE=self.mode)
            log_path = root / "pytest.log"
            with log_path.open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    "--rootdir", str(root), "--confcutdir", str(root),
                    "--junitxml", str(root / "junit.xml"),
                    str(package / "test_gateway.py"), cwd=root, env=env,
                    stdout=log, stderr=asyncio.subprocess.STDOUT,
                )
                state: Literal["COMPLETED", "TIMED_OUT", "CANCELLED"] = "COMPLETED"
                try:
                    await asyncio.wait_for(process.wait(), timeout=30)
                except TimeoutError:
                    state = "TIMED_OUT"
                    process.kill()
                    await process.wait()
                except asyncio.CancelledError:
                    process.kill()
                    await process.wait()
                    raise
            log_hash = self.store.put(log_path.read_bytes())
            junit_path = root / "junit.xml"
            junit_hash = self.store.put(junit_path.read_bytes()) if junit_path.exists() else None
            return ExecutionRecord(
                evidence_id=f"ev_{uuid4().hex}", task_id=self.task_id, run_id=f"run_{uuid4().hex}",
                operation_id=self.operation_id, target=self.target, state=state,
                exit_code=process.returncode, expected_cases=EXPECTED_CASES,
                junit_hash=junit_hash, log_hash=log_hash,
                observed_at=datetime.now(timezone.utc).isoformat(),
            )
