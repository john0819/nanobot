"""Immutable contracts for the first evidence-gated execution slice."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Target(Contract):
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    env_snapshot_id: str = Field(min_length=1)
    suite_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision_kind: Literal["WORKTREE_SNAPSHOT"] = "WORKTREE_SNAPSHOT"


class Counts(Contract):
    planned: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    skipped: int = Field(ge=0)
    not_run: int = Field(ge=0)

    @model_validator(mode="after")
    def conserved(self) -> "Counts":
        if self.planned != self.passed + self.failed + self.errors + self.skipped + self.not_run:
            raise ValueError("test counts do not conserve planned scope")
        return self

    @property
    def executed(self) -> int:
        return self.passed + self.failed + self.errors

    @property
    def pass_rate(self) -> float | None:
        return self.passed / self.planned if self.planned else None


class ExecutionRecord(Contract):
    """Created by the executor, never accepted from model/tool arguments."""

    evidence_id: str
    task_id: str
    run_id: str
    operation_id: str
    target: Target
    state: Literal["COMPLETED", "TIMED_OUT", "CANCELLED"]
    exit_code: int | None
    expected_cases: tuple[str, ...]
    junit_hash: str | None
    log_hash: str
    observed_at: str
    source_system: Literal["trusted-local-fixture", "isolated-container-fixture"] = "trusted-local-fixture"


class Claim(Contract):
    claim_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    type: Literal["TEST_EXECUTED", "TEST_COUNTS", "ALL_PASSED", "CASE_FAILED"]
    evidence_ids: tuple[str, ...]
    value: bool | Counts | str


class ReportCandidate(Contract):
    task_id: str
    run_id: str
    target: Target
    claims: tuple[Claim, ...] = Field(min_length=1, max_length=32)
