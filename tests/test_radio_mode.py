"""Radio-only mode: config parsing, now-playing parsing, command gating and startup."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from src import bot
from src.audio_manager import RADIO_MODE_REASON, AudioManager
from src.bot import fetch_now_playing, now_playing_label
from src.cogs.music_commands import MusicCommandsCog, blocked_in_radio_mode
from src.config import DEFAULT_BACKUP_STREAM_URLS, load_config
from src.database import DatabaseManager

STREAM = "https://stream.audiobookmanager.com/listen/lofi/radio.mp3"
NOWPLAYING = "https://stream.audiobookmanager.com/api/nowplaying/lofi"


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr("src.config.load_dotenv", lambda: None)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    for name in ("RADIO_STREAM_URL", "RADIO_CHANNEL_ID", "RADIO_NOWPLAYING_URL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_radio_mode_is_off_without_a_stream(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("RADIO_CHANNEL_ID", "123")
    config = load_config()
    assert config.radio_mode is False
    assert config.radio_channel_id is None
    assert config.backup_fallback_stream_urls == DEFAULT_BACKUP_STREAM_URLS


def test_radio_mode_reads_stream_channel_and_nowplaying(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("RADIO_STREAM_URL", f" {STREAM} ")
    base_env.setenv("RADIO_CHANNEL_ID", "123456789012345678")
    base_env.setenv("RADIO_NOWPLAYING_URL", NOWPLAYING)
    config = load_config()
    assert config.radio_mode is True
    assert config.radio_stream_url == STREAM
    assert config.radio_channel_id == 123456789012345678
    assert config.radio_nowplaying_url == NOWPLAYING


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RADIO_CHANNEL_ID", ""),
        ("RADIO_CHANNEL_ID", "lofi-radio"),
        ("RADIO_STREAM_URL", "ftp://example.com/radio.mp3"),
        ("RADIO_NOWPLAYING_URL", "example.com/api"),
    ],
)
def test_radio_mode_rejects_bad_settings(
    base_env: pytest.MonkeyPatch, name: str, value: str
) -> None:
    base_env.setenv("RADIO_STREAM_URL", STREAM)
    base_env.setenv("RADIO_CHANNEL_ID", "123")
    base_env.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        load_config()


def test_now_playing_label_from_azuracast_json() -> None:
    data = {"now_playing": {"song": {"title": "Aurora", "artist": "Purrple Cat"}}}
    assert now_playing_label(data) == "Aurora – Purrple Cat"
    assert now_playing_label({"now_playing": {"song": {"title": "Aurora", "artist": ""}}}) == (
        "Aurora"
    )
    assert len(now_playing_label({"now_playing": {"song": {"title": "x" * 300}}}) or "") == 128


@pytest.mark.parametrize(
    "data",
    [{}, None, [], {"now_playing": None}, {"now_playing": {"song": {"title": "  "}}}],
)
def test_now_playing_label_ignores_missing_song(data: Any) -> None:
    assert now_playing_label(data) is None


class FakeResponse:
    def __init__(self, status: int, data: Any) -> None:
        self.status = status
        self.data = data

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def json(self, content_type: str | None = "application/json") -> Any:
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def fake_session(response: FakeResponse, urls: list[str]) -> Any:
    class FakeSession:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        def get(self, url: str) -> FakeResponse:
            urls.append(url)
            return response

    return FakeSession


async def test_fetch_now_playing(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []
    song = {"now_playing": {"song": {"title": "Aurora", "artist": "Purrple Cat"}}}
    monkeypatch.setattr(bot.aiohttp, "ClientSession", fake_session(FakeResponse(200, song), urls))
    assert await fetch_now_playing(NOWPLAYING) == "Aurora – Purrple Cat"
    assert urls == [NOWPLAYING]

    monkeypatch.setattr(bot.aiohttp, "ClientSession", fake_session(FakeResponse(503, song), urls))
    assert await fetch_now_playing(NOWPLAYING) is None

    bad_json = FakeResponse(200, ValueError("not json"))
    monkeypatch.setattr(bot.aiohttp, "ClientSession", fake_session(bad_json, urls))
    assert await fetch_now_playing(NOWPLAYING) is None


def test_blocked_in_radio_mode_covers_playlist_commands_only() -> None:
    for name in (
        "playlist add",
        "playlist resume",
        "fallback start",
        "search",
        "skip",
        "mode",
        "setup",
    ):
        assert blocked_in_radio_mode(name) is True
    for name in ("play", "pause", "stop", "volume", "status"):
        assert blocked_in_radio_mode(name) is False


def fake_interaction(command_name: str, channel_id: int = 99) -> Any:
    interaction = MagicMock()
    interaction.guild.id = 1
    interaction.channel_id = channel_id
    interaction.command.qualified_name = command_name
    interaction.response.send_message = AsyncMock()
    return interaction


async def make_cog(tmp_path: Path, radio_mode: bool) -> MusicCommandsCog:
    db = DatabaseManager(tmp_path / "bot.db", default_fallback_url=STREAM)
    await db.init_db()
    await db.set_channels(1, voice_channel_id=7, control_channel_id=99)
    return MusicCommandsCog(
        bot=MagicMock(), db=db, spotify=MagicMock(), audio=MagicMock(), radio_mode=radio_mode
    )


async def test_radio_mode_replies_to_playlist_commands(tmp_path: Path) -> None:
    cog = await make_cog(tmp_path, radio_mode=True)
    interaction = fake_interaction("playlist add")
    assert await cog._require_mod_channel(interaction) is False
    reply = interaction.response.send_message.await_args
    assert "radio" in reply.args[0]
    assert reply.kwargs == {"ephemeral": True}

    assert await cog._require_mod_channel(fake_interaction("volume")) is True


async def test_playlist_commands_work_when_radio_mode_is_off(tmp_path: Path) -> None:
    cog = await make_cog(tmp_path, radio_mode=False)
    interaction = fake_interaction("playlist add")
    assert await cog._require_mod_channel(interaction) is True
    interaction.response.send_message.assert_not_awaited()


async def test_start_radio_binds_channel_and_plays_the_stream(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "bot.db", default_fallback_url="https://lofi.radio")
    await db.init_db()
    await db.set_channels(1, voice_channel_id=5, control_channel_id=99)
    manager = AudioManager(bot=MagicMock(), db=db, backup_streams=(STREAM,))
    manager._spawn = MagicMock()  # type: ignore[method-assign]
    manager.start_fallback = MagicMock()  # type: ignore[method-assign]
    channel = MagicMock()
    channel.id = 7
    channel.guild.id = 1

    await manager.start_radio(channel, STREAM)

    settings = await db.get_guild_settings(1)
    assert (settings.voice_channel_id, settings.control_channel_id) == (7, 99)
    assert (settings.fallback_url, settings.fallback_triggers) == (STREAM, "manual_only")
    assert 1 in manager._wanted
    manager.start_fallback.assert_called_once_with(
        channel.guild, reason=RADIO_MODE_REASON, announce=False
    )
    manager._spawn.assert_called_once()


async def test_radio_retries_its_own_stream_after_it_died(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "bot.db", default_fallback_url=STREAM)
    manager = AudioManager(bot=MagicMock(), db=db, backup_streams=(STREAM,))
    manager.mark_stream_dead(STREAM, "stream ended")
    manager._stream_reachable = AsyncMock(return_value=True)  # type: ignore[method-assign]
    assert await manager._resolve_fallback_stream_url(STREAM, use_backup=True) == STREAM


async def test_play_in_radio_mode_uses_only_the_configured_channel_and_stream(
    tmp_path: Path,
) -> None:
    cog = await make_cog(tmp_path, radio_mode=True)
    cog.radio_channel_id, cog.radio_stream_url = 7, STREAM
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.guild.id = 1
    cog.bot.get_channel = MagicMock(return_value=channel)  # type: ignore[method-assign]
    cog.audio.start_radio = AsyncMock()  # type: ignore[method-assign]
    own, other = MagicMock(), MagicMock()
    own.id, other.id = 1, 2

    assert "only plays in its own channel" in await cog._play_radio(other)
    cog.audio.start_radio.assert_not_awaited()

    assert await cog._play_radio(own) == "📻 Playing the radio stream."
    cog.audio.start_radio.assert_awaited_once_with(channel, STREAM)
