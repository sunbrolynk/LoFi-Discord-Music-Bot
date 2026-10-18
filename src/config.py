"""Configuration loader for the LoFi Discord Music Bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_RADIO_URL = "https://streams.ilovemusic.de/iloveradio17.mp3"


@dataclass(frozen=True)
class BotConfig:
    """Immutable runtime configuration loaded from environment variables."""

    discord_bot_token: str
    spotify_client_id: str
    spotify_client_secret: str
    default_fallback_stream_url: str
    database_path: Path
    log_level: str


def load_config() -> BotConfig:
    """Load and validate configuration from environment variables."""
    load_dotenv()

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    fallback_url = os.getenv("DEFAULT_FALLBACK_STREAM_URL", DEFAULT_RADIO_URL).strip()
    db_path_raw = os.getenv("DATABASE_PATH", "data/lofi_bot.db").strip()
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
        database_path=Path(db_path_raw),
        log_level=log_level,
    )
