"""Bearer identity adapters; no actor/tenant/project is accepted from task JSON."""

import hmac
from typing import Any, Protocol

import jwt
from pydantic import BaseModel, Field

from testpilot.task_api import Principal


class AuthenticationError(RuntimeError):
    pass


class IdentityProvider(Protocol):
    async def authenticate(self, authorization: str) -> Principal: ...


class StaticIdentity:
    def __init__(self, tokens: dict[str, Principal]) -> None:
        if not tokens or any(len(token) < 32 for token in tokens):
            raise ValueError("strong development tokens required")
        self.tokens = tokens

    async def authenticate(self, authorization: str) -> Principal:
        for token, principal in self.tokens.items():
            if hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
                return principal
        raise AuthenticationError("Authentication required")


class IdentityClaims(BaseModel):
    sub: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    projects: tuple[str, ...] = Field(min_length=1, max_length=32)
    roles: tuple[str, ...] = Field(default=(), max_length=32)
    groups: tuple[str, ...] = Field(default=(), max_length=64)


class JWTIdentity:
    """RS256 only, configured issuer/audience/JWKS. Ignores token URLs and rejects ambiguity.

    Trusted JWKS is supplied by the operator/IdP adapter, never by token jku/x5u.
    Identity snapshots may be rotated via replace_keys without restarting streams.
    """

    def __init__(self, issuer: str, audience: str, jwks: dict[str, Any]) -> None:
        self.issuer, self.audience = issuer, audience
        self.keys = jwt.PyJWKSet.from_dict(jwks)
        self.revoked: set[str] = set()

    def replace_keys(self, jwks: dict[str, Any]) -> None:
        self.keys = jwt.PyJWKSet.from_dict(jwks)

    async def authenticate(self, authorization: str) -> Principal:
        if not authorization.startswith("Bearer ") or len(authorization) > 16384:
            raise AuthenticationError("Authentication required")
        token = authorization[7:]
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or any(key in header for key in ("jku", "x5u", "jwk")):
                raise AuthenticationError("Token header denied")
            keys = [key for key in self.keys.keys if key.key_id == header.get("kid") and key.algorithm_name == "RS256"]
            if len(keys) != 1:
                raise AuthenticationError("Signing key unavailable")
            payload = jwt.decode(token, keys[0].key, algorithms=["RS256"], issuer=self.issuer, audience=self.audience,
                                 options={"require": ["iss", "aud", "exp", "iat", "nbf", "sub", "tenant_id", "jti"]})
            if payload["jti"] in self.revoked:
                raise AuthenticationError("Token revoked")
            claims = IdentityClaims.model_validate(payload)
            return Principal(claims.sub, claims.tenant_id, claims.roles, claims.projects, claims.groups)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise AuthenticationError("Token invalid or expired") from None


def require_role(principal: Principal, role: str, project: str = "gateway-fixture") -> None:
    if project not in principal.projects or (role not in principal.roles and "admin" not in principal.roles):
        raise PermissionError("Role/project permission required")
