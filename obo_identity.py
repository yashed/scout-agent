"""OBO (On-Behalf-Of) token exchange for platform-hosted agents.

This is a plain, from-scratch implementation of RFC 8693 OAuth 2.0 Token
Exchange against ThunderID's token endpoint — it deliberately does not depend
on the `amp_identity` runtime package (see
`.worktrees/obo-direct/*/libs/amp-identity`); it exists so Scout's own code
demonstrates the wire protocol end to end.

The request shape below (parameter names, which ones ThunderID actually
honors, the Basic-auth client authentication) is confirmed against ThunderID's
own grant handler
(`backend/internal/oauth/oauth2/granthandlers/token_exchange.go` and
`backend/internal/oauth/oauth2/constants/constants.go` in the thunderid repo),
not assumed from the RFC alone:

- ``resource`` (RFC 8707) is the parameter ThunderID resolves against its
  registered resource servers. It must be the *same* resource identifier
  already used for this agent's own client_credentials calls (see
  ``agent_identity.py``) — for Scout that's each MCP proxy's configured URL.
  ``audience`` is accepted on the wire but explicitly NOT honored by the
  token-exchange grant handler, so this never sends it.
- ``scope`` is not optional in practice: when the trusted issuer's
  authorization is mapping-based (an "authorization mapping" rule, exactly
  what Agent Manager's Connections page configures) and ``scope`` is omitted,
  ThunderID's handler treats the request as asking for nothing and grants
  nothing. Which scopes to request per resource is configured below via
  ``<PROXY>_OBO_SCOPES`` env vars.
- The caller's own token (the ``subject_token``) must be the live request's
  forwarded ``Authorization: Bearer <token>`` header — this only exists when
  Agent Manager's "Invocation security is OAuth" + "Token forwarding" are
  both enabled for this agent (see the Agent ID -> Delegated Access readiness
  checklist in the console).

Challenge-and-retry (RFC 9728): a single exchange is not the whole story. If
the MCP gateway rejects a call because the delegated token lacks a scope, it
is expected to answer with::

    403 Forbidden
    WWW-Authenticate: Bearer error="insufficient_scope",
      scope="orders:read",
      resource_metadata="https://gateway.example.com/orders/.well-known/oauth-protected-resource"

``DelegatedTokenAuth`` reacts to exactly that: it fetches and validates the
advertised protected-resource metadata, re-exchanges the *original* caller
token (never the token it already minted — there is no token-of-a-token
chaining in this design) for the resource/scope the challenge named, and
retries the call once. This means the ``<PROXY>_OBO_SCOPES`` env vars below
are a starting guess, not a requirement: leaving them unset still works as
long as the gateway's challenge is correct, which is the one thing this
module cannot guarantee — see the caveat below.

Known caveat, carried over from the platform's own design doc rather than
discovered here: as of this writing the MCP gateway policy has a documented,
not-yet-confirmed-fixed bug where it reports ``invalid_token`` instead of
``insufficient_scope`` for this exact missing-scope case. This module treats
both error codes as a possible scope challenge so it recovers either way;
a genuinely invalid/expired subject token still fails safely, since that
response carries no usable ``resource_metadata`` to retry against.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from config import settings

logger = logging.getLogger(__name__)

TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

# ThunderID error codes that mean the exchange request reached the server and
# was well-formed, but this caller simply has no path to the requested
# access (no trusted issuer, no matching mapping, scope not granted) — an
# expected, fail-closed denial, not a transport failure or a bug in this
# agent's code.
_DENIAL_ERROR_CODES = frozenset({"access_denied", "invalid_target", "invalid_scope", "invalid_request"})

# MCP-gateway challenge error codes worth reacting to. insufficient_scope is
# the RFC 9728-correct code for "right token, wrong scope"; invalid_token is
# included because of the documented, not-yet-confirmed-fixed gateway bug
# described in the module docstring. Anything else (or a challenge with no
# resource_metadata) is returned to the caller as-is, not retried.
_CHALLENGE_ERROR_CODES = frozenset({"insufficient_scope", "invalid_token"})

_ERROR_BODY_SNIPPET_LEN = 200


class UntrustedMetadataError(Exception):
    """RFC 9728 protected-resource metadata failed to fetch or validate."""


@dataclass(frozen=True)
class _Challenge:
    error: str | None
    scope: str | None
    resource_metadata: str | None


def _parse_www_authenticate(header_value: str) -> _Challenge:
    """Parse a ``WWW-Authenticate: Bearer error=..., scope=..., resource_metadata=...``
    challenge header. Tolerant of quoting/spacing differences between servers."""
    if not header_value:
        return _Challenge(error=None, scope=None, resource_metadata=None)
    _, _, rest = header_value.partition(" ")
    params: dict[str, str] = {}
    for part in rest.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        params[key.strip().lower()] = value.strip().strip('"')
    return _Challenge(
        error=params.get("error"),
        scope=params.get("scope"),
        resource_metadata=params.get("resource_metadata"),
    )


class MissingSubjectTokenError(Exception):
    """Raised when an OBO exchange is attempted with no caller token in scope.

    This is a hard failure by design: silently falling back to the agent's
    own client_credentials identity would defeat the point of delegated
    access (the call would run as the agent, not as the caller), so there is
    no fallback path here.
    """


class TokenExchangeError(Exception):
    """The token endpoint rejected or failed the exchange."""

    def __init__(self, message: str, *, status_code: int | None = None, error_code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class DelegationDeniedError(TokenExchangeError):
    """The exchange was well-formed but denied: no trust/mapping/scope path."""


# ---------------------------------------------------------------------------
# Per-request subject token context
#
# Scout builds its MCP tool clients once at process startup (see
# mcp_tools.py), but the caller's token is only known per /chat request. A
# ContextVar bridges the two: app.py sets it for the lifetime of one
# agent_graph.ainvoke(...) call, and DelegatedTokenAuth (below) reads it
# whenever LangGraph's ToolNode actually makes an MCP call, however deep in
# the call stack that happens. ContextVar values propagate automatically into
# tasks created from within the `with` block (asyncio copies the current
# context at task-creation time), so this holds even through LangGraph's
# internal async scheduling.
# ---------------------------------------------------------------------------
_current_subject_token: ContextVar[str | None] = ContextVar("obo_subject_token", default=None)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Parse an inbound ``Authorization: Bearer <token>`` header.

    Returns None (not an error) for a missing header or a non-Bearer scheme —
    callers decide whether that's fine (a conversational message that never
    touches a tool) or fatal (a tool call is attempted with no caller
    identity available, see MissingSubjectTokenError).
    """
    if not authorization_header:
        return None
    scheme, _, value = authorization_header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


@contextmanager
def subject_token_scope(token: str | None):
    """Make ``token`` the active caller identity for every OBO exchange made
    inside this block, in this task and any task spawned from inside it."""
    reset: Token[str | None] = _current_subject_token.set(token)
    try:
        yield
    finally:
        _current_subject_token.reset(reset)


def current_subject_token() -> str | None:
    return _current_subject_token.get()


def obo_enabled() -> bool:
    """Whether this deployment should attempt delegated (OBO) access at all.

    Reuses the same AMP_AGENTID_* credentials/endpoint as the existing
    client_credentials path (agent_identity.py): token exchange authenticates
    as this agent's own AgentID exactly like client_credentials does, it only
    changes the grant_type and adds the caller's subject_token.
    """
    from agent_identity import agent_identity_configured

    return settings.obo_enabled and agent_identity_configured()


class DelegatedTokenAuth(httpx.Auth):
    """Attaches a delegated (OBO) bearer token to every MCP request for one
    resource, exchanging the current request's caller token for one scoped
    to that resource via RFC 8693 token exchange.

    One instance per MCP proxy, same as AgentIdentityAuth, and for the same
    reason: a token exchange binds to one target resource server, so proxies
    cannot share a cache.

    There is deliberately no fallback to the agent's own identity: a tool
    call made without an active caller token raises MissingSubjectTokenError
    rather than silently running as the agent instead of the user.
    """

    def __init__(self, resource: str, scopes: Sequence[str] = ()) -> None:
        self._resource = resource
        self._scopes = tuple(scopes)
        # Cache keyed by (hash of caller token, resource, scopes): the same
        # caller re-invoking the same tool within the token's lifetime reuses
        # the exchanged token instead of re-exchanging on every call. The
        # subject token itself is hashed, never stored or logged, since it's
        # a live caller credential, not this agent's own static secret.
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self._get_delegated_token(self._resource, self._scopes)
        logger.info(
            "[OBO] attaching delegated Bearer token to request -> %s %s (resource=%s)",
            request.method, request.url, self._resource,
        )
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        if response.status_code not in (401, 403):
            return

        challenge = _parse_www_authenticate(response.headers.get("WWW-Authenticate", ""))
        if challenge.error not in _CHALLENGE_ERROR_CODES or not challenge.resource_metadata:
            # Not a scope challenge this knows how to act on (e.g. a genuinely
            # invalid/expired token with no resource_metadata) — surface the
            # original response rather than guessing at a retry.
            return

        logger.warning(
            "[OBO] got a %s challenge (error=%s) for resource='%s' — fetching its protected-resource "
            "metadata and retrying once with the scope/resource it names",
            response.status_code, challenge.error, self._resource,
        )

        try:
            resource, retry_scopes = await self._resolve_challenge(challenge)
        except UntrustedMetadataError:
            logger.exception(
                "[OBO][FAIL] refusing to retry — protected-resource metadata at '%s' failed validation",
                challenge.resource_metadata,
            )
            return

        try:
            new_token = await self._get_delegated_token(resource, retry_scopes)
        except (MissingSubjectTokenError, TokenExchangeError):
            logger.exception("[OBO][FAIL] retry exchange failed for resource='%s'", resource)
            return

        request.headers["Authorization"] = f"Bearer {new_token}"
        yield request

    async def _resolve_challenge(self, challenge: _Challenge) -> tuple[str, Sequence[str]]:
        """Validate the challenge's RFC 9728 metadata and return the
        (resource, scopes) to re-exchange for.

        Refuses the metadata unless it is HTTPS, valid JSON, and advertises
        exactly the resource this instance already targets — a resource
        server cannot use its own challenge to redirect this agent into
        minting a token for some other resource.
        """
        metadata_url = challenge.resource_metadata or ""
        if urlsplit(metadata_url).scheme != "https":
            raise UntrustedMetadataError(f"Refusing a non-HTTPS resource_metadata URL: {metadata_url!r}")

        async with httpx.AsyncClient(timeout=10) as client:
            try:
                response = await client.get(metadata_url)
            except httpx.HTTPError as exc:
                raise UntrustedMetadataError(f"Failed to fetch resource_metadata: {exc}") from exc

        if response.status_code != 200:
            raise UntrustedMetadataError(f"resource_metadata endpoint returned {response.status_code}")

        try:
            data = response.json()
        except ValueError as exc:
            raise UntrustedMetadataError("resource_metadata response was not valid JSON") from exc

        advertised_resource = data.get("resource") if isinstance(data, dict) else None
        if advertised_resource != self._resource:
            raise UntrustedMetadataError(
                f"resource_metadata advertises resource {advertised_resource!r}, expected {self._resource!r}"
            )

        retry_scopes: Sequence[str] = (challenge.scope,) if challenge.scope else self._scopes
        return advertised_resource, retry_scopes

    async def _get_delegated_token(self, resource: str, scopes: Sequence[str]) -> str:
        subject_token = current_subject_token()
        if not subject_token:
            logger.error(
                "[OBO][FAIL] no caller token in scope for resource='%s' — refusing to fall back to this "
                "agent's own identity. Is Agent Manager forwarding the caller's token? (Agent ID -> "
                "Delegated Access -> 'Token forwarding is enabled')",
                resource,
            )
            raise MissingSubjectTokenError(
                f"OBO exchange for resource {resource!r} attempted with no caller token in scope"
            )

        cache_key = self._cache_key(subject_token, resource, scopes)

        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() < cached[1]:
                logger.info(
                    "[OBO] delegated token cache HIT for resource='%s' (expires in %.1fs)",
                    resource, cached[1] - time.monotonic(),
                )
                return cached[0]

            logger.info(
                "[OBO] exchanging caller token for a delegated token — resource='%s' scopes=%s",
                resource, scopes,
            )
            access_token, expiry = await self._exchange(subject_token, resource, scopes)
            self._cache[cache_key] = (access_token, expiry)
            return access_token

    @staticmethod
    def _cache_key(subject_token: str, resource: str, scopes: Sequence[str]) -> str:
        digest = hashlib.sha256(subject_token.encode("utf-8")).hexdigest()
        return f"{digest}|{resource}|{','.join(sorted(scopes))}"

    async def _exchange(self, subject_token: str, resource: str, scopes: Sequence[str]) -> tuple[str, float]:
        form: dict[str, str] = {
            "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "resource": resource,
        }
        if scopes:
            form["scope"] = " ".join(scopes)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    settings.agentid_token_endpoint,
                    data=form,
                    auth=(settings.agentid_client_id, settings.agentid_client_secret),
                )
        except httpx.TimeoutException as exc:
            logger.error("[OBO][FAIL] token endpoint timed out for resource='%s'", resource)
            raise TokenExchangeError(f"Token endpoint timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            logger.error("[OBO][FAIL] token endpoint request failed for resource='%s': %s", resource, exc)
            raise TokenExchangeError(f"Token endpoint request failed: {exc}") from exc

        if response.status_code != 200:
            raise self._error_for_response(response, resource)

        try:
            payload = response.json()
        except ValueError as exc:
            raise TokenExchangeError("Token endpoint response was not valid JSON") from exc

        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not access_token or not isinstance(expires_in, (int, float)):
            raise TokenExchangeError("Token endpoint response is missing access_token/expires_in")

        # Refresh at 75% of the token's lifetime, matching agent_identity.py's
        # existing client_credentials cache so both auth paths behave the same way.
        expiry = time.monotonic() + (expires_in * 0.75)
        logger.info(
            "[OBO][OK] delegated token minted for resource='%s' scope='%s' expires_in=%s",
            resource, payload.get("scope"), expires_in,
        )
        return access_token, expiry

    def _error_for_response(self, response: httpx.Response, resource: str) -> TokenExchangeError:
        error_code: str | None = None
        error_description: str | None = None
        try:
            body = response.json()
            if isinstance(body, dict):
                error_code = body.get("error")
                error_description = body.get("error_description")
        except ValueError:
            pass

        detail = error_description or (response.text or "")[:_ERROR_BODY_SNIPPET_LEN]
        logger.error(
            "[OBO][FAIL] token endpoint returned %s for resource='%s': error=%s description=%s",
            response.status_code, resource, error_code, detail,
        )

        if error_code in _DENIAL_ERROR_CODES:
            return DelegationDeniedError(
                f"Delegated access denied for resource {resource!r} "
                f"(error={error_code!r}): {detail}. Check: the AgentID's token-exchange grant is "
                "enabled, a trusted issuer connection is enabled for this environment, and it has an "
                "authorization mapping covering this caller's token.",
                status_code=response.status_code,
                error_code=error_code,
            )
        return TokenExchangeError(
            f"Token endpoint returned {response.status_code}: {detail}",
            status_code=response.status_code,
            error_code=error_code,
        )
