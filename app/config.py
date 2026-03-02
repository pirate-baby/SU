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

    claude_code_oauth_token: Optional[str] = None
    self_iteration_mode: bool = False

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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


# Global settings instance
settings = Settings()
