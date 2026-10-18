"""Playback state machine tests against fake voice objects: replacement, recovery, restore."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import discord
import pytest
from src import audio_manager
from src.audio_manager import AudioManager, backoff_delay
from src.database import DatabaseManager, TrackMetadata

REAL_SLEEP = asyncio.sleep
GUILD_ID = 42
VOICE_ID = 7


class FakeVoiceClient(discord.VoiceClient):
    def __init__(self, channel: Any) -> None:  # noqa: D107
        self.channel = channel
        self.connected = True
        self.playing = False
        self.plays: list[str] = []
        self._after: Callable[[Exception | None], None] | None = None

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self.playing

    def is_paused(self) -> bool:
        return False

    def play(self, source: Any, *, after: Any = None, **_: Any) -> None:  # type: ignore[override]
        self.plays.append(source.url)
        self.playing = True
        self._after = after

    def finish(self, error: Exception | None = None) -> None:
        """End the current source the way discord.py's player thread does."""
        self.playing = False
        after, self._after = self._after, None
        if after is not None:
            after(error)

    def stop(self) -> None:
        self.finish()

    async def disconnect(self, *, force: bool = False) -> None:
        self.connected = False
        self.playing = False


class FakeVoiceChannel(discord.VoiceChannel):
    def __init__(self, guild: FakeGuild) -> None:  # noqa: D107
        self.id = VOICE_ID
        self.fake_guild = guild
        self.connects = 0

    async def connect(self, **_: Any) -> FakeVoiceClient:  # type: ignore[override]
        self.connects += 1
        client = FakeVoiceClient(self)
        self.fake_guild.voice_client = client
        return client


class FakeGuild:
    def __init__(self) -> None:
        self.id = GUILD_ID
        self.name = "Test Server"
        self.voice_client: FakeVoiceClient | None = None
        self.channel = FakeVoiceChannel(self)

    def get_channel(self, channel_id: int) -> Any:
        return self.channel if channel_id == VOICE_ID else None


class FakeBot:
    def __init__(self, guild: FakeGuild) -> None:
        self.loop = asyncio.get_running_loop()
        self.guilds = [guild]
        self._guild = guild

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self._guild if guild_id == self._guild.id else None


class FakeSource:
    def __init__(self, url: str, **_: Any) -> None:
        self.url = url


class FakeVolume:
    def __init__(self, source: FakeSource, volume: float) -> None:
        self.url = source.url
        self.volume = volume


@pytest.fixture
async def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(audio_manager.discord, "FFmpegPCMAudio", FakeSource)
    monkeypatch.setattr(audio_manager.discord, "PCMVolumeTransformer", FakeVolume)
    monkeypatch.setattr(audio_manager, "backoff_delay", lambda *a, **k: 0.0)

    async def no_sleep(_: float) -> None:
        await REAL_SLEEP(0)

    monkeypatch.setattr(audio_manager.asyncio, "sleep", no_sleep)

    db = DatabaseManager(tmp_path / "bot.db", default_fallback_url="https://radio.example/a.mp3")
    await db.init_db()
    await db.set_channels(GUILD_ID, voice_channel_id=VOICE_ID, control_channel_id=99)
    await db.add_tracks(
        GUILD_ID,
        [TrackMetadata(f"https://open.spotify.com/track/{n}", f"Song {n}", "Cat") for n in "ABC"],
        added_by="mod",
    )
    guild = FakeGuild()
    manager = AudioManager(bot=FakeBot(guild), db=db)  # type: ignore[arg-type]

    async def fake_resolve(query: str) -> str:
        return f"https://cdn.example/{query.split(' - ')[-1].replace(' ', '_')}"

    async def reachable(url: str, recheck: bool = False) -> bool:
        return True

    monkeypatch.setattr(manager, "_extract_stream_url", fake_resolve)
    monkeypatch.setattr(manager, "_stream_reachable", reachable)
    yield manager, guild, db
    for task in [*manager._background, *manager._recovery_tasks.values()]:
        task.cancel()


async def settle() -> None:
    """Let spawned handlers, which wait on SQLite worker threads, run to completion."""
    await REAL_SLEEP(0.3)


async def test_backoff_delay_grows_and_caps() -> None:
    assert [backoff_delay(n) for n in range(4)] == [5.0, 10.0, 20.0, 40.0]
    assert backoff_delay(20) == 300.0
    assert backoff_delay(3, base=2.0, cap=10.0) == 10.0


async def test_skip_replaces_track_without_a_runaway_chain(setup: Any) -> None:
    manager, guild, db = setup
    await manager.play_playlist_index(guild, index=0)
    await manager.skip(guild)
    await settle()

    # The stopped track's after-callback must not advance the playlist a second time
    assert guild.voice_client.plays == ["https://cdn.example/Song_A", "https://cdn.example/Song_B"]
    assert (await db.get_guild_settings(GUILD_ID)).current_index == 1


async def test_natural_track_end_advances(setup: Any) -> None:
    manager, guild, _ = setup
    await manager.play_playlist_index(guild, index=0)
    guild.voice_client.finish()
    await settle()
    assert guild.voice_client.plays[-1] == "https://cdn.example/Song_B"


async def test_voice_drop_mid_track_reconnects_and_resumes(setup: Any) -> None:
    manager, guild, _ = setup
    await manager.play_playlist_index(guild, index=0)
    dead = guild.voice_client
    dead.connected = False
    dead.finish()
    await settle()

    assert guild.voice_client is not dead
    assert guild.channel.connects == 2
    assert guild.voice_client.plays == ["https://cdn.example/Song_A"]


async def test_handler_exception_schedules_recovery(setup: Any, monkeypatch: Any) -> None:
    manager, guild, _ = setup
    await manager.play_playlist_index(guild, index=0)
    calls = {"n": 0}
    real = manager.play_playlist_index

    async def flaky(g: Any, index: int | None = None) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise discord.ClientException("Not connected to voice.")
        await real(g, index)

    monkeypatch.setattr(manager, "play_playlist_index", flaky)
    guild.voice_client.finish()
    await settle()
    assert calls["n"] == 2
    assert guild.voice_client.is_playing()


async def test_stop_cancels_recovery_and_ignores_stopped_source(setup: Any) -> None:
    manager, guild, db = setup
    await manager.play_playlist_index(guild, index=0)
    await manager.stop_and_disconnect(guild)
    await settle()
    assert guild.channel.connects == 1
    assert (await db.get_guild_settings(GUILD_ID)).autoplay is False
    assert manager.watchdog_tick(now=10_000.0) is True


async def test_restore_sessions_resumes_after_restart(setup: Any, tmp_path: Path) -> None:
    manager, guild, db = setup
    await manager.play_playlist_index(guild, index=1)

    restarted_guild = FakeGuild()
    restarted = AudioManager(bot=FakeBot(restarted_guild), db=db)  # type: ignore[arg-type]
    restarted._extract_stream_url = manager._extract_stream_url  # type: ignore[method-assign]
    restarted._stream_reachable = manager._stream_reachable  # type: ignore[method-assign]
    await restarted.restore_sessions()
    await settle()
    assert restarted_guild.voice_client is not None
    assert restarted_guild.voice_client.plays == ["https://cdn.example/Song_B"]


async def test_restore_clears_stale_fallback_flag_when_not_playing(setup: Any) -> None:
    manager, guild, db = setup
    await db.set_fallback_state(GUILD_ID, on_fallback=True, reason="old")
    await manager.restore_sessions()
    await settle()
    assert (await db.get_guild_settings(GUILD_ID)).on_fallback is False
    assert guild.voice_client is None


async def test_watchdog_recovers_silent_guild_and_reports_unhealthy(setup: Any) -> None:
    manager, guild, _ = setup
    await manager.play_playlist_index(guild, index=0)
    guild.voice_client.playing = False  # silent without any callback, e.g. a hung stream
    assert manager.watchdog_tick(now=0.0) is True
    assert manager.watchdog_tick(now=audio_manager.UNHEALTHY_IDLE_SECONDS) is False
    await settle()
    assert guild.voice_client.is_playing()
    assert manager.watchdog_tick(now=audio_manager.UNHEALTHY_IDLE_SECONDS + 1) is True


async def test_resolve_failure_falls_back_to_radio(setup: Any, monkeypatch: Any) -> None:
    manager, guild, db = setup

    async def blocked(query: str) -> str:
        raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(manager, "_extract_stream_url", blocked)
    await manager.play_playlist_index(guild, index=0)
    assert (await db.get_guild_settings(GUILD_ID)).on_fallback is True
    assert guild.voice_client.plays == ["https://radio.example/a.mp3"]


async def test_fallback_restart_does_not_loop(setup: Any) -> None:
    manager, guild, _ = setup
    await manager.start_fallback(guild, announce=False)
    await manager.start_fallback(guild, announce=False)
    await settle()
    assert len(guild.voice_client.plays) == 2


async def test_search_queue_plays_before_playlist_continues(setup: Any) -> None:
    manager, guild, _ = setup
    await manager.play_playlist_index(guild, index=0)
    slot = await manager.enqueue(guild, TrackMetadata("", "Queued Song", "Dog"))
    assert slot == 1
    guild.voice_client.finish()
    await settle()
    assert guild.voice_client.plays[-1] == "https://cdn.example/Queued_Song"
    guild.voice_client.finish()
    await settle()
    assert guild.voice_client.plays[-1] == "https://cdn.example/Song_B"


async def test_search_plays_immediately_when_idle(setup: Any) -> None:
    manager, guild, _ = setup
    assert await manager.enqueue(guild, TrackMetadata("", "Now Song", "Dog")) == 0
    assert guild.voice_client.plays == ["https://cdn.example/Now_Song"]


async def test_backoff_delay_caps_huge_attempts() -> None:
    assert backoff_delay(5000) == 300.0


async def test_idle_search_resumes_saved_cursor_without_skipping(setup: Any) -> None:
    manager, guild, _ = setup
    await manager.enqueue(guild, TrackMetadata("", "Now Song", "Dog"))
    guild.voice_client.finish()
    await settle()
    assert guild.voice_client.plays[-1] == "https://cdn.example/Song_A"


async def test_unplayable_queued_song_moves_on(setup: Any, monkeypatch: Any) -> None:
    manager, guild, db = setup
    await db.update_fallback_config(GUILD_ID, triggers="manual_only")
    resolve = manager._extract_stream_url

    async def picky(query: str) -> str:
        if "Broken" in query:
            raise RuntimeError("HTTP Error 404")
        return str(await resolve(query))

    monkeypatch.setattr(manager, "_extract_stream_url", picky)
    monkeypatch.setattr(manager, "notify_control_channel", lambda *a, **k: asyncio.sleep(0))
    await manager.play_playlist_index(guild, index=0)
    await manager.enqueue(guild, TrackMetadata("", "Broken Song", "Dog"))
    await manager.enqueue(guild, TrackMetadata("", "Good Song", "Dog"))
    guild.voice_client.finish()
    await settle()
    assert guild.voice_client.plays[-1] == "https://cdn.example/Good_Song"


async def test_recovery_leaves_already_recovered_playback_alone(setup: Any) -> None:
    manager, guild, _ = setup
    await manager.play_playlist_index(guild, index=0)
    manager.schedule_recovery(guild, "test")
    await settle()
    assert guild.voice_client.plays == ["https://cdn.example/Song_A"]


async def test_recovery_waits_out_an_in_flight_lookup(setup: Any, monkeypatch: Any) -> None:
    manager, guild, _ = setup
    release = asyncio.Event()
    lookups: list[str] = []
    resolve = manager._extract_stream_url

    async def slow(query: str) -> str:
        lookups.append(query)
        await release.wait()
        return str(await resolve(query))

    monkeypatch.setattr(manager, "_extract_stream_url", slow)
    play = asyncio.create_task(manager.play_playlist_index(guild, index=0))
    await settle()
    manager.schedule_recovery(guild, "silent while the lookup runs")
    await settle()
    release.set()
    await play
    await settle()
    assert lookups == ["Cat - Song A"]
    assert guild.voice_client.plays == ["https://cdn.example/Song_A"]


async def test_stale_fallback_callback_leaves_the_new_station_alone(setup: Any) -> None:
    manager, guild, db = setup
    await manager.start_fallback(guild, announce=False)
    real_settings = db.get_guild_settings

    async def replaced_meanwhile(guild_id: int) -> Any:
        manager._generation[guild_id] += 1
        manager._fallback_playing[guild_id] = "https://radio.example/new.mp3"
        return await real_settings(guild_id)

    db.get_guild_settings = replaced_meanwhile
    await manager._handle_fallback_track_finished(guild, error=RuntimeError("old stream"))
    assert manager._dead_streams == {}
