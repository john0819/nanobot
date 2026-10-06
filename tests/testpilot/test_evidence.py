"""Evidence negatives: unsupported claims cannot cross the publication gate."""

import json
from pathlib import Path

import pytest
from defusedxml.common import DefusedXmlException

from testpilot.artifacts import MAX_ARTIFACT_BYTES, ArtifactStore
from testpilot.domain import ExecutionRecord
from testpilot.evidence import build_report, render_markdown
from testpilot.execution import fixture_target
from testpilot.junit import parse_junit

SHA = "a" * 40
CASES = ("suite::pass", "suite::fail", "suite::skip")
XML = b'''<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1">
<testcase classname="suite" name="pass"/>
<testcase classname="suite" name="fail"><failure message="independent oracle"/></testcase>
<testcase classname="suite" name="skip"><skipped/></testcase>
</testsuite></testsuites>'''


@pytest.fixture
def evidence(tmp_path: Path) -> tuple[ArtifactStore, ExecutionRecord]:
    store = ArtifactStore(tmp_path / "artifacts")
    record = ExecutionRecord(
        evidence_id="ev_one", task_id="task_one", run_id="run_one", operation_id="op_one",
        target=fixture_target(SHA, "healthy"), state="COMPLETED", exit_code=1,
        expected_cases=CASES, junit_hash=store.put(XML), log_hash=store.put(b"real-log-contract-fixture"),
        observed_at="2026-10-06T00:00:00+00:00",
    )
    return store, record


def candidate(record: ExecutionRecord) -> dict:
    counts = parse_junit(XML, CASES).counts.model_dump()
    return {"task_id": record.task_id, "run_id": record.run_id,
            "target": record.target.model_dump(), "claims": [
                {"claim_id": "numbers", "type": "TEST_COUNTS", "value": counts,
                 "evidence_ids": [record.evidence_id]},
                {"claim_id": "failed", "type": "CASE_FAILED", "value": "suite::fail",
                 "evidence_ids": [record.evidence_id]},
            ]}


def report(store: ArtifactStore, record: ExecutionRecord, data: dict | None = None) -> dict:
    return build_report(task_id="task_one", target=fixture_target(SHA, "healthy"), record=record,
                        store=store, candidate_json=json.dumps(data or candidate(record)))


def test_counts_conserve_and_skip_is_not_executed() -> None:
    counts = parse_junit(XML, CASES + ("suite::missing",)).counts
    assert counts.model_dump() == dict(planned=4, passed=1, failed=1, errors=0, skipped=1, not_run=1)
    assert counts.executed == 2
    assert counts.pass_rate == 0.25


def test_valid_fail_report_is_complete_not_runtime_failure(evidence) -> None:
    store, record = evidence
    result = report(store, record)
    assert result["report_validated"] is True
    assert result["quality_verdict"] == "FAIL"
    assert result["task_status"] == "COMPLETED"
    assert result["findings"][0]["category"] == "UNKNOWN"


@pytest.mark.parametrize("field,value", [("commit_sha", "b" * 40), ("project_id", "other"),
                                        ("tenant_id", "other"), ("env_snapshot_id", "other"),
                                        ("suite_hash", "b" * 64)])
def test_execution_target_mismatch_rejected(evidence, field, value) -> None:
    store, record = evidence
    target = record.target.model_copy(update={field: value})
    result = report(store, record.model_copy(update={"target": target}))
    assert result["report_validated"] is False
    assert result["test_summary"] is None
    assert "EXECUTION_SCOPE_MISMATCH" in result["validation_gaps"]


@pytest.mark.parametrize("field,value", [("task_id", "another-task"), ("run_id", "another-run")])
def test_model_scope_mismatch_rejected(evidence, field, value) -> None:
    store, record = evidence
    data = candidate(record)
    data[field] = value
    result = report(store, record, data)
    assert result["report_validated"] is False
    assert all(c["status"] == "UNSUPPORTED" for c in result["claims"])


def test_all_passed_and_invented_counts_are_blocked(evidence) -> None:
    store, record = evidence
    data = candidate(record)
    data["claims"][0]["value"] = dict(planned=3, passed=3, failed=0, errors=0, skipped=0, not_run=0)
    data["claims"].append({"claim_id": "fake-pass", "type": "ALL_PASSED", "value": True,
                           "evidence_ids": [record.evidence_id]})
    result = report(store, record, data)
    assert result["report_validated"] is False
    assert result["quality_verdict"] == "FAIL"
    assert result["test_summary"]["skipped"] == 1
    assert result["test_summary"]["failed"] == 1


def test_missing_or_wrong_evidence_id_and_duplicate_claim_id_blocked(evidence) -> None:
    store, record = evidence
    data = candidate(record)
    data["claims"][0]["evidence_ids"] = ["ev_invented"]
    data["claims"].append(data["claims"][1])
    result = report(store, record, data)
    assert result["report_validated"] is False
    assert result["claims"][-1]["status"] == "UNSUPPORTED"


@pytest.mark.parametrize("artifact", ["junit_hash", "log_hash"])
def test_artifact_tampering_blocks_statistics(evidence, artifact) -> None:
    store, record = evidence
    (store.root / getattr(record, artifact)).write_bytes(b"tampered")
    result = report(store, record)
    assert result["report_validated"] is False
    assert result["test_summary"] is None


@pytest.mark.parametrize("updates", [{"state": "TIMED_OUT"}, {"exit_code": 2},
                                    {"exit_code": 0}, {"junit_hash": None}])
def test_incomplete_or_conflicting_execution_blocks_report(evidence, updates) -> None:
    store, record = evidence
    result = report(store, record.model_copy(update=updates))
    assert result["report_validated"] is False
    assert result["test_summary"] is None


def test_no_tool_execution_cannot_publish_model_prose(tmp_path) -> None:
    result = build_report(task_id="task_one", target=fixture_target(SHA, "healthy"), record=None,
                          store=ArtifactStore(tmp_path), candidate_json="全部通过；已经提交 BUG-123")
    assert result["report_validated"] is False
    assert result["quality_verdict"] == "INCONCLUSIVE"
    assert "BUG-123" not in render_markdown(result)


@pytest.mark.parametrize("xml", [
    b'<testsuite><testcase classname="suite" name="pass"/><testcase classname="suite" name="pass"/></testsuite>',
    b'<testsuite><testcase classname="suite" name="unknown"/></testsuite>',
    b'<testsuite tests="2"><testcase classname="suite" name="pass"/></testsuite>',
    b'<testsuite><testcase classname="suite" name="pass"><failure/><skipped/></testcase></testsuite>',
    b'<testsuite><testcase name="pass"/></testsuite>', b'<not-junit/>', b'<malformed',
])
def test_invalid_junit_rejected(xml) -> None:
    with pytest.raises((ValueError, SyntaxError)):
        parse_junit(xml, CASES)


def test_dtd_and_large_xml_rejected() -> None:
    with pytest.raises(DefusedXmlException):
        parse_junit(b'<!DOCTYPE x [<!ENTITY a "expansion">]><testsuite/>', CASES)
    with pytest.raises(ValueError, match="size limit"):
        parse_junit(b" " * (MAX_ARTIFACT_BYTES + 1), CASES)


def test_nested_suite_counts_not_double_counted() -> None:
    parsed = parse_junit(b'<testsuites tests="1"><testsuite tests="1"><testsuite tests="1">'
                         b'<testcase classname="suite" name="pass"/></testsuite></testsuite></testsuites>', CASES)
    assert parsed.counts.passed == 1
    assert parsed.counts.not_run == 2


def test_empty_suite_never_supports_all_passed(evidence) -> None:
    store, record = evidence
    record = record.model_copy(update={"expected_cases": (), "exit_code": 0,
                                       "junit_hash": store.put(b'<testsuite tests="0"/>')})
    data = {"task_id": record.task_id, "run_id": record.run_id, "target": record.target.model_dump(),
            "claims": [{"claim_id": "pass", "type": "ALL_PASSED", "value": True,
                        "evidence_ids": [record.evidence_id]}]}
    result = report(store, record, data)
    assert result["report_validated"] is False
    assert result["quality_verdict"] == "INCONCLUSIVE"


def test_skip_and_missing_scope_cannot_be_disguised_as_pass(evidence) -> None:
    store, record = evidence
    record = record.model_copy(update={"expected_cases": ("suite::pass", "suite::skip", "suite::missing"),
                                       "exit_code": 0, "junit_hash": store.put(
                                           b'<testsuite><testcase classname="suite" name="pass"/>'
                                           b'<testcase classname="suite" name="skip"><skipped/></testcase></testsuite>')})
    counts = dict(planned=3, passed=1, failed=0, errors=0, skipped=1, not_run=1)
    data = {"task_id": record.task_id, "run_id": record.run_id, "target": record.target.model_dump(),
            "claims": [{"claim_id": "counts", "type": "TEST_COUNTS", "value": counts,
                        "evidence_ids": [record.evidence_id]}]}
    result = report(store, record, data)
    assert result["report_validated"] is False
    assert result["quality_verdict"] == "INCONCLUSIVE"
    assert result["test_summary"]["not_run"] == 1
    assert "INCOMPLETE_TEST_SCOPE" in result["validation_gaps"]


def test_all_skipped_cannot_support_all_passed(evidence) -> None:
    store, record = evidence
    record = record.model_copy(update={"expected_cases": ("suite::skip",), "exit_code": 0,
                                       "junit_hash": store.put(b'<testsuite><testcase classname="suite" '
                                                                 b'name="skip"><skipped/></testcase></testsuite>')})
    data = {"task_id": record.task_id, "run_id": record.run_id, "target": record.target.model_dump(),
            "claims": [{"claim_id": "pass", "type": "ALL_PASSED", "value": True,
                        "evidence_ids": [record.evidence_id]}]}
    result = report(store, record, data)
    assert result["report_validated"] is False
    assert result["test_summary"]["executed"] == 0
    assert result["quality_verdict"] == "INCONCLUSIVE"


def test_oversized_model_candidate_is_not_parsed(evidence) -> None:
    store, record = evidence
    result = build_report(task_id=record.task_id, target=record.target, record=record, store=store,
                          candidate_json=" " * 65537)
    assert result["report_validated"] is False
    assert "INVALID_REPORT_CANDIDATE" in result["validation_gaps"]


def test_artifact_path_escape_and_symlink_blocked(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "store")
    with pytest.raises(ValueError):
        store.read("../secret")
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    (store.root / ("c" * 64)).symlink_to(outside)
    with pytest.raises(ValueError):
        store.read("c" * 64)
