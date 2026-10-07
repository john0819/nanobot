"""nanobot testpilot serve: local task service backed by isolated Runner jobs."""

import os
from pathlib import Path

import typer

from testpilot.artifacts import ArtifactStore
from testpilot.baseline import FORK_BASELINE_SHA
from testpilot.container_runner import ContainerFixtureExecutor, container_target
from testpilot.demo_provider import ScriptedProvider
from testpilot.execution import FixtureMode
from testpilot.executor_contract import FixtureExecutor
from testpilot.nanobot_adapter import run_task
from testpilot.runtime_contracts import RunControls

app = typer.Typer(help="TestPilot local development task service (fixed fixture only)")


@app.command()
def serve(
    runner_image: str = typer.Option(..., help="Immutable local Docker image ID sha256:..."),
    port: int = typer.Option(8920, min=1, max=65535),
    output: Path = typer.Option(Path(".local/testpilot-api")),
    config: Path | None = typer.Option(None, help="Use a configured real nanobot model; default scripted contract provider"),
    durable: bool = typer.Option(False, help="Use PostgreSQL ledger and retain Jobs for crash recovery"),
    lease_seconds: int = typer.Option(30, min=1, max=300, help="Durable Worker lease duration; heartbeat every third"),
) -> None:
    try:
        from aiohttp import web

        from testpilot.task_api import Principal, create_app
    except ImportError:
        raise typer.BadParameter("Install nanobot-ai[api] to run the local Task API") from None
    token = os.environ.get("TESTPILOT_API_TOKEN", "")
    if len(token) < 32:
        raise typer.BadParameter("Set TESTPILOT_API_TOKEN to a random token of at least 32 characters")
    # Fail startup for mutable tags before accepting tasks.
    container_target(FORK_BASELINE_SHA, "healthy", runner_image)
    if config is not None:
        if not config.is_file():
            raise typer.BadParameter("Configured model file does not exist")
        from nanobot.providers.factory import load_provider_snapshot

        load_provider_snapshot(config)  # Validate credentials/settings before admitting any task.
    output = output.resolve()

    def executor(store: ArtifactStore, task_id: str, mode: FixtureMode) -> FixtureExecutor:
        return ContainerFixtureExecutor(store, task_id, container_target(FORK_BASELINE_SHA, mode, runner_image),
                                        mode, runner_image, output / "jobs")

    async def run(execution: FixtureExecutor, controls: RunControls | None = None) -> dict[str, object]:
        if config is None:
            provider = ScriptedProvider()
            report = await run_task(execution, provider, provider.get_default_model(), controls=controls)
            report["evaluation_kind"] = "scripted-provider-real-container-pytest"
            return report
        # Use nanobot's own provider configuration path, no secret logging or duplicated model client.
        from nanobot.providers.factory import load_provider_snapshot

        snapshot = load_provider_snapshot(config)
        report = await run_task(execution, snapshot.provider, snapshot.model, controls=controls)
        report["evaluation_kind"] = "configured-model-real-container-pytest"
        return report

    if durable:
        from testpilot.durable_api import create_app as create_durable_app
        from testpilot.storage.postgres import Ledger

        dsn = os.environ.get("TESTPILOT_DATABASE_URL", "")
        if not dsn:
            raise typer.BadParameter("Set TESTPILOT_DATABASE_URL for the durable profile")
        service = create_durable_app(tokens={token: Principal("local-reviewer")}, root=output,
                                     ledger=Ledger(dsn), image=runner_image, run=run, lease_seconds=lease_seconds,
                                     target=lambda mode: container_target(FORK_BASELINE_SHA, mode, runner_image))
    else:
        service = create_app(tokens={token: Principal("local-reviewer")}, root=output,
                             executor_factory=executor, run=run)
    typer.echo(f"TestPilot local Task API: http://127.0.0.1:{port}; model={'configured' if config else 'scripted'}")
    web.run_app(service, host="127.0.0.1", port=port, print=None, access_log=None)


@app.command()
def migrate() -> None:
    """Apply checksum-locked TestPilot SQL migrations to the configured database."""
    import asyncio

    from testpilot.storage.postgres import Ledger

    dsn = os.environ.get("TESTPILOT_DATABASE_URL", "")
    if not dsn:
        raise typer.BadParameter("Set TESTPILOT_DATABASE_URL")
    asyncio.run(Ledger(dsn).migrate())
    typer.echo("TestPilot migration applied")
