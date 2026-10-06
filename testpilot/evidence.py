"""Deterministic publication gate. Model final text is always private input."""

from dataclasses import dataclass
from typing import Literal

from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from testpilot.artifacts import ArtifactStore
from testpilot.domain import Claim, Counts, ExecutionRecord, ReportCandidate, Target
from testpilot.junit import PARSER_VERSION, ParsedJUnit, parse_junit


@dataclass(frozen=True)
class EvidenceCheck:
    parsed: ParsedJUnit | None
    gaps: tuple[str, ...]


def check_execution(
    record: ExecutionRecord | None, store: ArtifactStore, task_id: str, target: Target,
) -> EvidenceCheck:
    if record is None:
        return EvidenceCheck(None, ("NO_EXECUTION_EVIDENCE",))
    gaps: list[str] = []
    if record.task_id != task_id or record.target != target:
        gaps.append("EXECUTION_SCOPE_MISMATCH")
    if record.state != "COMPLETED" or record.exit_code not in {0, 1}:
        gaps.append("EXECUTION_NOT_COMPLETED")
    if record.junit_hash is None:
        gaps.append("MISSING_JUNIT")
    if gaps:
        return EvidenceCheck(None, tuple(gaps))
    try:
        store.read(record.log_hash)
        assert record.junit_hash is not None
        parsed = parse_junit(store.read(record.junit_hash), record.expected_cases)
    except (ValueError, OSError, SyntaxError, DefusedXmlException) as exc:
        # Do not echo XML/paths/exception text into public reports.
        return EvidenceCheck(None, (f"INVALID_ARTIFACT:{type(exc).__name__}",))
    has_failures = parsed.counts.failed + parsed.counts.errors > 0
    if (record.exit_code == 0 and has_failures) or (record.exit_code == 1 and not has_failures):
        return EvidenceCheck(None, ("EXIT_CODE_JUNIT_CONFLICT",))
    return EvidenceCheck(parsed, ())


def _supported(claim: Claim, parsed: ParsedJUnit) -> bool:
    counts = parsed.counts
    if claim.type == "TEST_EXECUTED":
        return claim.value is True and counts.executed > 0 and counts.not_run == 0
    if claim.type == "TEST_COUNTS":
        return isinstance(claim.value, Counts) and claim.value == counts
    if claim.type == "ALL_PASSED":
        all_passed = counts.planned > 0 and counts.passed == counts.planned
        return isinstance(claim.value, bool) and claim.value == all_passed
    return isinstance(claim.value, str) and claim.value in parsed.failed_cases


def build_report(
    *, task_id: str, target: Target, record: ExecutionRecord | None,
    store: ArtifactStore, candidate_json: str | None, runner_stop_reason: str = "completed",
) -> dict[str, object]:
    check = check_execution(record, store, task_id, target)
    gaps = list(check.gaps)
    parsed = check.parsed
    if parsed is not None and parsed.counts.not_run:
        gaps.append("INCOMPLETE_TEST_SCOPE")
    if runner_stop_reason != "completed":
        gaps.append("AGENT_NOT_COMPLETED")
    try:
        # Bound the dynamic model boundary independently of provider truncation.
        candidate = ReportCandidate.model_validate_json(
            candidate_json if candidate_json is not None and len(candidate_json) <= 65536 else ""
        )
    except ValidationError:
        candidate = None
        gaps.append("INVALID_REPORT_CANDIDATE")
    statuses: list[dict[str, str]] = []
    candidate_scope_valid = (
        candidate is not None and record is not None
        and candidate.task_id == task_id and candidate.run_id == record.run_id
        and candidate.target == target
    )
    if candidate is not None and not candidate_scope_valid:
        gaps.append("CANDIDATE_SCOPE_MISMATCH")
    if candidate is not None:
        seen: set[str] = set()
        for claim in candidate.claims:
            supported = (
                candidate_scope_valid and parsed is not None and record is not None
                and claim.evidence_ids == (record.evidence_id,)
                and claim.claim_id not in seen and _supported(claim, parsed)
            )
            seen.add(claim.claim_id)
            statuses.append({"claim_id": claim.claim_id, "type": claim.type,
                             "status": "VERIFIED" if supported else "UNSUPPORTED"})
            if not supported:
                gaps.append(f"UNSUPPORTED_CLAIM:{claim.claim_id}")
    counts = parsed.counts if parsed else None
    verdict: Literal["PASS", "FAIL", "INCONCLUSIVE"] = "INCONCLUSIVE"
    if counts is not None:
        if counts.failed or counts.errors:
            verdict = "FAIL"
        elif counts.planned and counts.passed == counts.planned:
            verdict = "PASS"
    # Publication is based on the gate, while safe partial reports retain real failure facts.
    return {
        "task_id": task_id, "report_version": 1,
        "task_status": "NEEDS_REVIEW" if gaps else "COMPLETED",
        "report_validated": not gaps, "quality_verdict": verdict,
        "target": target.model_dump(), "scope": list(record.expected_cases) if record else [],
        "run_id": record.run_id if record else None,
        "operation_id": record.operation_id if record else None,
        "completed_actions": ["fixed_fixture_pytest"] if parsed else [],
        "test_summary": ({**counts.model_dump(), "executed": counts.executed,
                          "pass_rate": counts.pass_rate} if counts else None),
        "findings": [{"category": "UNKNOWN", "case_id": case,
                      "claim_status": "VERIFIED", "evidence_ids": [record.evidence_id] if record else [],
                      "limitations": ["失败已复现；不能由单次结果确认产品缺陷或归因。"]}
                     for case in parsed.failed_cases] if parsed else [],
        "claims": statuses, "validation_gaps": gaps,
        "limitations": ["仅覆盖固定网关策略 fixture，不代表企业项目回归或代码覆盖率。",
                        "目标为工作树快照：commit_sha 记录 fork 基线，suite_hash 绑定实际源码与断言。",
                        ("独立隔离容器执行固定 HTTP suite；不开放生成代码，也不提供持久任务恢复。"
                         if record and record.source_system == "isolated-container-fixture"
                         else "本地执行器仅执行审核过的固定源码；不提供生成代码隔离或持久恢复。"),
                        "未验证真实 LLM 自主规划效果，RAG 本次未参与执行证据判定。"],
        "artifact_refs": [h for h in (record.junit_hash, record.log_hash) if h] if record else [],
        "next_actions": ["审查证据缺口，补充验证后重新生成报告。"] if gaps else [],
        "versions": {"evidence_contract": "v1", "junit_parser": PARSER_VERSION},
    }


def render_markdown(report: dict[str, object]) -> str:
    """Template uses server facts only; never publishes arbitrary model prose."""
    import json

    return (
        f"# TestPilot 测试报告\n\n任务：{report['task_id']}\n\n"
        f"状态：{report['task_status']}；测试结论：{report['quality_verdict']}\n\n"
        f"证据闸门通过：{report['report_validated']}\n\n"
        "可信统计及证据：\n\n```json\n"
        + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n"
    )
