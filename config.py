from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
