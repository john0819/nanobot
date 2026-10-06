"""Offline reproducible fixture demonstration: python -m testpilot."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from testpilot.artifacts import ArtifactStore
from testpilot.baseline import FORK_BASELINE_SHA
from testpilot.demo_provider import ScriptedProvider
from testpilot.evidence import render_markdown
from testpilot.execution import FixedFixtureExecutor, fixture_target
from testpilot.nanobot_adapter import run_task


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["healthy", "retry-write-bug"], default="healthy")
    parser.add_argument("--claim-all-passed", action="store_true", help="Inject an unsupported model claim")
    parser.add_argument("--output", type=Path, default=Path(".local/testpilot"))
    args = parser.parse_args()
    # The fixture is bound to this implementation baseline, not an arbitrary PR SHA.
    mode = "healthy" if args.mode == "healthy" else "retry-write-bug"
    target = fixture_target(FORK_BASELINE_SHA, mode)
    task_id = f"task_{uuid4().hex}"
    output = args.output.resolve() / task_id
    store = ArtifactStore(output / "artifacts")
    executor = FixedFixtureExecutor(store, task_id, target, mode)
    provider = ScriptedProvider(claim_all_passed=args.claim_all_passed)
    report = await run_task(executor, provider, provider.get_default_model())
    report["evaluation_kind"] = "scripted-provider-real-pytest"
    output.joinpath("report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.joinpath("report.md").write_text(render_markdown(report), encoding="utf-8")
    if executor.record is not None:
        output.joinpath("execution.json").write_text(executor.record.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output / "report.json"), "quality_verdict": report["quality_verdict"],
                      "report_validated": report["report_validated"], "counts": report["test_summary"]}, ensure_ascii=False))
    return 0 if report["report_validated"] else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
