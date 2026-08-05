import logging
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    openai_timeout: float = Field(
        default=30.0,
        description="Timeout in seconds for OpenAI API calls.",
    )
    openai_max_retries: int = Field(
        default=3,
        description="Maximum retry attempts for OpenAI API calls.",
    )

    # MCP Tool Configuration env vars injected by Agent Manager. Names below
    # match the platform's auto-generated names for proxies called "Fetch"
    # and "DeepWiki" (see Configure -> Tool Configurations on the agent).
    # Rename here if you named the tool configurations differently.
    fetch_mcp_url: str = Field(default="", validation_alias="FETCH_URL")
    fetch_mcp_api_key: str = Field(default="", validation_alias="FETCH_API_KEY")
    deepwiki_mcp_url: str = Field(default="", validation_alias="DEEPWIKI_URL")
    deepwiki_mcp_api_key: str = Field(default="", validation_alias="DEEPWIKI_API_KEY")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()


# ---------------------------------------------------------------------------
# Debug helpers — intentionally prints secrets for troubleshooting.
# Remove or gate behind a DEBUG flag before shipping to production.
# ---------------------------------------------------------------------------

def dump_all_env_vars() -> None:
    """Log every OS environment variable (including secrets) for debugging."""
    logger.debug("=" * 70)
    logger.debug("[DEBUG] ALL ENVIRONMENT VARIABLES (including secrets)")
    logger.debug("=" * 70)
    env_vars = dict(os.environ)
    if not env_vars:
        logger.debug("  (no environment variables found)")
    else:
        for key in sorted(env_vars.keys()):
            logger.debug("  %s = %s", key, env_vars[key])
    logger.debug("=" * 70)


def dump_settings() -> None:
    """Log all parsed settings values (including secrets) for debugging."""
    logger.debug("=" * 70)
    logger.debug("[DEBUG] PARSED SETTINGS (from env / .env file)")
    logger.debug("=" * 70)
    logger.debug("  openai_api_key     = %s", settings.openai_api_key)
    logger.debug("  openai_model       = %s", settings.openai_model)
    logger.debug("  openai_timeout     = %s", settings.openai_timeout)
    logger.debug("  openai_max_retries = %s", settings.openai_max_retries)
    logger.debug("  FETCH_URL          = %s", settings.fetch_mcp_url if settings.fetch_mcp_url else "(unset)")
    logger.debug("  FETCH_API_KEY      = %s", settings.fetch_mcp_api_key if settings.fetch_mcp_api_key else "(unset)")
    logger.debug("  DEEPWIKI_URL       = %s", settings.deepwiki_mcp_url if settings.deepwiki_mcp_url else "(unset)")
    logger.debug("  DEEPWIKI_API_KEY   = %s", settings.deepwiki_mcp_api_key if settings.deepwiki_mcp_api_key else "(unset)")
    logger.debug("=" * 70)

    # Quick validation checks
    logger.debug("[DEBUG] SETTINGS VALIDATION CHECKS")
    if settings.openai_api_key:
        logger.debug("  [OK]    OPENAI_API_KEY is set")
    else:
        logger.warning("  [FAIL]  OPENAI_API_KEY is MISSING — agent will not be able to call OpenAI")

    if settings.fetch_mcp_url:
        logger.debug("  [OK]    FETCH_URL is set -> %s", settings.fetch_mcp_url)
    else:
        logger.debug("  [WARN]  FETCH_URL is not set — Fetch MCP proxy will be skipped")

    if settings.fetch_mcp_api_key:
        logger.debug("  [OK]    FETCH_API_KEY is set")
    else:
        logger.debug("  [WARN]  FETCH_API_KEY is not set")

    if settings.deepwiki_mcp_url:
        logger.debug("  [OK]    DEEPWIKI_URL is set -> %s", settings.deepwiki_mcp_url)
    else:
        logger.debug("  [WARN]  DEEPWIKI_URL is not set — DeepWiki MCP proxy will be skipped")

    if settings.deepwiki_mcp_api_key:
        logger.debug("  [OK]    DEEPWIKI_API_KEY is set")
    else:
        logger.debug("  [WARN]  DEEPWIKI_API_KEY is not set")

    logger.debug("=" * 70)
