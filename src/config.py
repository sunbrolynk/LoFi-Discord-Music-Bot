"""Configuration loader for the LoFi Discord Music Bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_RADIO_URL = "https://lofi.radio"
DEFAULT_DATABASE_PATH = "data/lofi_bot.db"


@dataclass(frozen=True)
class BotConfig:
    """Immutable runtime configuration loaded from environment variables."""

    discord_bot_token: str
    spotify_client_id: str
    spotify_client_secret: str
    default_fallback_stream_url: str
    database_path: Path
    log_level: str

    @property
    def log_dir(self) -> Path:
        """Directory for the rotating log files, beside the database."""
        return self.database_path.parent / "logs"

    @property
    def heartbeat_path(self) -> Path:
        """File the bot touches while healthy; read by the container healthcheck."""
        return heartbeat_path_for(self.database_path)


def heartbeat_path_for(database_path: Path) -> Path:
    """Return the heartbeat file kept beside the database."""
    return database_path.parent / "heartbeat"


def database_path_from_env() -> Path:
    """Return the SQLite path from DATABASE_PATH."""
    return Path(os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH).strip())


def load_config() -> BotConfig:
    """Load and validate configuration from environment variables."""
    load_dotenv()

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    fallback_url = os.getenv("DEFAULT_FALLBACK_STREAM_URL", DEFAULT_RADIO_URL).strip()
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()

    missing: list[str] = []
    if not token:
        missing.append("DISCORD_BOT_TOKEN")
    if not client_id:
        missing.append("SPOTIFY_CLIENT_ID")
    if not client_secret:
        missing.append("SPOTIFY_CLIENT_SECRET")

    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Missing required environment variable(s): {joined}")

    return BotConfig(
        discord_bot_token=token,
        spotify_client_id=client_id,
        spotify_client_secret=client_secret,
        default_fallback_stream_url=fallback_url or DEFAULT_RADIO_URL,
        database_path=database_path_from_env(),
        log_level=log_level,
    )
