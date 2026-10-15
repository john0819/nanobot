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


@app.command(name="verify-artifacts")
def verify_artifacts(task_id: str, actor: str, output: Path = typer.Option(Path(".local/testpilot-api")),
                     settings: Path | None = typer.Option(None)) -> None:
    """Operator-only local command: verify registered private/public artifact hashes after restore."""
    import asyncio
    import json

    from nanobot.config.loader import load_config
    from nanobot.config.schema import TestPilotConfig
    from testpilot.maintenance import verify_inventory
    from testpilot.settings import artifact_factory
    from testpilot.storage.postgres import Ledger

    dsn = os.environ.get("TESTPILOT_DATABASE_URL", "")
    if not dsn:
        raise typer.BadParameter("Set TESTPILOT_DATABASE_URL")

    async def check() -> dict[str, object]:
        extension = load_config(settings).testpilot if settings else TestPilotConfig()
        ledger = Ledger(dsn, extension.tenant_id)
        await ledger.ready()
        task = await ledger.get(task_id, actor)
        if task is None:
            raise typer.BadParameter("Task/actor scope not found")
        return await verify_inventory(ledger, task, output.resolve(), artifact_factory(extension))

    result = asyncio.run(check())
    typer.echo(json.dumps(result))
    if not result["integrity_ok"]:
        raise typer.Exit(2)


@app.command()
def serve(
    runner_image: str = typer.Option(..., help="Immutable local Docker image ID sha256:..."),
    port: int = typer.Option(8920, min=1, max=65535),
    output: Path = typer.Option(Path(".local/testpilot-api")),
    config: Path | None = typer.Option(None, help="Use a configured real nanobot model; default scripted contract provider"),
    durable: bool = typer.Option(False, help="Use PostgreSQL ledger and retain Jobs for crash recovery"),
    lease_seconds: int = typer.Option(30, min=1, max=300, help="Durable Worker lease duration; heartbeat every third"),
    job_poll_seconds: float = typer.Option(5, min=0.01, max=30, help="Scheduler base poll delay; grows to 3x/6x, capped at 30s"),
    workers: int = typer.Option(2, min=1, max=4),
    scheduler: bool = typer.Option(True, "--scheduler/--no-scheduler", help="Run the Job scheduler in this process"),
    settings: Path | None = typer.Option(None, help="Nanobot config containing testpilot identity/storage/knowledge settings; independent of model"),
) -> None:
    try:
        from aiohttp import web

        from testpilot.task_api import Principal, create_app
    except ImportError:
        raise typer.BadParameter("Install nanobot-ai[api] to run the local Task API") from None
    from dataclasses import replace

    from nanobot.config.loader import load_config
    from nanobot.config.schema import TestPilotConfig
    from testpilot.git_source import GitManifest, GitSource
    from testpilot.gitlab import GitLabAdapter
    from testpilot.http_transport import SafeHTTP
    from testpilot.settings import (
        artifact_factory,
        identity_provider,
        knowledge_client,
        runner_factory,
        target_factory,
    )

    extension = load_config(settings or config).testpilot if settings or config else TestPilotConfig()
    identity = identity_provider(extension)
    token = os.environ.get("TESTPILOT_API_TOKEN", "")
    if identity is None and len(token) < 32:
        raise typer.BadParameter("Set TESTPILOT_API_TOKEN to a random token of at least 32 characters")
    if not durable and (identity or extension.s3_endpoint or extension.knowledge_endpoint or extension.require_approval or extension.git_manifest or extension.runner_endpoint):
        raise typer.BadParameter("Identity/storage/approval/knowledge adapters require --durable")
    # Fail startup for mutable tags before accepting tasks.
    container_target(FORK_BASELINE_SHA, "healthy", runner_image)
    if config is not None:
        if not config.is_file():
            raise typer.BadParameter("Configured model file does not exist")
        from nanobot.providers.factory import load_provider_snapshot

        load_provider_snapshot(config)  # Validate credentials/settings before admitting any task.
    output = output.resolve()
    source = GitSource(Path(extension.git_repository), GitManifest.model_validate_json(Path(extension.git_manifest).read_bytes())) if extension.git_manifest and extension.git_repository else None
    gitlab = GitLabAdapter(extension.gitlab_endpoint, extension.gitlab_project_id, extension.gitlab_token.get_secret_value(),
        GitManifest.model_validate_json(Path(extension.git_manifest).read_bytes()), SafeHTTP(extension.development), extension.gitlab_job_id) if extension.gitlab_endpoint and extension.gitlab_project_id and extension.git_manifest else None

    def executor(store: ArtifactStore, task_id: str, mode: FixtureMode) -> FixtureExecutor:
        return ContainerFixtureExecutor(store, task_id, container_target(FORK_BASELINE_SHA, mode, runner_image),
                                        mode, runner_image, output / "jobs")

    async def run(execution: FixtureExecutor, controls: RunControls | None = None) -> dict[str, object]:
        if controls:
            controls = replace(controls, git_source=source, gitlab=gitlab)
        if config is None:
            provider = ScriptedProvider()
            report = await run_task(execution, provider, provider.get_default_model(), controls=controls)
            report["evaluation_kind"] = "scripted-provider-real-container-pytest"
            return report
        # Use nanobot's own provider configuration path, no secret logging or duplicated model client.
        from nanobot.providers.factory import load_provider_snapshot
        from nanobot.utils.llm_runtime import runtime_from_provider_snapshot

        snapshot = load_provider_snapshot(config)
        report = await run_task(execution, snapshot.provider, snapshot.model, controls=controls,
                                runtime=runtime_from_provider_snapshot(snapshot))
        report["evaluation_kind"] = "configured-model-real-container-pytest"
        return report

    if durable:
        from testpilot.durable_api import create_app as create_durable_app
        from testpilot.storage.postgres import Ledger

        dsn = os.environ.get("TESTPILOT_DATABASE_URL", "")
        if not dsn:
            raise typer.BadParameter("Set TESTPILOT_DATABASE_URL for the durable profile")
        ledger = Ledger(dsn, extension.tenant_id)
        service = create_durable_app(tokens={} if identity else {token: Principal("local-reviewer", extension.tenant_id)}, root=output,
                                     ledger=ledger, image=runner_image, run=run, lease_seconds=lease_seconds, poll_delay=job_poll_seconds,
                                     workers=workers, start_scheduler=scheduler,
                                     artifacts=artifact_factory(extension), identity=identity,
                                     require_approval=extension.require_approval, model_limit=extension.model_limit,
                                     knowledge=knowledge_client(extension, ledger.tenant_id),
                                     target=target_factory(extension, runner_image), runner=runner_factory(extension))
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


@app.command(name="scheduler")
def scheduler_service(
    runner_image: str = typer.Option(...), output: Path = typer.Option(Path(".local/testpilot-api")),
    lease_seconds: int = typer.Option(30, min=1, max=300),
    job_poll_seconds: float = typer.Option(5, min=0.01, max=30),
    settings: Path | None = typer.Option(None, help="Same testpilot artifact settings as the API Worker"),
) -> None:
    """Run the external Job scheduler separately; no model credentials or calls."""
    import asyncio
    import signal

    from nanobot.config.loader import load_config
    from nanobot.config.schema import TestPilotConfig
    from testpilot.scheduler import JobScheduler
    from testpilot.settings import artifact_factory, runner_factory
    from testpilot.storage.postgres import Ledger

    dsn = os.environ.get("TESTPILOT_DATABASE_URL", "")
    if not dsn:
        raise typer.BadParameter("Set TESTPILOT_DATABASE_URL")
    container_target(FORK_BASELINE_SHA, "healthy", runner_image)

    async def run() -> None:
        extension = load_config(settings).testpilot if settings else TestPilotConfig()
        service = JobScheduler(Ledger(dsn, extension.tenant_id), output, runner_image, lease_seconds, job_poll_seconds, artifact_factory(extension), runner_factory(extension))
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        if os.name != "nt":
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stopped.set)
        await service.start()
        try:
            await stopped.wait()
        finally:
            await service.stop()

    typer.echo("TestPilot Job Scheduler started (no model calls)")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


@app.command(name="runner")
def runner_service(runner_image: str = typer.Option(...), settings: Path | None = typer.Option(None),
                   output: Path = typer.Option(Path(".local/testpilot-runner")), port: int = typer.Option(8930)) -> None:
    """Independent persistent Runner process. Only this process needs Docker authority."""
    from aiohttp import web

    from nanobot.config.loader import load_config
    from nanobot.config.schema import TestPilotConfig
    from testpilot.runner_service import create_runner_app
    from testpilot.settings import artifact_factory, runner_factory
    from testpilot.storage.postgres import Ledger

    extension = load_config(settings).testpilot if settings else TestPilotConfig()
    token = extension.runner_service_token.get_secret_value() or os.environ.get("TESTPILOT_RUNNER_TOKEN", "")
    dsn = os.environ.get("TESTPILOT_DATABASE_URL", "")
    if not dsn:
        raise typer.BadParameter("Set TESTPILOT_DATABASE_URL")
    service = create_runner_app(Ledger(dsn, extension.tenant_id), output, runner_image, token,
                                runner=runner_factory(extension, remote=False), artifacts=artifact_factory(extension), max_jobs=extension.runner_max_jobs)
    web.run_app(service, host="127.0.0.1", port=port, print=None, access_log=None)
