import logging
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # LLM routing flag. When USE_LLM_PROVIDER=true the agent routes chat
    # completions through the Agent Manager LLM proxy / AI gateway
    # (LLM_PROVIDER_URL + LLM_PROVIDER_KEY, OpenAI-compatible). When false
    # (the default) it calls OpenAI directly with OPENAI_API_KEY.
    use_llm_provider: bool = Field(default=False, validation_alias="USE_LLM_PROVIDER")
    llm_provider_url: str = Field(default="", validation_alias="LLM_PROVIDER_URL")
    llm_provider_key: str = Field(default="", validation_alias="LLM_PROVIDER_KEY")

    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", validation_alias="OPENAI_MODEL")
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

    # AgentID credentials, auto-injected by Agent Manager into every
    # platform-hosted agent's pod (see docs:
    # tutorials/use-agentid-in-platform-hosted-agents). Used to mint bearer
    # tokens for MCP gateway calls; left unset when running locally or as an
    # externally-hosted agent, in which case AgentID auth is skipped.
    agentid_client_id: str = Field(default="", validation_alias="AMP_AGENTID_CLIENT_ID")
    agentid_client_secret: str = Field(default="", validation_alias="AMP_AGENTID_CLIENT_SECRET")
    agentid_token_endpoint: str = Field(default="", validation_alias="AMP_AGENTID_TOKEN_ENDPOINT")
    agentid_scopes: str = Field(default="", validation_alias="AMP_AGENTID_SCOPES")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()


# ---------------------------------------------------------------------------
# Debug helpers — intentionally prints secrets for troubleshooting.
# Remove or gate behind a DEBUG flag before shipping to production.
# ---------------------------------------------------------------------------

def dump_all_env_vars() -> None:
    """Log every OS environment variable (including secrets) for debugging."""
    logger.info("=" * 70)
    logger.info("[DEBUG] ALL ENVIRONMENT VARIABLES (including secrets)")
    logger.info("=" * 70)
    env_vars = dict(os.environ)
    if not env_vars:
        logger.info("  (no environment variables found)")
    else:
        for key in sorted(env_vars.keys()):
            logger.info("  %s = %s", key, env_vars[key])
    logger.info("=" * 70)


def dump_settings() -> None:
    """Log all parsed settings values (including secrets) for debugging."""
    logger.info("=" * 70)
    logger.info("[DEBUG] PARSED SETTINGS (from env / .env file)")
    logger.info("=" * 70)
    logger.info("  use_llm_provider   = %s", settings.use_llm_provider)
    logger.info("  llm_provider_url   = %s", settings.llm_provider_url if settings.llm_provider_url else "(unset)")
    logger.info("  llm_provider_key   = %s", settings.llm_provider_key if settings.llm_provider_key else "(unset)")
    logger.info("  openai_api_key     = %s", settings.openai_api_key)
    logger.info("  openai_model       = %s", settings.openai_model)
    logger.info("  openai_timeout     = %s", settings.openai_timeout)
    logger.info("  openai_max_retries = %s", settings.openai_max_retries)
    logger.info("  FETCH_URL                  = %s", settings.fetch_mcp_url if settings.fetch_mcp_url else "(unset)")
    logger.info("  FETCH_API_KEY              = %s", settings.fetch_mcp_api_key if settings.fetch_mcp_api_key else "(unset)")
    logger.info("  DEEPWIKI_URL               = %s", settings.deepwiki_mcp_url if settings.deepwiki_mcp_url else "(unset)")
    logger.info("  DEEPWIKI_API_KEY           = %s", settings.deepwiki_mcp_api_key if settings.deepwiki_mcp_api_key else "(unset)")
    logger.info("  AMP_AGENTID_CLIENT_ID      = %s", settings.agentid_client_id if settings.agentid_client_id else "(unset)")
    logger.info("  AMP_AGENTID_CLIENT_SECRET  = %s", settings.agentid_client_secret if settings.agentid_client_secret else "(unset)")
    logger.info("  AMP_AGENTID_TOKEN_ENDPOINT = %s", settings.agentid_token_endpoint if settings.agentid_token_endpoint else "(unset)")
    logger.info("  AMP_AGENTID_SCOPES         = %s", settings.agentid_scopes if settings.agentid_scopes else "(unset)")
    logger.info("=" * 70)

    # Quick validation checks
    logger.info("[DEBUG] SETTINGS VALIDATION CHECKS")
    if settings.use_llm_provider:
        logger.info("  [OK]    USE_LLM_PROVIDER is true — routing through the Agent Manager LLM proxy")
        if settings.llm_provider_url:
            logger.info("  [OK]    LLM_PROVIDER_URL is set -> %s", settings.llm_provider_url)
        else:
            logger.warning("  [FAIL]  LLM_PROVIDER_URL is MISSING — proxy calls will fail")
        if settings.llm_provider_key:
            logger.info("  [OK]    LLM_PROVIDER_KEY is set")
        else:
            logger.warning("  [FAIL]  LLM_PROVIDER_KEY is MISSING — proxy calls will 401")
    else:
        logger.info("  [INFO]  USE_LLM_PROVIDER is false — calling OpenAI directly")
        if settings.openai_api_key:
            logger.info("  [OK]    OPENAI_API_KEY is set")
        else:
            logger.warning("  [FAIL]  OPENAI_API_KEY is MISSING — agent will not be able to call OpenAI")

    if settings.fetch_mcp_url:
        logger.info("  [OK]    FETCH_URL is set -> %s", settings.fetch_mcp_url)
    else:
        logger.info("  [WARN]  FETCH_URL is not set — Fetch MCP proxy will be skipped")

    if settings.fetch_mcp_api_key:
        logger.info("  [OK]    FETCH_API_KEY is set")
    else:
        logger.info("  [WARN]  FETCH_API_KEY is not set")

    if settings.deepwiki_mcp_url:
        logger.info("  [OK]    DEEPWIKI_URL is set -> %s", settings.deepwiki_mcp_url)
    else:
        logger.info("  [WARN]  DEEPWIKI_URL is not set — DeepWiki MCP proxy will be skipped")

    if settings.deepwiki_mcp_api_key:
        logger.info("  [OK]    DEEPWIKI_API_KEY is set")
    else:
        logger.info("  [WARN]  DEEPWIKI_API_KEY is not set")

    # AgentID validation
    agentid_all_set = all([
        settings.agentid_client_id,
        settings.agentid_client_secret,
        settings.agentid_token_endpoint,
    ])
    if agentid_all_set:
        logger.info("  [OK]    AgentID credentials fully set — Bearer token auth will be used for MCP")
    else:
        missing = []
        if not settings.agentid_client_id:      missing.append("AMP_AGENTID_CLIENT_ID")
        if not settings.agentid_client_secret:   missing.append("AMP_AGENTID_CLIENT_SECRET")
        if not settings.agentid_token_endpoint:  missing.append("AMP_AGENTID_TOKEN_ENDPOINT")
        logger.warning(
            "  [WARN]  AgentID credentials NOT set (missing: %s) — "
            "Bearer token auth will be SKIPPED. MCP calls may fail with 401 if the gateway requires it.",
            ", ".join(missing),
        )

    logger.info("=" * 70)
