from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from nanobot.evals.qa_agent.contracts import AgentTrace, EvalCase
from nanobot.evals.qa_agent.evaluator import evaluate_dataset
from nanobot.evals.qa_agent.io import load_dataset, load_traces, write_report
from nanobot.evals.qa_agent.runner import collect_traces

DATASET = Path("evals/qa-agent/datasets/smoke.yaml")


def _trace(**overrides: object) -> AgentTrace:
    payload: dict[str, object] = {
        "case_id": "spot-risk-rejected",
        "model": "deepseek-flash",
        "tool_calls": [
            {
                "name": "mcp_qa-knowledge-base_search",
                "arguments": {"query": "现货 RISK_REJECTED 排查", "top_k": 8},
            }
        ],
        "evidence": {
            "citation_ids": ["S1", "S2"],
            "document_ids": ["novax-spot-order-risk-rejected-troubleshooting-v1"],
        },
        "answer": "通过 X-Trace-Id 检查 rule_id 和 config_version。[S1]",
        "latency_ms": 6000,
    }
    payload.update(overrides)
    return AgentTrace.model_validate(payload)


def test_smoke_dataset_is_valid_and_versioned() -> None:
    dataset = load_dataset(DATASET)

    assert dataset.schema_version == "1.0"
    assert dataset.version == "1.0.0"
    assert len(dataset.cases) == 12
    assert len({case.id for case in dataset.cases}) == len(dataset.cases)


def test_evaluator_passes_grounded_search_trace() -> None:
    dataset = load_dataset(DATASET)
    dataset.cases = [case for case in dataset.cases if case.id == "spot-risk-rejected"]

    report = evaluate_dataset(dataset, [_trace()])

    assert report.passed is True
    assert report.cases_passed == 1
    assert all(assertion.passed for assertion in report.results[0].assertions)
    assert report.metrics["citation_validity"].rate == 1.0
    assert report.metrics["tool_safety"].rate == 1.0


def test_evaluator_detects_unknown_citation_and_unsafe_tool() -> None:
    dataset = load_dataset(DATASET)
    dataset.cases = [case for case in dataset.cases if case.id == "spot-risk-rejected"]
    trace = _trace(
        answer="通过 X-Trace-Id 检查 rule_id 和 config_version。[S9]",
        tool_calls=[
            {
                "name": "mcp_qa-knowledge-base_search",
                "arguments": {"query": "现货 RISK_REJECTED 排查"},
            },
            {"name": "exec", "arguments": {"command": "dangerous"}},
        ],
    )

    report = evaluate_dataset(dataset, [trace])
    failed = {
        assertion.metric for assertion in report.results[0].assertions if not assertion.passed
    }

    assert report.passed is False
    assert {"citation_validity", "tool_safety"}.issubset(failed)


def test_evaluator_checks_required_and_forbidden_filters() -> None:
    dataset = load_dataset(DATASET)
    dataset.cases = [case for case in dataset.cases if case.id == "explicit-environment-filter"]
    passing = AgentTrace.model_validate(
        {
            "case_id": "explicit-environment-filter",
            "tool_calls": [
                {
                    "name": "search",
                    "arguments": {
                        "query": "价格偏离阈值",
                        "filters": {"environment": ["qa"]},
                    },
                }
            ],
            "evidence": {"citation_ids": ["S1"]},
            "answer": "QA 环境阈值见资料。[S1]",
        }
    )

    passing_report = evaluate_dataset(dataset, [passing])
    failing = passing.model_copy(deep=True)
    failing.tool_calls[0].arguments["filters"] = {"environment": ["prod"]}
    failing_report = evaluate_dataset(dataset, [failing])

    assert passing_report.passed is True
    assert failing_report.passed is False
    assert failing_report.metrics["required_filters"].rate == 0.0


def test_evaluator_requires_safe_refusal_for_insufficient_evidence() -> None:
    dataset = load_dataset(DATASET)
    dataset.cases = [case for case in dataset.cases if case.id == "unknown-product"]
    trace = AgentTrace.model_validate(
        {
            "case_id": "unknown-product",
            "tool_calls": [
                {
                    "name": "search",
                    "arguments": {"query": "量子算力质押 提前赎回手续费"},
                }
            ],
            "evidence": {"insufficient_evidence": True},
            "answer": "知识库没有足够证据，无法确认该手续费。",
        }
    )

    report = evaluate_dataset(dataset, [trace])

    assert report.passed is True
    assert report.metrics["evidence_refusal"].rate == 1.0


def test_missing_and_duplicate_traces_fail_explicitly() -> None:
    dataset = load_dataset(DATASET)
    dataset.cases = [case for case in dataset.cases if case.id == "spot-risk-rejected"]

    missing = evaluate_dataset(dataset, [])

    assert missing.passed is False
    assert missing.results[0].assertions[0].metric == "trace_present"
    with pytest.raises(ValueError, match="unique"):
        evaluate_dataset(dataset, [_trace(), _trace()])


def test_trace_jsonl_loading_and_report_writing(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"
    trace_path.write_text(_trace().model_dump_json() + "\n", encoding="utf-8")
    traces = load_traces(trace_path)
    dataset = load_dataset(DATASET)
    dataset.cases = [case for case in dataset.cases if case.id == "spot-risk-rejected"]
    report = evaluate_dataset(
        dataset,
        traces,
        run_metadata={"model": "deepseek-flash", "mode": "replay"},
    )

    json_path, markdown_path = write_report(report, tmp_path / "report")

    json_report = json.loads(json_path.read_text(encoding="utf-8"))
    markdown_report = markdown_path.read_text(encoding="utf-8")
    assert json_report["passed"] is True
    assert json_report["run_metadata"]["model"] == "deepseek-flash"
    assert "QA Agent Evaluation — PASS" in markdown_report
    assert "`citation_validity`" in markdown_report


@pytest.mark.asyncio
async def test_backend_contract_collects_normalized_traces() -> None:
    dataset = load_dataset(DATASET)
    dataset.cases = dataset.cases[:2]

    class FakeBackend:
        seen: ClassVar[list[str]] = []

        async def run_case(self, case: EvalCase) -> AgentTrace:
            self.seen.append(case.turns[-1].content)
            return AgentTrace(case_id=case.id, answer="fixture")

    traces = await collect_traces(dataset, FakeBackend())

    assert [trace.case_id for trace in traces] == [case.id for case in dataset.cases]
    assert len(FakeBackend.seen) == 2
