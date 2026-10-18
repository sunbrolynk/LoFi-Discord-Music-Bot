"""Main entrypoint for the LoFi Discord Music Bot."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from src.audio_manager import AudioManager
from src.cogs.music_commands import MusicCommandsCog
from src.config import BotConfig, load_config
from src.database import DatabaseManager
from src.spotify_client import SpotifyResolver

logger = logging.getLogger(__name__)


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

    async def on_ready(self) -> None:
        """Log bot connection readiness."""
        if self.user:
            logger.info("LoFi Bot logged in as %s (ID: %s)", self.user, self.user.id)


def main() -> None:
    """Load config, configure logging, and start the Discord bot."""
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    bot = LoFiBot(config=config)
    bot.run(config.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
