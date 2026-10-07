"""Task-scoped read-only qa-kb-service MCP search; all identities are server-owned."""

import json
from typing import Any

import httpx
from pydantic import BaseModel, Field

from nanobot.security.network import resolve_url_target
from testpilot.artifacts import digest
from testpilot.domain import Contract


class KnowledgeUnavailableError(RuntimeError):
    pass


class Citation(BaseModel):
    citation_id: str
    document_id: str
    document_version_id: str
    parent_chunk_id: str
    title: str
    content: str
    source_uri: str | None = None
    start_line: int
    end_line: int
    truncated: bool = False


class KnowledgeResult(BaseModel):
    query: str
    citations: tuple[Citation, ...]
    context: str
    context_tokens: int
    warnings: tuple[dict[str, Any], ...] = ()
    insufficient_evidence: bool
    degraded: tuple[str, ...] = ()
    latency_ms: int = Field(ge=0)
    artifact_hash: str | None = None  # Host-owned projection; never trusted from MCP responses.


class RPCMessage(BaseModel):
    id: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class MCPText(BaseModel):
    text: str = ""


class MCPResult(BaseModel):
    is_error: bool = Field(default=False, alias="isError")
    structured_content: dict[str, Any] | None = Field(default=None, alias="structuredContent")
    content: tuple[MCPText, ...] = ()


class KnowledgeEvidence(Contract):
    evidence_id: str
    query_hash: str
    result_json: str
    artifact_hash: str


class MCPKnowledgeClient:
    def __init__(self, endpoint: str, tenant_id: str, actor_tokens: dict[str, str], development: bool = False) -> None:
        if not actor_tokens or any(not token for token in actor_tokens.values()):
            raise ValueError("Explicit actor-bound knowledge credentials required")
        self.endpoint, self.tenant_id = endpoint, tenant_id
        self.actor_tokens, self.development = actor_tokens, development

    async def _rpc(self, token: str, payload: dict[str, object], session: str | None = None) -> tuple[dict[str, Any], str | None]:
        allowed, _, _ = resolve_url_target(self.endpoint, allow_loopback=self.development)
        if not allowed:
            raise KnowledgeUnavailableError("Knowledge endpoint denied")
        headers = {"Authorization": "Bearer "+token, "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-06-18"}
        if session:
            headers["Mcp-Session-Id"] = session
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", self.endpoint, json=payload, headers=headers) as response:
                if response.status_code not in {200, 202}:
                    raise KnowledgeUnavailableError("Knowledge authentication/service denied")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1024*1024:
                        raise KnowledgeUnavailableError("Knowledge response exceeds bound")
                session_id = response.headers.get("Mcp-Session-Id", session)
                if session_id and (len(session_id) > 128 or not session_id.isascii()):
                    raise KnowledgeUnavailableError("Invalid knowledge session handle")
                if not body:
                    return {}, session_id
                text = body.decode()
                envelopes = [RPCMessage.model_validate_json(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")] if "text/event-stream" in response.headers.get("Content-Type", "") else [RPCMessage.model_validate_json(text)]
                for item in envelopes:
                    if item.id == payload.get("id"):
                        return item.model_dump(), session_id
                raise KnowledgeUnavailableError("Knowledge RPC response missing")

    async def search(self, query: str, tenant_id: str, actor_id: str) -> KnowledgeResult:
        if tenant_id != self.tenant_id or actor_id not in self.actor_tokens or not 1 <= len(query.strip()) <= 4000:
            raise KnowledgeUnavailableError("Knowledge scope denied")
        token = self.actor_tokens[actor_id]
        try:
            _, session = await self._rpc(token, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "testpilot", "version": "1"}}})
            await self._rpc(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
            envelope, _ = await self._rpc(token, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "search", "arguments": {"query": query, "top_k": 8}}}, session)
            if envelope.get("error"):
                raise KnowledgeUnavailableError("Knowledge RPC failed")
            result = MCPResult.model_validate(envelope.get("result", {}))
            if result.is_error:
                raise KnowledgeUnavailableError("Knowledge retrieval failed")
            structured = result.structured_content
            if structured is None:
                structured = json.loads("".join(part.text for part in result.content))
            return KnowledgeResult.model_validate(structured).model_copy(update={"artifact_hash": None})
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise KnowledgeUnavailableError("Knowledge unavailable; no invented evidence") from None


def evidence(result: KnowledgeResult, artifact_hash: str) -> KnowledgeEvidence:
    return KnowledgeEvidence(evidence_id="kb_"+digest(result.model_dump_json().encode())[:32],
                             query_hash=digest(result.query.encode()), result_json=result.model_dump_json(),
                             artifact_hash=artifact_hash)
