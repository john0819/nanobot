"""Fixed container contract plus opt-in real Docker execution and isolation probes."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from testpilot.artifacts import ArtifactStore
from testpilot.baseline import FORK_BASELINE_SHA
from testpilot.container_runner import ContainerFixtureExecutor, container_target
from testpilot.demo_provider import ScriptedProvider
from testpilot.nanobot_adapter import run_task

IMAGE = os.environ.get("TESTPILOT_RUNNER_IMAGE", "")


def test_container_contract_denies_mutable_image_and_dangerous_mounts(tmp_path):
    with pytest.raises(ValueError, match="immutable"):
        container_target(FORK_BASELINE_SHA, "healthy", "testpilot-runner:dev")
    image = "sha256:" + "a" * 64
    executor = ContainerFixtureExecutor(ArtifactStore(tmp_path / "store"), "task_contract",
                                        container_target(FORK_BASELINE_SHA, "healthy", image),
                                        "healthy", image, tmp_path / "jobs")
    command = executor.command(tmp_path / "job-output")
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--user") + 1] == "10001:10001"
    assert command.count("--mount") == 1
    assert "docker.sock" not in str(command)
    assert "--privileged" not in command


@pytest.mark.skipif(not IMAGE, reason="Set TESTPILOT_RUNNER_IMAGE to run actual isolated container jobs")
@pytest.mark.parametrize("mode,passed,failed", [("healthy", 4, 0), ("retry-write-bug", 3, 1)])
async def test_real_container_nanobot_round_trip(tmp_path, mode, passed, failed):
    executor = ContainerFixtureExecutor(ArtifactStore(tmp_path / "store"), "task_container",
                                        container_target(FORK_BASELINE_SHA, mode, IMAGE), mode, IMAGE,
                                        tmp_path / "jobs")
    provider = ScriptedProvider()
    result = await run_task(executor, provider, provider.get_default_model())
    assert result["report_validated"] is True, result["validation_gaps"]
    assert result["test_summary"]["passed"] == passed
    assert result["test_summary"]["failed"] == failed
    assert executor.record.source_system == "isolated-container-fixture"
    assert await executor.execute() is executor.record


@pytest.mark.skipif(not IMAGE, reason="Docker isolation probe requires an admitted image")
async def test_real_container_hardening(tmp_path):
    executor = ContainerFixtureExecutor(ArtifactStore(tmp_path / "store"), "task_probe",
                                        container_target(FORK_BASELINE_SHA, "healthy", IMAGE),
                                        "healthy", IMAGE, tmp_path / "jobs")
    output = tmp_path / "output"
    output.mkdir(mode=0o777)
    output.chmod(0o777)
    code = (
        'import os,json; from pathlib import Path; '
        'assert os.getuid()==10001; assert not Path("/var/run/docker.sock").exists(); '
        'status=Path("/proc/self/status").read_text(); '
        'assert "CapEff:\\t0000000000000000" in status; assert "NoNewPrivs:\\t1" in status; '
        'assert len(Path("/proc/net/route").read_text().splitlines())==1; '
        'assert os.statvfs("/").f_flag & os.ST_RDONLY; '
        'assert os.environ.get("DEEPSEEK_API_KEY") is None; '
        'print(json.dumps({"uid":os.getuid(),"network":"none","capabilities":"zero","no_new_privs":True}))'
    )
    command = executor.command(output)[:-1] + ["--entrypoint", "python", IMAGE, "-I", "-c", code]
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, stderr.decode()
    assert json.loads(stdout)["uid"] == 10001


@pytest.mark.skipif(not IMAGE, reason="Docker hash mismatch rejection requires an admitted image")
async def test_image_source_mismatch_cannot_publish_execution_success(tmp_path):
    executor = ContainerFixtureExecutor(ArtifactStore(tmp_path / "store"), "task_source_mismatch",
                                        container_target(FORK_BASELINE_SHA, "healthy", IMAGE),
                                        "healthy", IMAGE, tmp_path / "jobs")
    original = executor.command

    def wrong_hash(path: Path):
        return ["TESTPILOT_SUITE_HASH=" + "b" * 64 if arg.startswith("TESTPILOT_SUITE_HASH=") else arg
                for arg in original(path)]

    executor.command = wrong_hash
    record = await executor.execute()
    assert record.exit_code != 0
    assert record.junit_hash is None


@pytest.mark.skipif(not IMAGE, reason="Docker cancellation requires a running isolated container")
async def test_cancel_confirms_container_removed_and_denies_redispatch(tmp_path):
    executor = ContainerFixtureExecutor(ArtifactStore(tmp_path / "store"), "task_cancel",
                                        container_target(FORK_BASELINE_SHA, "healthy", IMAGE),
                                        "healthy", IMAGE, tmp_path / "jobs")
    original = executor.command

    def blocking(path: Path):
        return original(path)[:-1] + ["--entrypoint", "python", IMAGE, "-I", "-c",
                                      "import time; time.sleep(30)"]

    executor.command = blocking
    worker = asyncio.create_task(executor.execute())
    running = False
    for _ in range(50):
        inspect = await asyncio.create_subprocess_exec(
            "docker", "container", "ls", "-q", "--filter", f"name=^/{executor.container_name}$",
            stdout=asyncio.subprocess.PIPE,
        )
        output, _ = await inspect.communicate()
        if output.strip():
            running = True
            break
        await asyncio.sleep(0.1)
    if not running:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        pytest.fail("test container did not become running")
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    await executor._cleanup()  # Idempotent, absence is reconfirmed against the Docker server.
    with pytest.raises(RuntimeError, match="redispatch denied"):
        await executor.execute()
