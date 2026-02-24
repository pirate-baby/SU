"""
Application configuration using Pydantic Settings.
"""
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    claude_code_oauth_token: Optional[str] = None
    self_iteration_mode: bool = False

    # Playwright MCP Bridge (optional — chat works without it)
    playwright_mcp_url: Optional[str] = None

    # ElevenLabs Voice Mode
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_tts_model: str = "eleven_flash_v2_5"

    # Web Push (VAPID)
    vapid_private_key: Optional[str] = None
    vapid_public_key: Optional[str] = None
    vapid_claims_email: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


# Global settings instance
settings = Settings()
