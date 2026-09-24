"""Replay recorded QA Agent traces against a frozen evaluation dataset."""

from __future__ import annotations

import argparse
from pathlib import Path

from nanobot.evals.qa_agent.evaluator import evaluate_dataset
from nanobot.evals.qa_agent.io import load_dataset, load_traces, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--traces", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--agent-policy-version", default="")
    parser.add_argument("--skill-version", default="")
    args = parser.parse_args()

    metadata = {
        key: value
        for key, value in {
            "model": args.model,
            "agent_policy_version": args.agent_policy_version,
            "skill_version": args.skill_version,
            "mode": "replay",
        }.items()
        if value
    }
    report = evaluate_dataset(
        load_dataset(args.dataset),
        load_traces(args.traces),
        run_metadata=metadata,
    )
    json_path, markdown_path = write_report(report, args.output_dir)
    print(f"QA Agent evaluation: {'PASS' if report.passed else 'FAIL'}")
    print(f"Cases: {report.cases_passed}/{report.cases_total}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
