"""Execution boundary between QA Agent implementations and the evaluator."""

from __future__ import annotations

from typing import Protocol

from nanobot.evals.qa_agent.contracts import AgentTrace, EvalCase, EvalDataset, EvalReport
from nanobot.evals.qa_agent.evaluator import evaluate_dataset


class AgentEvalBackend(Protocol):
    """Adapter implemented by deterministic mock and live Agent runtimes."""

    async def run_case(self, case: EvalCase) -> AgentTrace:
        """Execute one isolated case and return its normalized trace."""
        ...


async def collect_traces(
    dataset: EvalDataset,
    backend: AgentEvalBackend,
) -> list[AgentTrace]:
    """Run every case sequentially and validate backend trace identity."""
    traces: list[AgentTrace] = []
    for case in dataset.cases:
        trace = await backend.run_case(case)
        if trace.case_id != case.id:
            raise ValueError(f"backend returned trace for {trace.case_id!r}; expected {case.id!r}")
        traces.append(trace)
    return traces


async def run_evaluation(
    dataset: EvalDataset,
    backend: AgentEvalBackend,
    *,
    run_metadata: dict[str, str] | None = None,
) -> tuple[EvalReport, list[AgentTrace]]:
    """Collect normalized traces and evaluate them with the shared scorer."""
    traces = await collect_traces(dataset, backend)
    return evaluate_dataset(dataset, traces, run_metadata=run_metadata), traces
