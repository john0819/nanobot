"""Explicit revalidation contract: never enlarge task budgets or overwrite a prior run."""

from pydantic import Field

from testpilot.domain import Contract


class RerunRequest(Contract):
    expected_state_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


class MemoryCandidateRequest(Contract):
    source_run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    case_id: str = Field(min_length=1, max_length=300)
    shared: bool = False


class MemoryDecision(Contract):
    expected_version: int = Field(ge=1)
    decision: str = Field(pattern=r"^(CONFIRM|REVOKE)$")
