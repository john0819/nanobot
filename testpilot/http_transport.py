"""Bounded operator-configured HTTP transport; no redirects, proxy inheritance or model URLs."""

import json
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import TypeAdapter

from nanobot.security.network import resolve_url_target


class IntegrationUnavailableError(RuntimeError):
    pass


class SafeHTTP:
    def __init__(self, development: bool = False, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.development, self.transport = development, transport

    async def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                      body: dict[str, object] | None = None, form: dict[str, str] | None = None,
                      limit: int = 4*1024*1024) -> bytes:
        allowed, _, _ = resolve_url_target(url, allow_loopback=self.development)
        if not allowed or (not self.development and urlparse(url).scheme != "https"):
            raise IntegrationUnavailableError("Integration endpoint denied")
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                async with client.stream(method, url, headers=headers, json=body, data=form) as response:
                    if response.status_code not in {200, 201, 202, 204}:
                        raise IntegrationUnavailableError(f"Integration HTTP {response.status_code}")
                    result = bytearray()
                    async for chunk in response.aiter_bytes():
                        result.extend(chunk)
                        if len(result) > limit:
                            raise IntegrationUnavailableError("Integration response exceeds bound")
                    return bytes(result)
        except httpx.HTTPError:
            raise IntegrationUnavailableError("Integration transport unavailable") from None

    async def json(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                   body: dict[str, object] | None = None, form: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            raw = await self.request(method, url, headers=headers, body=body, form=form)
            return TypeAdapter(dict[str, Any]).validate_python(json.loads(raw))
        except (ValueError, TypeError):
            raise IntegrationUnavailableError("Integration response contract invalid") from None
