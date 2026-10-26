"""Main entrypoint for the LoFi Discord Music Bot."""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import aiohttp
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
NOWPLAYING_TIMEOUT_SECONDS = 10.0
ACTIVITY_NAME_LIMIT = 128


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


def now_playing_label(data: Any) -> str | None:
    """Return "title – artist" from AzuraCast now-playing JSON, or None if it has no title."""
    try:
        song = data["now_playing"]["song"]
        title = str(song.get("title") or "").strip()
        artist = str(song.get("artist") or "").strip()
    except (KeyError, TypeError, AttributeError):
        return None
    if not title:
        return None
    return (f"{title} – {artist}" if artist else title)[:ACTIVITY_NAME_LIMIT]


async def fetch_now_playing(url: str) -> str | None:
    """Fetch the now-playing label from `url`, or None if it can't be read."""
    timeout = aiohttp.ClientTimeout(total=NOWPLAYING_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.debug("Now-playing returned HTTP %s.", resp.status)
                    return None
                data = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
        logger.debug("Now-playing fetch failed: %s", exc)
        return None
    return now_playing_label(data)


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
        # Radio mode retries its own stream instead of switching to another station
        backups = (
            (config.radio_stream_url,) if config.radio_mode else config.backup_fallback_stream_urls
        )
        self.audio = AudioManager(bot=self, db=self.db, backup_streams=backups)
        self._sessions_restored = False
        self._now_playing: str | None = None

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
                radio_mode=self.config.radio_mode,
                radio_channel_id=self.config.radio_channel_id,
                radio_stream_url=self.config.radio_stream_url,
            )
        )
        synced = await self.tree.sync()
        logger.info("Synced %d slash command(s) globally.", len(synced))
        self.watchdog.start()
        if self.config.radio_nowplaying_url:
            self.now_playing.start()

    async def on_ready(self) -> None:
        """Log readiness and, on the first ready only, resume playback from before a restart."""
        if self.user:
            logger.info("LoFi Bot logged in as %s (ID: %s)", self.user, self.user.id)
        if not self._sessions_restored:
            self._sessions_restored = True
            if self.config.radio_mode:
                await self._start_radio()
            else:
                await self.audio.restore_sessions()

    async def _start_radio(self) -> None:
        """Join RADIO_CHANNEL_ID and play RADIO_STREAM_URL there."""
        channel = self.get_channel(self.config.radio_channel_id or 0)
        if not isinstance(channel, discord.VoiceChannel):
            logger.error(
                "RADIO_CHANNEL_ID %s isn't a voice channel this bot can see; radio not started.",
                self.config.radio_channel_id,
            )
            return
        await self.audio.start_radio(channel, self.config.radio_stream_url)

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

    @tasks.loop(seconds=30)
    async def now_playing(self) -> None:
        """Show the radio's current song as the bot's "Listening to" activity."""
        try:
            label = await fetch_now_playing(self.config.radio_nowplaying_url)
            if label and label != self._now_playing:
                await self.change_presence(
                    activity=discord.Activity(type=discord.ActivityType.listening, name=label)
                )
                self._now_playing = label
        except Exception:
            logger.exception("Now-playing update failed.")

    @now_playing.before_loop
    async def _before_now_playing(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    """Load config, configure logging, and start the Discord bot."""
    config = load_config()
    configure_logging(config.log_level, config.log_dir)
    bot = LoFiBot(config=config)
    bot.run(config.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
