"""AgentID support for platform-hosted agents.

Agent Manager injects four env vars (AMP_AGENTID_CLIENT_ID/CLIENT_SECRET/
TOKEN_ENDPOINT/SCOPES, see ``config.py``) into every platform-hosted agent's
pod. This module mints and caches an OAuth2 client_credentials access token
from those credentials and exposes it as an ``httpx.Auth`` so
langchain-mcp-adapters attaches a fresh ``Authorization: Bearer`` header to
every MCP gateway call. See:
https://wso2.github.io/agent-manager/docs/next/tutorials/use-agentid-in-platform-hosted-agents/
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator

import httpx

from config import settings

logger = logging.getLogger(__name__)


class AgentIdentityNotProvisionedError(Exception):
    """Raised when AgentID hasn't finished provisioning yet (env vars unset)."""


def agent_identity_configured() -> bool:
    """Whether AMP has injected AgentID credentials into this pod."""
    return bool(
        settings.agentid_client_id
        and settings.agentid_client_secret
        and settings.agentid_token_endpoint
    )


class AgentIdentityAuth(httpx.Auth):
    """Attaches a cached AgentID bearer token to every request, minting or
    refreshing it via the client_credentials grant as needed.

    Share one instance across every MCP proxy's client config so they share
    the token cache instead of each minting their own token.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._expiry = 0.0
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self._get_token()
        logger.debug("[DEBUG] AgentIdentityAuth: attaching Bearer token to request -> %s %s", request.method, request.url)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request

    async def _get_token(self) -> str:
        # The lock both protects the cache and coalesces concurrent refreshes
        # onto a single token-endpoint call: whichever request loses the race
        # blocks here, then finds a fresh cached token already waiting.
        async with self._lock:
            if self._token and time.monotonic() < self._expiry:
                logger.debug("[DEBUG] AgentID token cache HIT — reusing cached token (expires in %.1fs)", self._expiry - time.monotonic())
                return self._token

            if not agent_identity_configured():
                logger.error(
                    "[FAIL] AgentID token generation FAILED — AMP_AGENTID_CLIENT_ID / "
                    "AMP_AGENTID_CLIENT_SECRET / AMP_AGENTID_TOKEN_ENDPOINT are not set. "
                    "AgentID has not finished provisioning yet."
                )
                raise AgentIdentityNotProvisionedError(
                    "AMP_AGENTID_CLIENT_ID/CLIENT_SECRET/TOKEN_ENDPOINT are not set; "
                    "AgentID has not finished provisioning yet."
                )

            logger.debug(
                "[DEBUG] AgentID token generation ATTEMPT — endpoint='%s' client_id='%s' scopes='%s'",
                settings.agentid_token_endpoint,
                settings.agentid_client_id,
                settings.agentid_scopes,
            )

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        settings.agentid_token_endpoint,
                        data={
                            "grant_type": "client_credentials",
                            "scope": settings.agentid_scopes,
                        },
                        auth=(settings.agentid_client_id, settings.agentid_client_secret),
                    )
                    response.raise_for_status()
                    token_data = response.json()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "[FAIL] AgentID token generation FAILED — HTTP %s from token endpoint '%s': %s",
                    exc.response.status_code,
                    settings.agentid_token_endpoint,
                    exc.response.text,
                )
                raise
            except Exception as exc:
                logger.exception(
                    "[FAIL] AgentID token generation FAILED — unexpected error from endpoint '%s': %s",
                    settings.agentid_token_endpoint,
                    exc,
                )
                raise

            self._token = token_data["access_token"]
            # Refresh at 75% of the token's lifetime so we're never caught
            # minting mid-request on a token that expired seconds ago.
            self._expiry = time.monotonic() + (token_data["expires_in"] * 0.75)

            logger.info(
                "Minted a new AgentID access token (expires_in=%s)",
                token_data.get("expires_in"),
            )
            logger.debug(
                "[OK]   AgentID token generation PASSED — access_token='%s' expires_in=%s",
                self._token,
                token_data.get("expires_in"),
            )
            return self._token
