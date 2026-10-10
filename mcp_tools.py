from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent_identity import AgentIdentityAuth, agent_identity_configured
from config import settings
from obo_identity import DelegatedTokenAuth, obo_enabled

logger = logging.getLogger(__name__)

# Shared cache mapping: one auth instance per resource (proxy handle). Which
# class populates this depends on settings.obo_enabled (see _server_configs):
# AgentIdentityAuth mints this agent's own client_credentials token;
# DelegatedTokenAuth exchanges the current request's caller token instead.
_mcp_auths: dict[str, httpx.Auth] = {}


def _server_configs() -> dict[str, dict[str, Any]]:
    """Build MultiServerMCPClient configs from the URL/API-key pairs Agent
    Manager injects for each attached MCP Tool Configuration. A proxy is
    skipped if its URL env var is unset (e.g. not attached yet).

    Both `API-Key` and `X-API-Key` are sent since AMP's own docs are
    inconsistent about which header its gateway expects (the proxy's
    Security tab defaults to `X-API-Key`, but the Tool Configuration
    integration snippet uses `API-Key`) — sending both is harmless.

    When AgentID credentials have been injected (platform-hosted agents,
    see agent_identity.py), each proxy's client also gets an `auth` that
    attaches a live `Authorization: Bearer` token to every request, minted
    and refreshed on demand.

    When AMP_OBO_ENABLED is also true, that `auth` is DelegatedTokenAuth
    instead (see obo_identity.py): each MCP call exchanges the current
    /chat request's caller token for one scoped to that proxy, rather than
    using this agent's own client_credentials identity.
    """
    proxies = {
        "fetch": (settings.fetch_mcp_url, settings.fetch_mcp_api_key, settings.fetch_obo_scopes),
        "deepwiki": (settings.deepwiki_mcp_url, settings.deepwiki_mcp_api_key, settings.deepwiki_obo_scopes),
    }

    logger.info("=" * 70)
    logger.info("[DEBUG] MCP CONFIGURATION CHECK")
    logger.info("=" * 70)

    identity_ready = agent_identity_configured()
    use_obo = obo_enabled()
    logger.info(
        "[DEBUG] AgentID credentials: %s",
        "[OK] SET — Bearer token auth will be attached to MCP requests" if identity_ready
        else "[WARN] NOT SET — Bearer token auth will be skipped (MCP may fail with 401)",
    )
    logger.info(
        "[DEBUG] Auth mode: %s",
        "OBO (delegated, per-caller token exchange)" if use_obo
        else "client_credentials (this agent's own static identity)",
    )

    configs: dict[str, dict[str, Any]] = {}
    for name, (url, api_key, obo_scopes_raw) in proxies.items():
        # Log the actual values (including secrets) for debugging
        logger.info(
            "[DEBUG] MCP proxy '%s': url='%s' | api_key='%s'",
            name,
            url if url else "(unset/empty)",
            api_key if api_key else "(unset/empty)",
        )
        if url:
            logger.info("  [OK]   '%s' URL is configured correctly -> %s", name, url)
        else:
            logger.warning(
                "  [SKIP] '%s' MCP proxy has NO URL configured — this proxy will be skipped. "
                "Check that the environment variable is set (FETCH_URL / DEEPWIKI_URL).",
                name,
            )

        if api_key:
            logger.info("  [OK]   '%s' API key is present (value: %s)", name, api_key)
        else:
            logger.info("  [WARN] '%s' API key is empty or unset", name)

        if not url:
            continue

        config: dict[str, Any] = {
            "url": url,
            "transport": "streamable_http",
            "headers": {"API-Key": api_key, "X-API-Key": api_key} if api_key else {},
        }
        if use_obo:
            if name not in _mcp_auths:
                scopes = obo_scopes_raw.split() if obo_scopes_raw else ()
                _mcp_auths[name] = DelegatedTokenAuth(resource=url, scopes=scopes)
            config["auth"] = _mcp_auths[name]
            logger.info(
                "  [OK]   '%s' OBO delegated-token auth attached (scopes=%s)",
                name, obo_scopes_raw or "(none requested)",
            )
        elif identity_ready:
            if name not in _mcp_auths:
                _mcp_auths[name] = AgentIdentityAuth(resource=url)
            config["auth"] = _mcp_auths[name]
            logger.info("  [OK]   '%s' AgentID Bearer auth attached", name)
        else:
            logger.info("  [WARN] '%s' no AgentID Bearer auth — running without Authorization header", name)

        configs[name] = config

    logger.info("  Total MCP proxies configured: %d / %d", len(configs), len(proxies))
    logger.info("=" * 70)
    return configs


async def load_mcp_tools() -> list:
    """Discover tools from every configured MCP proxy independently, so one
    proxy failing (auth error, downtime, etc.) doesn't drop tools from the
    others. Returns whatever tools loaded successfully — an empty list if
    none are configured or all fail — so the agent always starts, at worst
    with native tools only.
    """
    configs = _server_configs()
    if not configs:
        logger.warning(
            "No MCP proxies configured (FETCH_URL / DEEPWIKI_URL unset); "
            "starting with native tools only."
        )
        return []

    async def _load_one(name: str, config: dict[str, Any]) -> list:
        logger.info("[DEBUG] Attempting to connect to MCP proxy '%s' at url='%s' ...", name, config.get("url"))
        try:
            tools = await MultiServerMCPClient({name: config}).get_tools()
        except Exception as exc:
            # Unwrap ExceptionGroup (Python 3.11+ / anyio TaskGroup) to surface
            # the real underlying error instead of just "unhandled errors in a TaskGroup".
            sub_exceptions = getattr(exc, "exceptions", None)
            if sub_exceptions:
                logger.error(
                    "[FAIL] MCP proxy '%s' tool discovery FAILED (ExceptionGroup with %d sub-error(s)) "
                    "— proxy url='%s'",
                    name,
                    len(sub_exceptions),
                    config.get("url"),
                )
                for i, sub_exc in enumerate(sub_exceptions, start=1):
                    logger.error(
                        "[FAIL]   sub-error #%d: %s: %s",
                        i,
                        type(sub_exc).__name__,
                        sub_exc,
                    )
            else:
                logger.exception(
                    "[FAIL] MCP proxy '%s' tool discovery FAILED — proxy url='%s' | %s: %s",
                    name,
                    config.get("url"),
                    type(exc).__name__,
                    exc,
                )
            return []
        tool_names = [getattr(t, "name", str(t)) for t in tools]
        logger.info(
            "[OK]   MCP proxy '%s' tool discovery PASSED — loaded %d tool(s): %s",
            name,
            len(tools),
            tool_names,
        )
        logger.info("Loaded %d tool(s) from MCP proxy '%s': %s", len(tools), name, tool_names)
        return tools

    results = await asyncio.gather(*(_load_one(name, cfg) for name, cfg in configs.items()))
    all_tools = [tool for tools in results for tool in tools]
    logger.info(
        "[DEBUG] MCP tool loading complete — total tools loaded across all proxies: %d",
        len(all_tools),
    )
    return all_tools
