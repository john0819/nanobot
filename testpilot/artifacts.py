"""Content-addressed local artifacts; readback verifies bytes every time."""

import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4

MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, content_hash: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise ValueError("invalid artifact hash")
        path = self.root / content_hash
        if path.is_symlink():
            raise ValueError("artifact symlinks are forbidden")
        return path

    def put(self, content: bytes) -> str:
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds size limit")
        content_hash = digest(content)
        path = self._path(content_hash)
        temporary = self.root / (".pending-" + uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)  # Publish complete bytes atomically without replacing immutable content.
            except FileExistsError:
                if self.read(content_hash) != content:
                    raise ValueError("artifact collision or corruption") from None
            if os.name != "nt":
                descriptor = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        return content_hash

    def read(self, content_hash: str) -> bytes:
        with self._path(content_hash).open("rb") as stream:
            content = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(content) > MAX_ARTIFACT_BYTES or digest(content) != content_hash:
            raise ValueError("artifact hash mismatch or size limit exceeded")
        return content
