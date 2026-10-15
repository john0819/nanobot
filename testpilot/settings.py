"""Construct scoped extension adapters from explicit, validated operator configuration."""

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import TypeAdapter

from nanobot.config.schema import TestPilotConfig
from testpilot.artifacts import ArtifactStore
from testpilot.baseline import FORK_BASELINE_SHA
from testpilot.container_runner import container_target
from testpilot.domain import Target
from testpilot.execution import FixtureMode
from testpilot.git_source import GitManifest, GitProfile, GitSource
from testpilot.http_transport import SafeHTTP
from testpilot.identity import IdentityProvider, JWTIdentity
from testpilot.knowledge import MCPKnowledgeClient
from testpilot.object_store import S3ArtifactStore, s3_client
from testpilot.oidc import OIDCIdentity
from testpilot.recoverable_runner import RetainedRunner
from testpilot.remote_runner import RemoteRunner
from testpilot.runner_contracts import RunnerBackend, RunnerFactory
from testpilot.storage.postgres import OperationRow, TaskRow
from testpilot.storage_contracts import ArtifactFactory, local_artifacts


def artifact_factory(settings: TestPilotConfig) -> ArtifactFactory:
    if settings.s3_endpoint is None:
        return local_artifacts
    client = s3_client(settings.s3_endpoint, settings.s3_access_key.get_secret_value(),
                       settings.s3_secret_key.get_secret_value(), settings.s3_region, settings.development)

    def scoped(root: Path, tenant_id: str, task_id: str) -> S3ArtifactStore:
        return S3ArtifactStore(root / tenant_id / task_id / "artifacts", client, settings.s3_bucket, tenant_id, task_id)

    return scoped


def identity_provider(settings: TestPilotConfig) -> IdentityProvider | None:
    if settings.oidc_discovery and settings.jwt_issuer and settings.jwt_audience:
        return OIDCIdentity(settings.jwt_issuer, settings.jwt_audience, SafeHTTP(settings.development), ttl=settings.oidc_jwks_ttl,
                            introspection_url=settings.oidc_introspection_url, client_id=settings.oidc_client_id,
                            client_secret=settings.oidc_client_secret.get_secret_value())
    if not settings.jwt_issuer or not settings.jwt_audience or not settings.jwks_file:
        return None
    body = Path(settings.jwks_file).read_bytes()
    if len(body) > 65536:
        raise ValueError("JWKS exceeds configured size bound")
    keys = TypeAdapter(dict[str, object]).validate_python(json.loads(body))
    return JWTIdentity(settings.jwt_issuer, settings.jwt_audience, keys)


def knowledge_client(settings: TestPilotConfig, tenant_id: str) -> MCPKnowledgeClient | None:
    if settings.knowledge_endpoint is None:
        return None
    return MCPKnowledgeClient(settings.knowledge_endpoint, tenant_id,
                              {actor: token.get_secret_value() for actor, token in settings.knowledge_actor_tokens.items()},
                              settings.development)


def target_factory(settings: TestPilotConfig, image: str) -> Callable[[FixtureMode], Target]:
    if settings.git_manifest:
        manifest = GitManifest.model_validate_json(Path(settings.git_manifest).read_bytes())
        return lambda mode: manifest.target(image, mode, settings.tenant_id)
    return lambda mode: container_target(FORK_BASELINE_SHA, mode, image, settings.tenant_id)


def runner_factory(settings: TestPilotConfig, remote: bool = True) -> RunnerFactory:
    if remote and settings.runner_endpoint:
        endpoint, token = settings.runner_endpoint, settings.runner_service_token.get_secret_value()
        def remote_factory(task: TaskRow, operation: OperationRow, store: ArtifactStore, image: str, scratch: Path) -> RunnerBackend:
            return RemoteRunner(task, operation, store, endpoint, token, SafeHTTP(settings.development))
        return remote_factory
    if settings.git_manifest:
        if settings.git_repository is None:
            raise ValueError("Git Runner requires operator-approved repository path")
        source = GitSource(Path(settings.git_repository), GitManifest.model_validate_json(Path(settings.git_manifest).read_bytes()))
        def git_factory(task: TaskRow, operation: OperationRow, store: ArtifactStore, image: str, scratch: Path) -> RunnerBackend:
            return RetainedRunner(task, operation, store, image, scratch, GitProfile(source, task.target, task.mode, image, scratch))
        return git_factory
    return RetainedRunner
