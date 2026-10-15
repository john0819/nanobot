"""Read-only GitLab evidence adapter. CI metadata never substitutes for current execution."""

import json
import re

from pydantic import BaseModel, Field, TypeAdapter

from testpilot.git_source import GitManifest
from testpilot.http_transport import SafeHTTP


class Commit(BaseModel):
    id: str
    parent_ids: list[str]
    title: str


class Diff(BaseModel):
    old_path: str
    new_path: str
    diff: str = ""
    new_file: bool = False
    deleted_file: bool = False
    renamed_file: bool = False
    collapsed: bool = False
    too_large: bool = False


class JobCommit(BaseModel):
    id: str


class CIJob(BaseModel):
    id: int = Field(ge=1)
    status: str
    name: str
    commit: JobCommit
    pipeline: dict[str, object]


class GitLabAdapter:
    def __init__(self, endpoint: str, project_id: int, token: str, manifest: GitManifest,
                 http: SafeHTTP, job_id: int | None = None) -> None:
        if project_id < 1 or not token or not re.fullmatch(r"https?://[^?#]+", endpoint):
            raise ValueError("Invalid GitLab operator configuration")
        self.base = endpoint.rstrip("/")+f"/api/v4/projects/{project_id}"
        self.headers = {"PRIVATE-TOKEN": token}
        self.manifest, self.http, self.job_id = manifest, http, job_id

    async def change(self) -> dict[str, object]:
        path = self.base+"/repository/commits/"+self.manifest.commit_sha
        commit = Commit.model_validate(await self.http.json("GET", path, headers=self.headers))
        if commit.id != self.manifest.commit_sha:
            raise ValueError("GitLab commit snapshot mismatch")
        raw = await self.http.request("GET", path+"/diff?per_page=100&page=1", headers=self.headers)
        diffs = TypeAdapter(list[Diff]).validate_json(raw)
        # Server diffs may be truncated even with fewer than 100 rows. Never imply full coverage.
        return {"commit": commit.model_dump(), "diffs": [item.model_dump() for item in diffs],
                "coverage_complete": False, "limitation": "Single bounded API page; GitLab diff limits may omit content.",
                "trust_level": "UNTRUSTED_GITLAB_CONTENT", "current_execution_evidence": False}

    async def ci_result(self) -> dict[str, object]:
        if self.job_id is None:
            raise ValueError("No operator-admitted CI Job")
        job = CIJob.model_validate(await self.http.json("GET", self.base+f"/jobs/{self.job_id}", headers=self.headers))
        if job.id != self.job_id or job.commit.id != self.manifest.commit_sha:
            raise ValueError("GitLab CI target mismatch")
        return {"job": job.model_dump(), "current_execution_evidence": False,
                "limitation": "Read-only historical CI metadata; PASS requires admitted manifest/JUnit/hash validation."}

    @staticmethod
    def visible(body: dict[str, object], ref: str) -> str:
        text = json.dumps(body, ensure_ascii=False)
        return json.dumps({"artifact_hash": ref, "content": text[:8000], "truncated": len(text) > 8000,
                           "trust_level": "UNTRUSTED_GITLAB_CONTENT", "current_execution_evidence": False})
