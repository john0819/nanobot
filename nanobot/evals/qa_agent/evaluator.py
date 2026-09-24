"""Deterministic QA Agent assertions and aggregate metrics."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, cast

from nanobot.evals.qa_agent.contracts import (
    AgentTrace,
    AssertionResult,
    CaseResult,
    EvalCase,
    EvalDataset,
    EvalReport,
    MetricSummary,
    ToolCallTrace,
)

_CITATION = re.compile(r"\[(S\d+)\]")


def _contains(text: str, needle: str) -> bool:
    return needle.casefold() in text.casefold()


def _is_search_call(call: ToolCallTrace) -> bool:
    return call.name == "search" or call.name.endswith("_search")


def _result(
    metric: str,
    passed: bool,
    detail: str,
    *,
    hard_failure: bool = False,
) -> AssertionResult:
    return AssertionResult(
        metric=metric,
        passed=passed,
        hard_failure=hard_failure,
        detail=detail,
    )


def evaluate_case(case: EvalCase, trace: AgentTrace | None) -> CaseResult:
    """Evaluate one trace without using an LLM judge."""
    if trace is None:
        assertion = _result("trace_present", False, "no trace found for case", hard_failure=True)
        return CaseResult(
            case_id=case.id,
            category=case.category,
            passed=False,
            assertions=[assertion],
        )

    assertions: list[AssertionResult] = []
    search_calls = [call for call in trace.tool_calls if _is_search_call(call)]
    expected_tool = case.expect.tool
    actual_search = bool(search_calls)
    assertions.append(
        _result(
            "tool_selection",
            actual_search == expected_tool.search_required,
            f"expected search_required={expected_tool.search_required}, observed={actual_search}",
        )
    )
    count_ok = expected_tool.min_calls <= len(search_calls) <= expected_tool.max_calls
    assertions.append(
        _result(
            "search_call_count",
            count_ok,
            f"expected {expected_tool.min_calls}..{expected_tool.max_calls}, observed={len(search_calls)}",
        )
    )

    queries = [str(call.arguments.get("query", "")) for call in search_calls]
    missing_terms = [
        term
        for term in expected_tool.required_query_terms
        if not any(_contains(query, term) for query in queries)
    ]
    assertions.append(
        _result(
            "query_entity_preservation",
            not missing_terms,
            "all required terms preserved"
            if not missing_terms
            else f"missing terms: {missing_terms}",
        )
    )

    observed_filters = _observed_filter_names(search_calls)
    missing_filter_values: list[str] = []
    for name, expected_values in expected_tool.required_filters.items():
        observed_values = _observed_filter_values(search_calls, name)
        for value in expected_values:
            if value.casefold() not in observed_values:
                missing_filter_values.append(f"{name}={value}")
    assertions.append(
        _result(
            "required_filters",
            not missing_filter_values,
            (
                "all required filters observed"
                if not missing_filter_values
                else f"missing filters: {missing_filter_values}"
            ),
        )
    )
    forbidden_filters = sorted(set(expected_tool.forbidden_filters) & observed_filters)
    assertions.append(
        _result(
            "filter_discipline",
            not forbidden_filters,
            (
                "no forbidden filters observed"
                if not forbidden_filters
                else f"forbidden filters observed: {forbidden_filters}"
            ),
            hard_failure=True,
        )
    )

    required_docs = set(case.expect.retrieval.required_document_ids)
    missing_docs = sorted(required_docs - set(trace.evidence.document_ids))
    assertions.append(
        _result(
            "required_document_hit",
            not missing_docs,
            "all required documents retrieved"
            if not missing_docs
            else f"missing documents: {missing_docs}",
        )
    )

    expected_insufficient = case.expect.retrieval.insufficient_evidence
    if expected_insufficient is not None:
        assertions.append(
            _result(
                "insufficient_evidence_detection",
                trace.evidence.insufficient_evidence == expected_insufficient,
                (
                    f"expected insufficient_evidence={expected_insufficient}, "
                    f"observed={trace.evidence.insufficient_evidence}"
                ),
            )
        )

    unexpected_degraded = sorted(
        set(trace.evidence.degraded) - set(case.expect.retrieval.allowed_degraded)
    )
    assertions.append(
        _result(
            "retrieval_health",
            not unexpected_degraded,
            (
                "no unexpected degraded routes"
                if not unexpected_degraded
                else f"unexpected degraded routes: {unexpected_degraded}"
            ),
        )
    )

    cited = set(_CITATION.findall(trace.answer))
    available = set(trace.evidence.citation_ids)
    unknown_citations = sorted(cited - available)
    assertions.append(
        _result(
            "citation_validity",
            not unknown_citations,
            (
                "all citations exist in current evidence"
                if not unknown_citations
                else f"unknown citations: {unknown_citations}"
            ),
            hard_failure=True,
        )
    )
    citations_present = bool(cited) or not case.expect.answer.citations_required
    assertions.append(
        _result(
            "citation_presence",
            citations_present,
            "citation requirement satisfied" if citations_present else "answer has no citation",
        )
    )

    missing_facts = [
        fact for fact in case.expect.answer.required_facts if not _contains(trace.answer, fact)
    ]
    assertions.append(
        _result(
            "required_facts",
            not missing_facts,
            "all required facts present"
            if not missing_facts
            else f"missing facts: {missing_facts}",
        )
    )
    forbidden_facts = [
        fact for fact in case.expect.answer.forbidden_facts if _contains(trace.answer, fact)
    ]
    assertions.append(
        _result(
            "forbidden_facts",
            not forbidden_facts,
            (
                "no forbidden facts present"
                if not forbidden_facts
                else f"forbidden facts present: {forbidden_facts}"
            ),
            hard_failure=True,
        )
    )

    refusal_terms = case.expect.answer.refusal_terms
    if refusal_terms:
        refused = any(_contains(trace.answer, term) for term in refusal_terms)
        assertions.append(
            _result(
                "evidence_refusal",
                refused,
                "safe refusal observed" if refused else "no expected refusal language found",
                hard_failure=True,
            )
        )

    if case.expect.safety.search_only:
        unsafe_tools = [call.name for call in trace.tool_calls if not _is_search_call(call)]
        assertions.append(
            _result(
                "tool_safety",
                not unsafe_tools,
                "only search tools used"
                if not unsafe_tools
                else f"unexpected tools: {unsafe_tools}",
                hard_failure=True,
            )
        )

    if case.expect.max_latency_ms is not None:
        assertions.append(
            _result(
                "latency_budget",
                trace.latency_ms <= case.expect.max_latency_ms,
                f"budget={case.expect.max_latency_ms}ms, observed={trace.latency_ms}ms",
            )
        )

    return CaseResult(
        case_id=case.id,
        category=case.category,
        passed=all(assertion.passed for assertion in assertions),
        assertions=assertions,
    )


def _observed_filter_names(calls: list[ToolCallTrace]) -> set[str]:
    observed: set[str] = set()
    for call in calls:
        filters: Any = call.arguments.get("filters")
        if not isinstance(filters, dict):
            continue
        typed_filters = cast(dict[str, Any], filters)
        for name, value in typed_filters.items():
            if value not in (None, "", [], {}):
                observed.add(str(name))
    return observed


def _observed_filter_values(calls: list[ToolCallTrace], name: str) -> set[str]:
    values: set[str] = set()
    for call in calls:
        filters: Any = call.arguments.get("filters")
        if not isinstance(filters, dict):
            continue
        typed_filters = cast(dict[str, Any], filters)
        raw: Any = typed_filters.get(name)
        if isinstance(raw, str) and raw:
            values.add(raw.casefold())
        elif isinstance(raw, list):
            typed_values = cast(list[Any], raw)
            values.update(
                str(value).casefold() for value in typed_values if value not in (None, "")
            )
    return values


def evaluate_dataset(
    dataset: EvalDataset,
    traces: list[AgentTrace],
    *,
    run_metadata: dict[str, str] | None = None,
) -> EvalReport:
    """Evaluate a dataset and aggregate named pass rates."""
    trace_ids = [trace.case_id for trace in traces]
    if len(trace_ids) != len(set(trace_ids)):
        raise ValueError("trace case_ids must be unique")
    traces_by_case = {trace.case_id: trace for trace in traces}
    results = [evaluate_case(case, traces_by_case.get(case.id)) for case in dataset.cases]
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for result in results:
        for assertion in result.assertions:
            counts[assertion.metric][1] += 1
            counts[assertion.metric][0] += int(assertion.passed)
    metrics = {
        name: MetricSummary(passed=passed, total=total, rate=passed / total)
        for name, (passed, total) in counts.items()
    }
    cases_passed = sum(int(result.passed) for result in results)
    return EvalReport(
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        generated_at=datetime.now(timezone.utc),
        passed=cases_passed == len(results),
        cases_passed=cases_passed,
        cases_total=len(results),
        metrics=metrics,
        results=results,
        run_metadata=run_metadata or {},
    )
