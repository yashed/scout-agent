from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

from config import settings

logger = logging.getLogger(__name__)


def _server_configs() -> dict[str, dict[str, Any]]:
    """Build MultiServerMCPClient configs from the URL/API-key pairs Agent
    Manager injects for each attached MCP Tool Configuration. A proxy is
    skipped if its URL env var is unset (e.g. not attached yet).

    Both `API-Key` and `X-API-Key` are sent since AMP's own docs are
    inconsistent about which header its gateway expects (the proxy's
    Security tab defaults to `X-API-Key`, but the Tool Configuration
    integration snippet uses `API-Key`) — sending both is harmless.
    """
    proxies = {
        "fetch": (settings.fetch_mcp_url, settings.fetch_mcp_api_key),
        "deepwiki": (settings.deepwiki_mcp_url, settings.deepwiki_mcp_api_key),
    }
    configs: dict[str, dict[str, Any]] = {}
    for name, (url, api_key) in proxies.items():
        logger.info(
            "MCP proxy '%s': url=%s api_key=%s",
            name,
            "set" if url else "unset",
            "set" if api_key else "unset",
        )
        if not url:
            continue
        configs[name] = {
            "url": url,
            "transport": "streamable_http",
            "headers": {"API-Key": api_key, "X-API-Key": api_key} if api_key else {},
        }
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
        try:
            tools = await MultiServerMCPClient({name: config}).get_tools()
        except Exception:
            logger.exception("Failed to load tools from MCP proxy '%s'", name)
            return []
        logger.info("Loaded %d tool(s) from MCP proxy '%s'", len(tools), name)
        return tools

    results = await asyncio.gather(*(_load_one(name, cfg) for name, cfg in configs.items()))
    return [tool for tools in results for tool in tools]
