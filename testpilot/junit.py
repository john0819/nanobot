"""Bounded JUnit parsing; count testcase outcomes rather than trusting totals."""

from dataclasses import dataclass

from defusedxml import ElementTree

from testpilot.artifacts import MAX_ARTIFACT_BYTES
from testpilot.domain import Counts

PARSER_VERSION = "junit-v1"


@dataclass(frozen=True)
class ParsedJUnit:
    counts: Counts
    failed_cases: tuple[str, ...]
    observed_cases: tuple[str, ...]


def parse_junit(content: bytes, expected_cases: tuple[str, ...]) -> ParsedJUnit:
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ValueError("JUnit exceeds size limit")
    if len(expected_cases) != len(set(expected_cases)):
        raise ValueError("duplicate planned case identity")
    root = ElementTree.fromstring(content, forbid_dtd=True)
    if root.tag not in {"testsuites", "testsuite"}:
        raise ValueError("unsupported JUnit root")
    expected = set(expected_cases)
    observed: list[str] = []
    failed: list[str] = []
    totals = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for case in root.iter("testcase"):
        if len(observed) >= 10000:
            raise ValueError("too many JUnit cases")
        name = case.get("name")
        classname = case.get("classname")
        if not name or not classname:
            raise ValueError("JUnit case identity is missing")
        identity = f"{classname}::{name}"
        if identity not in expected or identity in observed:
            raise ValueError("unexpected or duplicate JUnit case")
        observed.append(identity)
        outcomes = [child.tag for child in case if child.tag in {"failure", "error", "skipped"}]
        if len(set(outcomes)) > 1:
            raise ValueError("ambiguous JUnit case outcome")
        category = {"failure": "failed", "error": "errors", "skipped": "skipped"}.get(
            outcomes[0] if outcomes else "", "passed"
        )
        totals[category] += 1
        if category in {"failed", "errors"}:
            failed.append(identity)
    # Validate aggregate attributes against descendant cases, including nested suites.
    for suite in root.iter():
        if suite.tag not in {"testsuite", "testsuites"}:
            continue
        cases = list(suite.iter("testcase"))
        actual = {
            "tests": len(cases),
            "failures": sum(any(c.tag == "failure" for c in case) for case in cases),
            "errors": sum(any(c.tag == "error" for c in case) for case in cases),
            "skipped": sum(any(c.tag == "skipped" for c in case) for case in cases),
        }
        for attribute, number in actual.items():
            declared = suite.get(attribute)
            if declared is not None and int(declared) != number:
                raise ValueError(f"JUnit {attribute} disagrees with testcase outcomes")
    return ParsedJUnit(
        counts=Counts(planned=len(expected_cases), not_run=len(expected - set(observed)), **totals),
        failed_cases=tuple(failed),
        observed_cases=tuple(observed),
    )
