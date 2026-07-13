from __future__ import annotations

import logging
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

from config import settings

logger = logging.getLogger(__name__)


def _server_configs() -> dict[str, dict[str, Any]]:
    """Build MultiServerMCPClient configs from the URL/API-key pairs Agent
    Manager injects for each attached MCP Tool Configuration. A proxy is
    skipped if its URL env var is unset (e.g. not attached yet).
    """
    proxies = {
        "fetch": (settings.fetch_mcp_url, settings.fetch_mcp_api_key),
        "deepwiki": (settings.deepwiki_mcp_url, settings.deepwiki_mcp_api_key),
    }
    configs: dict[str, dict[str, Any]] = {}
    for name, (url, api_key) in proxies.items():
        if not url:
            continue
        configs[name] = {
            "url": url,
            "transport": "streamable_http",
            "headers": {"API-Key": api_key} if api_key else {},
        }
    return configs


async def load_mcp_tools() -> list:
    """Discover tools from every configured MCP proxy. Returns an empty list
    (never raises) if no proxies are configured or discovery fails, so the
    agent still starts with its native tools.
    """
    configs = _server_configs()
    if not configs:
        logger.warning(
            "No MCP proxies configured (FETCH_URL / DEEPWIKI_URL unset); "
            "starting with native tools only."
        )
        return []
    try:
        client = MultiServerMCPClient(configs)
        tools = await client.get_tools()
    except Exception:
        logger.exception("Failed to load tools from MCP proxies: %s", list(configs))
        return []
    logger.info("Loaded %d tool(s) from MCP proxies %s", len(tools), list(configs))
    return tools
