"""Safe dataset, trace, and report I/O for QA Agent evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter

from nanobot.evals.qa_agent.contracts import AgentTrace, EvalDataset, EvalReport

_TRACES = TypeAdapter(list[AgentTrace])


def load_dataset(path: Path) -> EvalDataset:
    """Load and validate one versioned YAML dataset."""
    payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return EvalDataset.model_validate(payload)


def load_traces(path: Path) -> list[AgentTrace]:
    """Load traces from a JSON array or JSONL file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
    return _TRACES.validate_python(payload)


def write_report(report: EvalReport, output_dir: Path) -> tuple[Path, Path]:
    """Write JSON and Markdown reports without mutating source datasets."""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "qa-agent-eval.json"
    markdown_path = output_dir / "qa-agent-eval.md"
    json_path.write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def render_markdown(report: EvalReport) -> str:
    """Render a compact review-friendly report."""
    status = "PASS" if report.passed else "FAIL"
    lines = [
        f"# QA Agent Evaluation — {status}",
        "",
        f"- Dataset: `{report.dataset_name}` `{report.dataset_version}`",
        f"- Cases: {report.cases_passed}/{report.cases_total}",
        f"- Generated: {report.generated_at.isoformat()}",
        "",
        "## Metrics",
        "",
        "| Metric | Passed | Total | Rate |",
        "|---|---:|---:|---:|",
    ]
    for name, metric in sorted(report.metrics.items()):
        lines.append(f"| `{name}` | {metric.passed} | {metric.total} | {metric.rate:.1%} |")
    lines.extend(["", "## Failed cases", ""])
    failures = [result for result in report.results if not result.passed]
    if not failures:
        lines.append("None.")
    for result in failures:
        lines.append(f"### `{result.case_id}` ({result.category})")
        lines.append("")
        for assertion in result.assertions:
            if not assertion.passed:
                severity = "hard" if assertion.hard_failure else "quality"
                lines.append(f"- `{assertion.metric}` [{severity}]: {assertion.detail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
