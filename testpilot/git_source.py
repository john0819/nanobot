"""Operator-reviewed Git manifest. Read blobs at exact SHA, never check out/run code on host."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from threading import Timer
from typing import Literal

from pydantic import Field, model_validator

from testpilot.domain import Contract, Target


class GitManifest(Contract):
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: dict[str, str] = Field(min_length=1, max_length=64)
    expected_cases: tuple[str, ...] = Field(min_length=1, max_length=200)
    test_paths: tuple[str, ...] = Field(min_length=1, max_length=20)
    profile_version: str = "git-pytest-v1"

    @model_validator(mode="after")
    def paths_and_hashes(self) -> "GitManifest":
        for name, value in self.files.items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or ".git" in path.parts or str(path) != name or not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", name):
                raise ValueError("Manifest path denied")
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("Manifest content hash required")
        if len(set(self.expected_cases)) != len(self.expected_cases) or any(path not in self.files for path in self.test_paths) or any("::" not in case for case in self.expected_cases):
            raise ValueError("Manifest test/case scope invalid")
        if self.profile_version != "git-pytest-v1":
            raise ValueError("Unknown execution profile")
        return self

    def suite_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def target(self, image: str, mode: str, tenant: str = "novax-demo", project: str = "gateway-fixture") -> Target:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Immutable image required")
        return Target(tenant_id=tenant, project_id=project, commit_sha=self.commit_sha,
                      env_snapshot_id=f"git-pytest-v1:{image}:{mode}", suite_hash=self.suite_hash(), source_revision_kind="GIT_COMMIT")


class GitSource:
    def __init__(self, repository: Path, manifest: GitManifest) -> None:
        if repository.is_symlink():
            raise ValueError("Repository alias denied")
        self.repository, self.manifest = repository.resolve(strict=True), manifest

    def git(self, *arguments: str, limit: int = 4*1024*1024) -> bytes:
        env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"}
        with subprocess.Popen(["git", "--no-replace-objects", "-C", str(self.repository), *arguments], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            assert process.stdout is not None
            timer = Timer(5, process.kill)
            timer.start()
            try:
                data = process.stdout.read(limit+1)
            finally:
                timer.cancel()
            if len(data) > limit:
                process.kill()
                raise ValueError("Git object exceeds bound")
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                raise ValueError("Git operation deadline") from None
            if code:
                raise ValueError("Approved Git object unavailable")
            return data

    def read(self, path: str) -> bytes:
        if path not in self.manifest.files:
            raise ValueError("File outside approved manifest")
        tree = self.git("ls-tree", self.manifest.commit_sha, "--", path).decode().split()
        if len(tree) < 4 or tree[0] not in {"100644", "100755"} or tree[1] != "blob":
            raise ValueError("Symlink/submodule/non-file Git object denied")
        size = int(self.git("cat-file", "-s", self.manifest.commit_sha+":"+path))
        if size > 4*1024*1024:
            raise ValueError("Git blob exceeds bound")
        content = self.git("cat-file", "blob", self.manifest.commit_sha+":"+path)
        if hashlib.sha256(content).hexdigest() != self.manifest.files[path]:
            raise ValueError("Git source differs from reviewed content")
        return content

    def export(self, destination: Path) -> None:
        root = destination.resolve()
        root.mkdir(parents=True, exist_ok=True)
        total = 0
        for path in self.manifest.files:
            output = root/path
            if output.is_symlink() or not output.resolve().is_relative_to(root):
                raise ValueError("Export escaped workspace")
            content = self.read(path)
            total += len(content)
            if total > 16*1024*1024:
                raise ValueError("Manifest total bytes exceeded")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
            output.chmod(0o444)


class GitProfile:
    source_system: Literal["isolated-container-fixture", "isolated-git-runner"] = "isolated-git-runner"

    def __init__(self, source: GitSource, target: Target, mode: str, image: str, scratch: Path) -> None:
        if target != source.manifest.target(image, mode, target.tenant_id, target.project_id):
            raise ValueError("Git target differs from reviewed manifest")
        self.source, self.image, self.mode = source, image, mode
        self.scratch = scratch.resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.expected_cases = source.manifest.expected_cases
        self.operation_id = ""
        self.container_name = ""

    def command(self, output: Path) -> list[str]:
        checkout = output.parent/"source"
        if checkout.is_symlink() or not checkout.resolve().is_relative_to(self.scratch):
            raise ValueError("Git export outside Runner scratch")
        self.source.export(checkout)
        # Only the exported read-only blobs and per-operation output cross the container boundary.
        return ["docker", "run", "--rm", "--name", self.container_name, "--label", "testpilot.operation="+self.operation_id,
                "--user", "10001:10001", "--read-only", "--network", "none", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--cpus", "1", "--memory", "256m", "--pids-limit", "64",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--mount", f"type=bind,src={checkout},dst=/source,readonly",
                "--mount", f"type=bind,src={output},dst=/artifacts", "--env", "TESTPILOT_FIXTURE_MODE="+self.mode,
                "--env", "TESTPILOT_SUITE_HASH="+self.source.manifest.target(self.image, self.mode).suite_hash,
                "--entrypoint", "python", self.image, "-I", "-c",
                "import os,sys,pytest;os.chdir('/source');sys.path.insert(0,'/source');"
                "raise SystemExit(pytest.main(['-q','--tb=short','-p','no:cacheprovider','--junitxml=/artifacts/junit.xml','--']+sys.argv[1:]))",
                *self.source.manifest.test_paths]
