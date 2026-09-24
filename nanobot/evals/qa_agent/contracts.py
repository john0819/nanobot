"""Typed contracts for QA Agent datasets, recorded traces, and reports."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ConversationTurn(BaseModel):
    """One input turn in an evaluation case."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ToolExpectation(BaseModel):
    """Expected search-tool behavior."""

    search_required: bool
    min_calls: int = Field(default=0, ge=0)
    max_calls: int = Field(default=1, ge=0)
    required_query_terms: list[str] = Field(default_factory=list)
    required_filters: dict[str, list[str]] = Field(default_factory=dict)
    forbidden_filters: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_call_range(self) -> "ToolExpectation":
        if self.min_calls > self.max_calls:
            raise ValueError("min_calls cannot exceed max_calls")
        if self.search_required and self.max_calls == 0:
            raise ValueError("search_required cannot use max_calls=0")
        return self


class RetrievalExpectation(BaseModel):
    """Expected evidence returned by the knowledge service."""

    required_document_ids: list[str] = Field(default_factory=list)
    insufficient_evidence: bool | None = None
    allowed_degraded: list[str] = Field(default_factory=list)


class AnswerExpectation(BaseModel):
    """Deterministic checks over the final answer."""

    citations_required: bool = False
    required_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)
    refusal_terms: list[str] = Field(default_factory=list)


class SafetyExpectation(BaseModel):
    """Hard safety checks for tools used during the turn."""

    search_only: bool = True


class CaseExpectation(BaseModel):
    """All deterministic expectations for one case."""

    tool: ToolExpectation
    retrieval: RetrievalExpectation = Field(default_factory=RetrievalExpectation)
    answer: AnswerExpectation = Field(default_factory=AnswerExpectation)
    safety: SafetyExpectation = Field(default_factory=SafetyExpectation)
    max_latency_ms: int | None = Field(default=None, gt=0)


class EvalCase(BaseModel):
    """One frozen QA Agent scenario."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    category: str = Field(min_length=1)
    description: str = ""
    turns: list[ConversationTurn] = Field(min_length=1)
    expect: CaseExpectation

    @model_validator(mode="after")
    def require_user_turn(self) -> "EvalCase":
        if not any(turn.role == "user" for turn in self.turns):
            raise ValueError("an evaluation case requires at least one user turn")
        return self


class EvalDataset(BaseModel):
    """Versioned collection of QA Agent scenarios."""

    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_case_ids(self) -> "EvalDataset":
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case ids must be unique")
        return self


class ToolCallTrace(BaseModel):
    """One tool call observed during an Agent turn."""

    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class EvidenceTrace(BaseModel):
    """Search evidence exposed to the answer model."""

    citation_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    insufficient_evidence: bool = False
    degraded: list[str] = Field(default_factory=list)
    request_ids: list[str] = Field(default_factory=list)
    generation_ids: list[str] = Field(default_factory=list)


class UsageTrace(BaseModel):
    """Optional model usage and estimated cost."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0)


class AgentTrace(BaseModel):
    """Recorded Agent execution consumed by deterministic evaluation."""

    schema_version: Literal["1.0"] = "1.0"
    case_id: str
    model: str = ""
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    evidence: EvidenceTrace = Field(default_factory=EvidenceTrace)
    answer: str = ""
    latency_ms: int = Field(default=0, ge=0)
    usage: UsageTrace = Field(default_factory=UsageTrace)
    metadata: dict[str, str] = Field(default_factory=dict)


class AssertionResult(BaseModel):
    """Result of one named deterministic assertion."""

    metric: str
    passed: bool
    hard_failure: bool = False
    detail: str = ""


class CaseResult(BaseModel):
    """Evaluation result for one case."""

    case_id: str
    category: str
    passed: bool
    assertions: list[AssertionResult]


class MetricSummary(BaseModel):
    """Aggregate pass rate for one metric."""

    passed: int
    total: int
    rate: float


class EvalReport(BaseModel):
    """Machine-readable output of a deterministic evaluation run."""

    schema_version: Literal["1.0"] = "1.0"
    dataset_name: str
    dataset_version: str
    generated_at: datetime
    passed: bool
    cases_passed: int
    cases_total: int
    metrics: dict[str, MetricSummary]
    results: list[CaseResult]
    run_metadata: dict[str, str] = Field(default_factory=dict)
