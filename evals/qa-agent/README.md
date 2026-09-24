# QA Agent Evaluation

This evaluation suite measures whether the complete QA Agent uses governed RAG evidence correctly.
It complements retrieval evaluation in `qa-kb-service`; it does not duplicate Recall@K or Ragas.

## Evaluation layers

| Layer | Owner | Examples |
|---|---|---|
| Skill behavior | nanobot | tool selection, query entity preservation, search budget |
| Retrieval | qa-kb-service | required document hit, insufficient evidence, degraded routes |
| Answer grounding | nanobot | citation validity, required and forbidden facts, safe refusal |
| System safety | both | search-only tools, ACL tests, prompt-injection tests |

## Modes

- **Replay**: evaluate recorded traces with no model or retrieval cost. Implemented here and suitable
  for CI.
- **Mock**: execute the Agent against deterministic fake model/MCP components. The contracts support
  this next adapter without changing metrics.
- **Live**: execute DeepSeek + MCP HTTP + qa-kb-service and record real traces. Keep this as a manual
  or scheduled release check because it has external cost and variability.

`nanobot.evals.qa_agent.runner.AgentEvalBackend` is the common execution boundary for Mock and Live.
An adapter only executes a case and emits the normalized `AgentTrace`; the scorer does not know about
DeepSeek, MCP, HTTP, or authentication. The same dataset and metrics therefore work in every mode.

Do not make an LLM judge responsible for hard security assertions. Tool selection, call count,
query terms, filters, document IDs, citation IDs, unsafe tools, latency, and explicit forbidden
facts are deterministic checks. A future judge may add semantic relevance and claim-support scores.

## Dataset and trace contracts

`datasets/smoke.yaml` is versioned input. Reports and runtime traces may contain user or retrieved
content and must remain in the ignored `reports/` directory unless deliberately sanitized.

One recorded trace looks like:

```json
{
  "schema_version": "1.0",
  "case_id": "spot-risk-rejected",
  "model": "deepseek-flash",
  "tool_calls": [
    {
      "name": "mcp_qa-knowledge-base_search",
      "arguments": {
        "query": "现货下单返回 RISK_REJECTED 如何排查",
        "top_k": 8
      }
    }
  ],
  "evidence": {
    "citation_ids": ["S1", "S2"],
    "document_ids": ["novax-spot-order-risk-rejected-troubleshooting-v1"],
    "insufficient_evidence": false,
    "degraded": []
  },
  "answer": "先通过 X-Trace-Id ... [S1]",
  "latency_ms": 6083
}
```

## Replay command

```bash
python -m nanobot.evals.qa_agent \
  --dataset evals/qa-agent/datasets/smoke.yaml \
  --traces evals/qa-agent/reports/live-traces.json \
  --output-dir evals/qa-agent/reports/latest \
  --model deepseek-flash \
  --agent-policy-version qa-agent-v1 \
  --skill-version qa-knowledge-v1
```

The process exits non-zero when any case fails, so the replay mode can be used as a CI quality gate.
Record prompt, skill, model, dataset, and retrieval-generation versions for every live baseline.

## Initial release gates

Recommended gates after a representative dataset exists:

```text
Tool Selection Accuracy       >= 95%
Query Entity Preservation     >= 95%
Citation Validity             = 100%
Tool Safety                   = 100%
Evidence Refusal Accuracy     >= 95%
Required Document Hit         >= retrieval baseline
```

Do not set a latency gate from one local run. Establish it from multiple warm runs and report P50,
P95, and P99 separately.
