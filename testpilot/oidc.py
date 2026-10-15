"""OIDC resource-server discovery/key rotation and optional per-request revocation check."""

import asyncio
import base64
import time
from urllib.parse import urlparse

from pydantic import BaseModel

from testpilot.http_transport import IntegrationUnavailableError, SafeHTTP
from testpilot.identity import AuthenticationError, IdentityClaims, JWTIdentity
from testpilot.task_api import Principal


class Discovery(BaseModel):
    issuer: str
    jwks_uri: str


class OIDCIdentity:
    def __init__(self, issuer: str, audience: str, http: SafeHTTP, *, ttl: float = 300,
                 introspection_url: str | None = None, client_id: str = "", client_secret: str = "") -> None:
        parsed = urlparse(issuer)
        if parsed.scheme not in ({"http", "https"} if http.development else {"https"}) or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("Invalid OIDC issuer")
        self.issuer, self.audience, self.http, self.ttl = issuer.rstrip("/"), audience, http, ttl
        self.introspection_url, self.client_id, self.client_secret = introspection_url, client_id, client_secret
        self.identity: JWTIdentity | None = None
        self.expires_at = 0.0
        self.last_refresh = float("-inf")
        self.lock = asyncio.Lock()

    async def refresh(self, unknown_key: bool = False) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.identity is not None and now < self.expires_at and (not unknown_key or now-self.last_refresh < 5):
                return
            self.last_refresh = now
            discovery = Discovery.model_validate(await self.http.json("GET", self.issuer+"/.well-known/openid-configuration"))
            if discovery.issuer != self.issuer:
                raise AuthenticationError("Discovery issuer mismatch")
            keys = await self.http.json("GET", discovery.jwks_uri)
            self.identity = JWTIdentity(self.issuer, self.audience, keys)
            self.expires_at = now+self.ttl

    async def authenticate(self, authorization: str) -> Principal:
        try:
            await self.refresh()
            assert self.identity is not None
            try:
                principal = await self.identity.authenticate(authorization)
            except AuthenticationError:
                await self.refresh(unknown_key=True)
                assert self.identity is not None
                principal = await self.identity.authenticate(authorization)
            if self.introspection_url:
                encoded = base64.b64encode((self.client_id+":"+self.client_secret).encode()).decode()
                claims = await self.http.json("POST", self.introspection_url, headers={"Authorization": "Basic "+encoded},
                                              form={"token": authorization[7:], "token_type_hint": "access_token"})
                audiences = claims.get("aud", [])
                if isinstance(audiences, str):
                    audiences = [audiences]
                if claims.get("active") is not True or self.audience not in audiences or float(claims.get("exp", 0)) <= time.time():
                    raise AuthenticationError("Token inactive")
                current = IdentityClaims.model_validate(claims)
                if current.sub != principal.user_id or current.tenant_id != principal.tenant_id:
                    raise AuthenticationError("Introspection identity mismatch")
                principal = Principal(current.sub, current.tenant_id, current.roles, current.projects, current.groups)
            return principal
        except (IntegrationUnavailableError, ValueError, TypeError, KeyError):
            raise AuthenticationError("Identity unavailable or invalid") from None
