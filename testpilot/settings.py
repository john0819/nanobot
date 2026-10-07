"""Construct scoped extension adapters from explicit, validated operator configuration."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from nanobot.config.schema import TestPilotConfig
from testpilot.identity import IdentityProvider, JWTIdentity
from testpilot.knowledge import MCPKnowledgeClient
from testpilot.object_store import S3ArtifactStore, s3_client
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
