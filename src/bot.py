"""Main entrypoint for the LoFi Discord Music Bot."""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord.ext import commands, tasks

from src.audio_manager import AudioManager
from src.cogs.music_commands import MusicCommandsCog
from src.config import BotConfig, load_config
from src.database import DatabaseManager
from src.spotify_client import SpotifyResolver

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE_MAX_BYTES = 5_000_000
LOG_FILE_BACKUPS = 5


def configure_logging(level: str, log_dir: Path) -> None:
    """Log to stdout and to a rotating file in `log_dir`; stdout alone if it isn't writable."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    formatter = logging.Formatter(LOG_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_error: OSError | None = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_dir / "bot.log",
                maxBytes=LOG_FILE_MAX_BYTES,
                backupCount=LOG_FILE_BACKUPS,
                encoding="utf-8",
            )
        )
    except OSError as exc:
        file_error = exc
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    if file_error is not None:
        logger.warning("File logging disabled, %s is not writable: %s", log_dir, file_error)


class LoFiBot(commands.Bot):
    """Discord bot client managing multi-server LoFi music playback."""

    def __init__(self, config: BotConfig) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

        self.config = config
        self.db = DatabaseManager(
            db_path=config.database_path,
            default_fallback_url=config.default_fallback_stream_url,
        )
        self.spotify = SpotifyResolver(
            client_id=config.spotify_client_id,
            client_secret=config.spotify_client_secret,
        )
        self.audio = AudioManager(bot=self, db=self.db)
        self._sessions_restored = False

    async def setup_hook(self) -> None:
        """Initialize Opus, the SQLite database, register cogs, and sync slash commands."""
        if not discord.opus.is_loaded():
            discord.opus.load_opus("libopus.so.0")
        logger.info("Opus loaded: %s", discord.opus.is_loaded())

        await self.db.init_db()
        await self.add_cog(
            MusicCommandsCog(
                bot=self,
                db=self.db,
                spotify=self.spotify,
                audio=self.audio,
            )
        )
        synced = await self.tree.sync()
        logger.info("Synced %d slash command(s) globally.", len(synced))
        self.watchdog.start()

    async def on_ready(self) -> None:
        """Log readiness and, on the first ready only, resume playback from before a restart."""
        if self.user:
            logger.info("LoFi Bot logged in as %s (ID: %s)", self.user, self.user.id)
        if not self._sessions_restored:
            self._sessions_restored = True
            await self.audio.restore_sessions()

    @tasks.loop(seconds=30)
    async def watchdog(self) -> None:
        """Restart silent playback and keep the healthcheck heartbeat fresh while healthy."""
        try:
            playback_ok = self.audio.watchdog_tick(time.monotonic())
            if self.is_ready() and not self.is_closed() and playback_ok:
                self.config.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
                self.config.heartbeat_path.touch()
        except Exception:
            logger.exception("Watchdog tick failed.")


def main() -> None:
    """Load config, configure logging, and start the Discord bot."""
    config = load_config()
    configure_logging(config.log_level, config.log_dir)
    bot = LoFiBot(config=config)
    bot.run(config.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
