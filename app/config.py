"""
Application configuration using Pydantic Settings.
"""
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # User identity (one instance = one user)
    user_name: str = "User"
    user_gender: str = "male"  # male | female | neutral
    su_name: str = "SU"

    # LLM provider: "anthropic" | "together" | "fireworks"
    llm_provider: str = "together"
    llm_model: str = "deepseek-ai/DeepSeek-V3"

    # Provider API keys (only the one matching llm_provider is required)
    anthropic_api_key: Optional[str] = None
    claude_code_oauth_token: Optional[str] = None
    together_api_key: Optional[str] = None
    fireworks_api_key: Optional[str] = None

    self_iteration_mode: bool = False

    # basic-memory MCP sidecar (streamable-http, started by entrypoint.sh)
    basic_memory_mcp_url: str = "http://127.0.0.1:8765/mcp"

    # Playwright MCP Bridge (optional — chat works without it)
    playwright_mcp_url: Optional[str] = None

    # ProtonMail (optional — enables email management via protonmail-mcp-server)
    # Both SMTP and IMAP route through Proton Bridge running as a sidecar container.
    # Password is the Bridge mailbox password, NOT the ProtonMail account password.
    # Get it from: docker compose run --rm proton-bridge setup → info
    protonmail_username: Optional[str] = None
    protonmail_password: Optional[str] = None
    protonmail_smtp_host: str = "proton-bridge"  # Proton Bridge sidecar container
    protonmail_smtp_port: int = 1025             # Proton Bridge SMTP port
    protonmail_imap_host: str = "proton-bridge"  # Proton Bridge sidecar container
    protonmail_imap_port: int = 1143             # Proton Bridge IMAP port

    # ElevenLabs Voice Mode
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_tts_model: str = "eleven_flash_v2_5"

    # Telegram Bot
    telegram_bot_token: Optional[str] = None
    app_host: Optional[str] = None  # e.g. "su.tail1234.ts.net:8000" — for Telegram deep links

    # Deep Learning Mode
    deep_learning_dir: str = "/data/deep-learning"

    # Documents (markdown editor)
    documents_dir: str = "/data/documents"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


# Global settings instance
settings = Settings()
