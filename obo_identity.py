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
  nothing.
- The caller's own token (the ``subject_token``) must be the live request's
  forwarded ``Authorization: Bearer <token>`` header — this only exists when
  Agent Manager's "Invocation security is OAuth" + "Token forwarding" are
  both enabled for this agent (see the Agent ID -> Delegated Access readiness
  checklist in the console).

Discovery-first (RFC 9728), corrected 2026-10-12: an earlier version of this
module pre-decided which scope to request per MCP resource from a
``<PROXY>_OBO_SCOPES`` env var, exchanged *before* ever calling the resource,
and treated a 401/403 challenge as a fallback for when that guess was wrong.
Live testing proved that path never actually gets exercised in practice — the
pre-known scope is always already right, so the gateway never has a reason to
challenge it, which also means the gateway's own challenge behavior was never
verified. Background research into MCP's own Authorization specification
(https://modelcontextprotocol.io/specification/draft/basic/authorization)
confirmed the mechanism was backwards: discovery-by-challenge is the *normal*
MCP path, not recovery. Its "Scope Selection Strategy" section gives a client
exactly two ways to learn what scope to request — the challenge's own
``scope`` parameter, or else the resource's ``scopes_supported`` — with no
"ask an admin to pre-configure it" step at all.

``DelegatedTokenAuth`` now follows that flow directly:

1. Attempt the call with this agent's own AgentID identity (the same
   client_credentials token ``agent_identity.py`` would use). An operation
   that doesn't actually require delegation — listing available tools,
   reading something this agent's own role already covers — just succeeds
   here, with no exchange at all.
2. A 401/403 proves this specific operation needs delegation. Fetch and
   validate the challenge's RFC 9728 protected-resource metadata, read the
   required scope off the challenge (or the metadata's ``scopes_supported``
   as a fallback), and exchange the *caller's* token — never this agent's own
   — for exactly that resource/scope.
3. Retry with the delegated token. If the gateway comes back with a second
   ``insufficient_scope`` challenge (a genuine step-up: the first delegated
   token was correctly scoped for the first challenge, but this particular
   operation needs more), union the newly-named scope with what was already
   held, re-exchange once more, and retry — then stop, bounded, win or lose.

Nothing here pre-declares a resource's required scope anywhere, and nothing
needs to: it is read off the response every time. A caller token's already-
exchanged scope set is remembered per resource for the rest of its lifetime
(see ``_known_scopes``) purely as a round-trip-saving cache, not as a second
source of truth — it is tried first but discarded the moment the gateway
disagrees with it.

Known caveat, carried over from the platform's own design doc rather than
discovered here: as of this writing the MCP gateway policy has a documented,
not-yet-confirmed-fixed bug where it reports ``invalid_token`` instead of
``insufficient_scope`` for a missing-scope case. This module treats both
error codes (and a bare 401/403 with no ``error`` parameter at all, which is
what MCP's own spec shows for a plain "no token" challenge) as a reactable
challenge, so it recovers regardless of which one actually comes back; a
genuinely invalid/expired subject token still fails safely, since that
response carries no usable ``resource_metadata`` to retry against.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from agent_identity import AgentIdentityAuth
from config import settings

logger = logging.getLogger(__name__)

TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"

# Deliberately the generic RFC 8693 "this is a JWT" identifier, not
# urn:ietf:params:oauth:token-type:access_token. Confirmed against
# ThunderID's own grant handler (token_exchange.go's validateAccessTokenType):
# that function enforces the RFC 9068 `at+jwt`/`application/at+jwt` typ
# header on the subject token, but ONLY when subject_token_type is declared
# as access_token — every other validation step (issuer trust, audience,
# signature, claims/authorization mapping) behaves identically regardless of
# which valid type is declared. Real external IdPs commonly don't set that
# header: Okta's org authorization server uses its own internal typ value,
# and Okta's custom authorization servers (the kind documented for
# third-party API access tokens) omit typ entirely, both confirmed live
# against a real Okta tenant. Declaring access_token here would make every
# caller token from either kind of Okta authorization server fail with
# "not an access token (missing at+jwt typ header)" despite being a
# perfectly valid, correctly signed JWT — declaring it as the generic jwt
# type describes what the token actually is and lets ThunderID's identical
# downstream validation run as intended.
SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"

# ThunderID error codes that mean the exchange request reached the server and
# was well-formed, but this caller simply has no path to the requested
# access (no trusted issuer, no matching mapping, scope not granted) — an
# expected, fail-closed denial, not a transport failure or a bug in this
# agent's code.
_DENIAL_ERROR_CODES = frozenset({"access_denied", "invalid_target", "invalid_scope", "invalid_request"})

# MCP-gateway challenge error codes worth reacting to, alongside a bare
# challenge with no `error` parameter at all (MCP's own spec shows exactly
# that for a plain "no token" 401). insufficient_scope is the RFC
# 9728-correct code for "right token, wrong scope"; invalid_token is included
# because of the documented, not-yet-confirmed-fixed gateway bug described in
# the module docstring. Anything else (or a challenge with no
# resource_metadata) is returned to the caller as-is, not retried.
_CHALLENGE_ERROR_CODES = frozenset({None, "insufficient_scope", "invalid_token"})

# Bounded step-up retries beyond the first discovery exchange (see MCP's own
# Step-Up Authorization Flow: "retry ... no more than a few times"). Total
# request attempts per call is this plus two: the own-identity probe and the
# first delegated exchange.
_MAX_STEP_UPS = 1

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


# Hosts where plain HTTP is trusted for fetching RFC 9728 protected-resource
# metadata, mirroring this platform's own established HTTPS-with-an-explicit-
# local-exception policy (see the OBO plan doc's "Require HTTPS ... Local
# HTTP exceptions must be explicit development configuration") rather than
# inventing a separate rule for this one check. ".localhost" is the RFC 6761
# reserved special-use TLD this entire local deployment already resolves to
# loopback everywhere (*.amp.localhost, *.gateway.localhost, etc.) — the same
# reasoning OAuth's own loopback exception for native apps (RFC 8252) relies
# on: traffic to these hosts never leaves the local machine, so HTTPS buys no
# protection a network attacker could otherwise defeat.
_LOOPBACK_METADATA_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_trusted_metadata_scheme(metadata_url: str) -> bool:
    parsed = urlsplit(metadata_url)
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname in _LOOPBACK_METADATA_HOSTS or hostname.endswith(".localhost")


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

# ---------------------------------------------------------------------------
# Per-request OBO failure signal
#
# LangGraph's ToolNode catches every exception a tool raises (including
# MissingSubjectTokenError / DelegationDeniedError / TokenExchangeError) and
# turns it into a ToolMessage the model then narrates in prose — useful for
# ordinary tool failures ("that URL doesn't exist"), but it means a genuine
# authorization failure would otherwise reach the caller as free-form text in
# a 200 OK response, not the "stable authorization_required or access_denied
# result" delegated-access callers need to depend on programmatically.
#
# This can NOT be a second ContextVar the way _current_subject_token is: the
# actual MCP HTTP call (and so record_obo_failure) runs inside a TaskGroup
# LangGraph's tool execution spawns internally (that's what "unhandled errors
# in a TaskGroup" in every logged failure actually is) — asyncio/anyio only
# copy a ContextVar's value DOWN into a child task at spawn time, never back
# UP when a child task calls .set(); a value set from inside that child task
# would simply vanish with it. current_subject_token() only ever reads inside
# the child task, so that asymmetry never bit it, but recording a failure
# needs the opposite direction.
#
# So the signal is a plain dict, keyed by a fresh id generated per request —
# plain dict writes ARE visible across task boundaries, since there's only
# one of it, not a per-task copy. The id itself still travels down via a
# ContextVar (read-only, the direction that does work), and request_key's own
# value is what ties a failure recorded in some child task back to the one
# /chat call it belongs to — this also means two concurrent calls that
# happen to share a client-supplied session_id (e.g. a double-submitted
# request) still can't cross-contaminate each other's failure, since each
# gets its own fresh key regardless of session_id.
# ---------------------------------------------------------------------------
_current_request_key: ContextVar[str | None] = ContextVar("obo_request_key", default=None)
_obo_failures_by_request: dict[str, BaseException] = {}


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
    inside this block, in this task and any task spawned from inside it.

    Yields this request's unique key — callers that want to check
    ``last_obo_failure`` after the block need it, since the failure itself
    isn't readable through a ContextVar (see the module-level comment above).
    Always drops this request's own entry from the failure dict on exit,
    whether or not it was ever read, so a request nobody checks never leaks.
    """
    request_key = str(uuid.uuid4())
    reset: Token[str | None] = _current_subject_token.set(token)
    key_reset: Token[str | None] = _current_request_key.set(request_key)
    try:
        yield request_key
    finally:
        _current_subject_token.reset(reset)
        _current_request_key.reset(key_reset)
        _obo_failures_by_request.pop(request_key, None)


def current_subject_token() -> str | None:
    return _current_subject_token.get()


def record_obo_failure(exc: BaseException) -> None:
    """Record the most recent OBO-layer failure for the request currently in
    subject_token_scope, for app.py's /chat handler to check (via
    last_obo_failure(request_key)) after the graph finishes. Called at each
    of this module's raise sites, right before raising — never a substitute
    for raising, since ToolNode still needs the real exception to produce its
    own tool-error message for the model to see and react to in-context.

    A no-op outside any subject_token_scope (current_request_key() is None)
    — the one-time startup discovery call is the only place that's true, and
    it never reaches a raise site that needs a subject token in the first
    place (see DelegatedTokenAuth: a discovery call that succeeds with this
    agent's own identity never touches the subject-token path at all), so
    this is just a defensive guard, not a path anything is expected to
    actually take.
    """
    request_key = _current_request_key.get()
    if request_key is None:
        logger.warning(
            "[OBO][WARN] record_obo_failure called with no active request — this failure won't be "
            "surfaced as a stable result, only as whatever the model narrates: %s", exc,
        )
        return
    _obo_failures_by_request[request_key] = exc


def last_obo_failure(request_key: str) -> BaseException | None:
    """The OBO failure recorded for request_key (the value subject_token_scope
    yielded for this request), if any — see record_obo_failure. Consumes it:
    a second call for the same key returns None."""
    return _obo_failures_by_request.pop(request_key, None)


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
    """Attaches the right bearer token to every MCP request for one resource,
    discovering per call whether delegation is even needed and, when it is,
    exchanging the current request's caller token for one scoped to exactly
    what the gateway's own challenge named (RFC 8693 + RFC 9728 — see the
    module docstring for the full flow and why it changed on 2026-10-12).

    One instance per MCP proxy, same as AgentIdentityAuth, and for the same
    reason: a token exchange binds to one target resource server, so proxies
    cannot share a cache.

    There is deliberately no fallback past a genuine delegation requirement:
    once a 401/403 proves this call needs the caller's own identity, a
    missing caller token raises MissingSubjectTokenError rather than silently
    running as the agent instead of the user.
    """

    def __init__(self, resource: str) -> None:
        self._resource = resource
        self._own_identity = AgentIdentityAuth(resource=resource)
        # Cache keyed by (hash of caller token, resource, scopes): the same
        # caller re-invoking the same tool within the token's lifetime reuses
        # the exchanged token instead of re-exchanging on every call. The
        # subject token itself is hashed, never stored or logged, since it's
        # a live caller credential, not this agent's own static secret.
        self._cache: dict[str, tuple[str, float]] = {}
        # Remembers, per caller, the scope set that last worked for this
        # resource — purely a round-trip-saving shortcut so a second tool
        # call in the same (or a later) turn can go straight to a delegated
        # token instead of re-probing with this agent's own identity and
        # re-running discovery every single time. Never trusted past the
        # gateway's own say-so: the moment it stops working (role/mapping
        # changed, a different operation needs more), async_auth_flow falls
        # straight back into full discovery, same as if nothing were cached.
        self._known_scopes: dict[str, tuple[str, ...]] = {}
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        subject_token = current_subject_token()
        memo_key = self._subject_fingerprint(subject_token) if subject_token else None
        remembered_scopes = self._known_scopes.get(memo_key, ()) if memo_key else ()

        if remembered_scopes:
            logger.info(
                "[OBO] resource='%s' previously required delegation for this caller — skipping the "
                "own-identity probe and exchanging directly for the remembered scope=%s",
                self._resource, remembered_scopes,
            )
            token = await self._get_delegated_token(self._resource, remembered_scopes)
        else:
            token = await self._own_identity.get_token()
            logger.info(
                "[OBO] attempting %s %s with this agent's own identity first (resource=%s)",
                request.method, request.url, self._resource,
            )
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        held_scopes = set(remembered_scopes)
        for _ in range(_MAX_STEP_UPS + 1):
            if response.status_code not in (401, 403):
                return

            await self._evict_cached_token(token)
            challenge = _parse_www_authenticate(response.headers.get("WWW-Authenticate", ""))
            if challenge.error not in _CHALLENGE_ERROR_CODES or not challenge.resource_metadata:
                # Not a challenge this knows how to act on (e.g. a genuinely
                # invalid/expired token with no resource_metadata) — surface
                # the response as-is rather than guessing at a retry.
                return

            logger.warning(
                "[OBO] got a %s challenge (error=%s) for resource='%s' — fetching its protected-resource "
                "metadata to learn what scope delegation actually needs here",
                response.status_code, challenge.error, self._resource,
            )

            try:
                resource, new_scopes = await self._resolve_challenge(challenge)
            except UntrustedMetadataError:
                logger.exception(
                    "[OBO][FAIL] refusing to exchange — protected-resource metadata at '%s' failed validation",
                    challenge.resource_metadata,
                )
                return

            held_scopes |= set(new_scopes)

            try:
                token = await self._get_delegated_token(resource, sorted(held_scopes))
            except (MissingSubjectTokenError, TokenExchangeError):
                logger.exception("[OBO][FAIL] exchange failed for resource='%s' scopes=%s", resource, held_scopes)
                return

            if memo_key:
                self._known_scopes[memo_key] = tuple(sorted(held_scopes))

            request.headers["Authorization"] = f"Bearer {token}"
            response = yield request
        # Retries exhausted (bounded step-up per MCP's own Step-Up
        # Authorization Flow) — whatever the last response was stands.

    async def _resolve_challenge(self, challenge: _Challenge) -> tuple[str, Sequence[str]]:
        """Validate the challenge's RFC 9728 metadata and return the
        (resource, scopes) to exchange for.

        Refuses the metadata unless it is HTTPS (or plain HTTP to a loopback
        host — see _is_trusted_metadata_scheme), valid JSON, and advertises
        exactly the resource this instance already targets — a resource
        server cannot use its own challenge to redirect this agent into
        minting a token for some other resource.

        Scope comes from the challenge's own ``scope`` parameter if present;
        otherwise falls back to the metadata's ``scopes_supported`` — exactly
        MCP's own Scope Selection Strategy priority order. There is no third
        fallback: a challenge naming neither simply yields no scope, and the
        resulting exchange request will ask ThunderID for none.
        """
        metadata_url = challenge.resource_metadata or ""
        if not _is_trusted_metadata_scheme(metadata_url):
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

        if challenge.scope:
            scopes: Sequence[str] = tuple(challenge.scope.split())
        else:
            supported = data.get("scopes_supported") if isinstance(data, dict) else None
            scopes = tuple(s for s in supported if isinstance(s, str)) if isinstance(supported, list) else ()
        return advertised_resource, scopes

    async def _get_delegated_token(self, resource: str, scopes: Sequence[str]) -> str:
        subject_token = current_subject_token()
        if not subject_token:
            logger.error(
                "[OBO][FAIL] no caller token in scope for resource='%s' — refusing to fall back to this "
                "agent's own identity. Is Agent Manager forwarding the caller's token? (Agent ID -> "
                "Delegated Access -> 'Token forwarding is enabled')",
                resource,
            )
            exc = MissingSubjectTokenError(
                f"OBO exchange for resource {resource!r} attempted with no caller token in scope"
            )
            record_obo_failure(exc)
            raise exc

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

    @staticmethod
    def _subject_fingerprint(subject_token: str) -> str:
        return hashlib.sha256(subject_token.encode("utf-8")).hexdigest()

    async def _evict_cached_token(self, token: str) -> None:
        """Remove token from the cache, however it got there — a 401/403
        response is authoritative over the cache's own TTL. Matches by value
        rather than recomputing the cache key, since the exact scope set that
        produced token isn't available at every call site (e.g. this agent's
        own-identity token, which was never in self._cache to begin with —
        a harmless no-op in that case)."""
        async with self._lock:
            stale_keys = [key for key, (cached_token, _) in self._cache.items() if cached_token == token]
            for key in stale_keys:
                del self._cache[key]

    async def _exchange(self, subject_token: str, resource: str, scopes: Sequence[str]) -> tuple[str, float]:
        form: dict[str, str] = {
            "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
            "subject_token": subject_token,
            "subject_token_type": SUBJECT_TOKEN_TYPE,
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
            timeout_exc = TokenExchangeError(f"Token endpoint timed out: {exc}")
            record_obo_failure(timeout_exc)
            raise timeout_exc from exc
        except httpx.HTTPError as exc:
            logger.error("[OBO][FAIL] token endpoint request failed for resource='%s': %s", resource, exc)
            request_exc = TokenExchangeError(f"Token endpoint request failed: {exc}")
            record_obo_failure(request_exc)
            raise request_exc from exc

        if response.status_code != 200:
            denial = self._error_for_response(response, resource)
            record_obo_failure(denial)
            raise denial

        try:
            payload = response.json()
        except ValueError as exc:
            bad_json_exc = TokenExchangeError("Token endpoint response was not valid JSON")
            record_obo_failure(bad_json_exc)
            raise bad_json_exc from exc

        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not access_token or not isinstance(expires_in, (int, float)):
            malformed_exc = TokenExchangeError("Token endpoint response is missing access_token/expires_in")
            record_obo_failure(malformed_exc)
            raise malformed_exc

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
