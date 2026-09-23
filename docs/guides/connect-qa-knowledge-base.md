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

If the local connection is blocked before OAuth begins, check the exact loopback CIDR in
`tools.ssrfWhitelist`. If OAuth succeeds but discovery fails, verify that the identity provider's
introspection response contains the required `kb.search` scope and the configured audience.
