"""Scoped S3 artifact storage. Local cache is not the publication source of truth."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

from nanobot.security.network import resolve_url_target
from testpilot.artifacts import MAX_ARTIFACT_BYTES, ArtifactStore, ArtifactUnavailableError, digest


class S3ArtifactStore(ArtifactStore):
    def __init__(self, root: Path, client: S3Client, bucket: str, tenant_id: str, task_id: str) -> None:
        super().__init__(root)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant_id) or not re.fullmatch(r"task_[0-9a-f]{32}", task_id):
            raise ValueError("invalid S3 artifact scope")
        self.client, self.bucket = client, bucket
        self.prefix = f"{tenant_id}/gateway-fixture/{task_id}/"

    def key(self, content_hash: str) -> str:
        self._path(content_hash)  # Shared digest/path validation, including rejecting local symlink aliases.
        return self.prefix + content_hash

    def put(self, content: bytes) -> str:
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds size limit")
        content_hash = digest(content)
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.key(content_hash), Body=content,
                                   Metadata={"sha256": content_hash}, IfNoneMatch="*")
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in {409, 412}:
                raise ArtifactUnavailableError("Object store write unavailable") from None
            if self.read(content_hash) != content:
                raise ValueError("remote artifact collision/corruption") from None
        except BotoCoreError:
            raise ArtifactUnavailableError("Object store write unavailable") from None
        # A successful upload must be readable before any PG evidence registration.
        if self.read(content_hash) != content:
            raise ValueError("remote artifact readback mismatch")
        return content_hash

    def read(self, content_hash: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(content_hash))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "NoSuchKey":
                raise FileNotFoundError("Artifact not in object store") from None
            raise ArtifactUnavailableError("Object store read unavailable") from None
        except BotoCoreError:
            raise ArtifactUnavailableError("Object store read unavailable") from None
        body = response["Body"]
        try:
            content = body.read(MAX_ARTIFACT_BYTES+1)
        except (BotoCoreError, OSError):
            raise ArtifactUnavailableError("Object store read interrupted") from None
        finally:
            body.close()
        if len(content) > MAX_ARTIFACT_BYTES or digest(content) != content_hash:
            raise ValueError("remote artifact integrity check failed")
        ArtifactStore.put(self, content)  # Atomic durable local projection, never a remote outage fallback.
        return content


def s3_client(endpoint: str, access_key: str, secret_key: str, region: str = "garage",
              development: bool = False) -> S3Client:
    allowed, reason, _ = resolve_url_target(endpoint, allow_loopback=development)
    if not allowed:
        raise ValueError(f"S3 endpoint denied: {reason}")
    # Installed S3 overload is concrete; unrelated optional AWS stub overloads remain unknown.
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key,  # pyright: ignore[reportUnknownMemberType]
                        aws_secret_access_key=secret_key, region_name=region,
                        config=Config(signature_version="s3v4", connect_timeout=3, read_timeout=5, proxies={},
                                      request_checksum_calculation="when_required", response_checksum_validation="when_required",
                                      retries={"max_attempts": 0}, s3={"addressing_style": "path"}))
