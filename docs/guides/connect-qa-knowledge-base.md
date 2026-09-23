# Connect qa-kb-service over MCP HTTP

nanobot can consume `qa-kb-service` as a remote, read-only knowledge tool over MCP
Streamable HTTP. The integration uses nanobot's existing MCP client rather than adding retrieval
logic to the agent loop.

## Contract

The built-in `QA Knowledge Base` preset applies these boundaries:

- transport: MCP Streamable HTTP;
- authentication: OAuth;
- endpoint: `http://127.0.0.1:8910/mcp` for local development;
- exposed tools: `search` only;
- tool timeout: 30 seconds.

The knowledge service remains responsible for identity, tenant and group authorization, rate
limiting, retrieval, reranking, citations and audit events. nanobot is the MCP client and does not
duplicate those policies.

## Runtime architecture

```text
User
  -> nanobot Agent (DeepSeek Flash)
  -> MCP Streamable HTTP client
  -> OAuth-protected qa-kb-service search tool
  -> query processing
  -> Elasticsearch BM25 + Qdrant dense retrieval
  -> RRF + rerank + parent context packing
  -> cited evidence
  -> DeepSeek grounded answer
```

The models have separate responsibilities. DeepSeek is the Agent's generation and tool-calling
model. The embedding, query-rewrite and rerank models belong to `qa-kb-service`; their provider and
version must match the published index generation. Replacing the Agent model must not trigger an
index rebuild, while changing the embedding model or dimensions must create a new index generation.

Configure DeepSeek without writing its API key into `config.json`:

```bash
export DEEPSEEK_API_KEY="..."
```

```json
{
  "providers": {
    "deepseek": {
      "apiKey": "${DEEPSEEK_API_KEY}",
      "apiBase": "https://api.deepseek.com"
    }
  },
  "modelPresets": {
    "deepseek-flash": {
      "provider": "deepseek",
      "model": "deepseek-flash",
      "maxTokens": 8192,
      "contextWindowTokens": 1000000,
      "reasoningEffort": "none"
    }
  },
  "agents": {
    "defaults": {
      "modelPreset": "deepseek-flash"
    }
  }
}
```

Use a secret manager or process environment in deployment. Do not commit a real key, bearer token,
OAuth client secret or generated OAuth credential store.

## Local development

Start the knowledge service in Streamable HTTP mode with its introspection-based authentication
configured. Then add the loopback host to nanobot's explicit SSRF allowlist and enable the preset:

```json
{
  "tools": {
    "ssrfWhitelist": ["127.0.0.1/32"],
    "mcpServers": {
      "qa-knowledge-base": {
        "type": "streamableHttp",
        "auth": "oauth",
        "url": "http://127.0.0.1:8910/mcp",
        "toolTimeout": 30,
        "enabledTools": ["search"]
      }
    }
  }
}
```

The allowlist entry is intentionally a single loopback address. Do not replace it with a broad
private network range. In the WebUI, open **Apps → MCP → QA Knowledge Base → Connect** and finish
the OAuth authorization flow.

An automated local smoke test may use a short-lived Bearer token in `headers` together with a local
RFC 7662 introspection stub. Keep that config in a Git-ignored runtime directory. The built-in
preset remains OAuth-based, and static test tokens must never be copied to a shared environment.

## Production deployment

Override the preset URL in `~/.nanobot/config.json` with the public HTTPS resource URL configured
by `QAKB_MCP_RESOURCE_SERVER_URL` on `qa-kb-service`:

```json
{
  "tools": {
    "mcpServers": {
      "qa-knowledge-base": {
        "type": "streamableHttp",
        "auth": "oauth",
        "url": "https://kb.example.com/mcp",
        "toolTimeout": 30,
        "enabledTools": ["search"]
      }
    }
  }
}
```

For a public endpoint, terminate TLS at the ingress, keep the OAuth audience equal to the MCP
resource URL, and do not add a public address to `ssrfWhitelist`. OAuth access and refresh tokens
are stored in nanobot's MCP credential store rather than in `config.json`.

## Verification

Run the gateway in verbose mode and connect the preset:

```bash
nanobot gateway --verbose
```

Verify all of the following:

1. the MCP server reaches `connected` in **Apps → MCP**;
2. the discovered tool list contains `search` and no write tool;
3. a question such as “现货订单从创建到成交有哪些状态？” invokes
   `mcp_qa-knowledge-base_search`;
4. the answer contains citations returned by `qa-kb-service`;
5. missing scope, tenant or group claims fail closed instead of returning unfiltered documents.

For an end-to-end acceptance test, also verify:

- PostgreSQL's published generation has the same Child IDs as the active Elasticsearch and Qdrant
  indexes;
- both lexical and dense routes contribute to a hybrid result, unless query routing intentionally
  selects an exact-identifier strategy;
- the MCP audit event contains a hashed principal, status and latency but no query or document text;
- the final model answer preserves citation IDs and does not claim facts absent from the returned
  evidence;
- stopping either retrieval engine produces the documented degraded behavior, while losing all
  eligible retrieval routes fails the tool call instead of asking the model to guess.

If the local connection is blocked before OAuth begins, check the exact loopback CIDR in
`tools.ssrfWhitelist`. If OAuth succeeds but discovery fails, verify that the identity provider's
introspection response contains the required `kb.search` scope and the configured audience.
