"""Per-server radio: /radio commands, per-guild gating, URL safety, now-playing status, startup."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sqlite3
from collections.abc import AsyncIterator
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp import web
from discord import app_commands
from src import audio_manager, radio
from src.audio_manager import AUTO_RADIO_PREFIX, RADIO_MODE_REASON, AudioManager
from src.cogs.music_commands import MusicCommandsCog, blocked_in_radio_mode
from src.config import load_config, parse_trusted_hosts
from src.database import DatabaseManager, TrackMetadata
from src.radio import (
    CHANNEL_STATUS_LIMIT,
    PublicResolver,
    RadioStatus,
    RadioURLError,
    check_public_url,
    fetch_now_playing,
    is_blocked_address,
    now_playing_label,
)
from src.resolver import Resolved

STREAM = "https://stream.example.com/listen/lofi/radio.mp3"
NOWPLAYING = "https://stream.example.com/api/nowplaying/lofi"
SONG = {"now_playing": {"song": {"title": "Aurora", "artist": "Purrple Cat"}}}


async def no_check(_url: str) -> None:
    return None


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    manager = DatabaseManager(tmp_path / "bot.db", default_fallback_url="https://lofi.radio")
    await manager.init_db()
    await manager.set_channels(1, voice_channel_id=5, control_channel_id=99)
    return manager


# URL safety


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.1.2.3",  # private
        "172.16.0.1",
        "192.168.1.10",
        "169.254.169.254",  # link-local, cloud metadata
        "100.64.0.1",  # CGNAT
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "0.0.0.0",  # noqa: S104 - unspecified
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fec0::1",  # site-local
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:192.168.1.1",
        "2002:c0a8:101::1",  # 6to4 of 192.168.1.1
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
    ],
)
async def test_non_public_addresses_are_refused(address: str) -> None:
    assert is_blocked_address(ip_address(address)) is True
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(RadioURLError, match="private network"):
        await check_public_url(f"http://{host}:8000/radio.mp3")


@pytest.mark.parametrize("address", ["93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_blocked_address(ip_address(address)) is False


@pytest.mark.parametrize(
    "url",
    [
        "ftp://stream.example.com/radio.mp3",
        "file:///etc/passwd",
        "stream.example.com/radio.mp3",
        "http://user:pass@stream.example.com/radio.mp3",
        "http://stream.example.com\\@127.0.0.1/radio.mp3",
        "http://stream.example.com /radio.mp3",
        "http:///radio.mp3",
        "http://stream.example.com:99999/radio.mp3",
        "https://" + "a" * 3000 + ".com/",
    ],
)
async def test_bad_urls_are_refused(url: str) -> None:
    with pytest.raises(RadioURLError):
        await check_public_url(url)


def fake_dns(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    def getaddrinfo(*_: Any, **__: Any) -> list[Any]:
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0))
            if ":" in address
            else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            for address in addresses
        ]

    monkeypatch.setattr(radio.socket, "getaddrinfo", getaddrinfo)


async def test_a_host_is_refused_if_any_address_is_private(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, "93.184.216.34")
    await check_public_url(STREAM)
    fake_dns(monkeypatch, "93.184.216.34", "192.168.1.20")
    with pytest.raises(RadioURLError, match="private network"):
        await check_public_url(STREAM)


async def test_localhost_by_name_is_refused() -> None:
    with pytest.raises(RadioURLError, match="private network"):
        await check_public_url("http://localhost/radio.mp3")
    with pytest.raises(OSError, match="private network"):
        await PublicResolver().resolve("localhost", 80)


# Trusted hosts


TRUSTED = "radio.example.com"


@pytest.fixture
def trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(radio, "trusted_hosts", frozenset({TRUSTED}))


@pytest.mark.usefixtures("trusted")
@pytest.mark.parametrize("host", [TRUSTED, "Radio.Example.COM", "radio.example.com."])
async def test_a_trusted_host_may_resolve_to_the_lan(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    fake_dns(monkeypatch, "192.168.1.20", "fd00::20")
    await check_public_url(f"https://{host}/listen/lofi/radio.mp3")
    assert await PublicResolver().resolve(host, 443)


@pytest.mark.usefixtures("trusted")
@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "fe80::1", "::ffff:127.0.0.1"])
async def test_a_trusted_host_still_cannot_reach_loopback_or_link_local(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    fake_dns(monkeypatch, address)
    with pytest.raises(RadioURLError, match="private network"):
        await check_public_url(f"https://{TRUSTED}/radio.mp3")
    with pytest.raises(OSError, match="private network"):
        await PublicResolver().resolve(TRUSTED, 443)


@pytest.mark.usefixtures("trusted")
@pytest.mark.parametrize(
    "host", ["other.example.com", "evil.radio.example.com", "radio.example.com.evil.net"]
)
async def test_only_the_exact_trusted_host_may_resolve_to_the_lan(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    fake_dns(monkeypatch, "192.168.1.20")
    with pytest.raises(RadioURLError, match="private network"):
        await check_public_url(f"https://{host}/radio.mp3")
    with pytest.raises(OSError, match="private network"):
        await PublicResolver().resolve(host, 443)


def test_trusted_hosts_parse_exact_hostnames() -> None:
    assert parse_trusted_hosts("") == frozenset()
    assert parse_trusted_hosts(" Radio.Example.com. , azuracast ,") == {TRUSTED, "azuracast"}


@pytest.mark.parametrize(
    "entry",
    [
        "*.example.com",
        "192.168.1.5",
        "::1",
        "1.2.3",
        "radio.example.com:8000",
        "https://radio.example.com",
        "-radio.example.com",
        "radio..example.com",
    ],
)
def test_trusted_hosts_reject_anything_but_a_hostname(entry: str) -> None:
    with pytest.raises(ValueError, match="RADIO_TRUSTED_HOSTS"):
        parse_trusted_hosts(f"{TRUSTED},{entry}")


def test_a_bad_trusted_host_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.config.load_dotenv", lambda: None)
    for name, value in (("DISCORD_BOT_TOKEN", "t"), ("SPOTIFY_CLIENT_ID", "i")):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "s")
    monkeypatch.setenv("RADIO_TRUSTED_HOSTS", "*.example.com")
    with pytest.raises(ValueError, match=r"RADIO_TRUSTED_HOSTS entry '\*\.example\.com'"):
        load_config()
    monkeypatch.setenv("RADIO_TRUSTED_HOSTS", TRUSTED)
    assert load_config().radio_trusted_hosts == {TRUSTED}


# Now-playing fetch


@pytest.fixture
async def server() -> AsyncIterator[tuple[str, list[str]]]:
    hits: list[str] = []

    async def handle(request: web.Request) -> web.StreamResponse:
        hits.append(request.path)
        if request.path == "/redirect":
            raise web.HTTPFound("/song")
        if request.path in ("/split", "/big"):
            # Valid JSON either way, so only the size cap can turn /big away
            body = json.dumps(SONG).encode()
            if request.path == "/big":
                body += b" " * radio.NOWPLAYING_MAX_BYTES
            resp = web.StreamResponse()
            resp.enable_chunked_encoding()
            await resp.prepare(request)
            half = len(body) // 2
            await resp.write(body[:half])
            await asyncio.sleep(0.05)
            await resp.write(body[half:])
            await resp.write_eof()
            return resp
        if request.path == "/nested":
            return web.json_response({"now_playing": {"song": {"title": {"a": 1}}}})
        return web.json_response(SONG)

    app = web.Application()
    app.router.add_get("/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    yield f"http://127.0.0.1:{port}", hits
    await runner.cleanup()


async def test_fetch_now_playing_reads_the_song(
    server: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, hits = server
    monkeypatch.setattr(radio, "check_public_url", no_check)
    assert await fetch_now_playing(f"{base}/song") == "Aurora – Purrple Cat"
    assert await fetch_now_playing(f"{base}/nested") is None
    assert hits == ["/song", "/nested"]


async def test_fetch_now_playing_reads_a_body_sent_in_parts(
    server: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = server
    monkeypatch.setattr(radio, "check_public_url", no_check)
    assert await fetch_now_playing(f"{base}/split") == "Aurora – Purrple Cat"


async def test_fetch_now_playing_refuses_a_body_over_the_cap(
    server: tuple[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    base, _ = server
    monkeypatch.setattr(radio, "check_public_url", no_check)
    with caplog.at_level(logging.DEBUG, logger="src.radio"):
        assert await fetch_now_playing(f"{base}/big") is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("over 64000 bytes" in m for m in messages)
    assert not any("fetch failed" in m for m in messages)


async def test_fetch_now_playing_does_not_follow_redirects(
    server: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, hits = server
    monkeypatch.setattr(radio, "check_public_url", no_check)
    assert await fetch_now_playing(f"{base}/redirect") is None
    assert hits == ["/redirect"]


async def test_fetch_now_playing_never_connects_to_the_lan(server: tuple[str, list[str]]) -> None:
    base, hits = server
    assert await fetch_now_playing(f"{base}/song") is None
    assert hits == []


def test_now_playing_label_from_azuracast_json() -> None:
    assert now_playing_label(SONG) == "Aurora – Purrple Cat"
    assert now_playing_label({"now_playing": {"song": {"title": " Aurora\n", "artist": 7}}}) == (
        "Aurora"
    )
    long = now_playing_label({"now_playing": {"song": {"title": "x" * 900}}}) or ""
    assert len(long) == CHANNEL_STATUS_LIMIT == 500


@pytest.mark.parametrize(
    "data",
    [
        {},
        None,
        [],
        {"now_playing": None},
        {"now_playing": {"song": {"title": "  "}}},
        {"now_playing": {"song": {"title": ["raw", "json"]}}},
    ],
)
def test_now_playing_label_ignores_missing_song(data: Any) -> None:
    assert now_playing_label(data) is None


# Voice channel status


def radio_guild(connected: bool = True) -> tuple[Any, Any]:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.edit = AsyncMock()
    channel.guild.id = 1
    guild = MagicMock()
    guild.id = 1
    guild.get_channel = MagicMock(return_value=channel)
    guild.voice_client.channel = channel if connected else None
    return guild, channel


async def test_status_shows_the_song_once_per_change(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db.set_radio(1, STREAM, 7, NOWPLAYING)
    monkeypatch.setattr(radio, "fetch_now_playing", AsyncMock(return_value="Aurora – Purrple Cat"))
    status = RadioStatus(db)
    guild, channel = radio_guild()

    await status.update(guild)
    await status.update(guild)

    channel.edit.assert_awaited_once_with(status="Aurora – Purrple Cat")
    assert status.current(1) == "Aurora – Purrple Cat"


async def test_status_waits_until_the_bot_is_in_the_channel(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db.set_radio(1, STREAM, 7, NOWPLAYING)
    fetch = AsyncMock(return_value="Aurora")
    monkeypatch.setattr(radio, "fetch_now_playing", fetch)
    guild, channel = radio_guild(connected=False)
    await RadioStatus(db).update(guild)
    fetch.assert_not_awaited()
    channel.edit.assert_not_awaited()


async def test_missing_status_permission_is_logged_once_and_keeps_going(
    db: DatabaseManager, caplog: pytest.LogCaptureFixture
) -> None:
    status = RadioStatus(db)
    _, channel = radio_guild()
    channel.edit.side_effect = discord.Forbidden(
        MagicMock(status=403, reason="Forbidden"), "Missing Permissions"
    )
    with caplog.at_level(logging.WARNING):
        await status.set_status(channel, "Aurora")
        await status.set_status(channel, "Midnight Tea")
    warnings = [r for r in caplog.records if "Set Voice Channel Status" in r.getMessage()]
    assert len(warnings) == 1


async def test_pollers_start_for_radio_servers_and_stop(db: DatabaseManager) -> None:
    await db.set_radio(1, STREAM, 7, NOWPLAYING)
    await db.set_radio(2, STREAM, 8)  # radio without a now-playing URL
    status = RadioStatus(db)
    status.update = AsyncMock()  # type: ignore[method-assign]
    guilds = [MagicMock(id=gid) for gid in (1, 2, 3)]

    await status.start_all(guilds)
    assert set(status._tasks) == {1}
    task = status._tasks[1]
    status.stop(1)
    await asyncio.sleep(0)
    assert task.cancelled() and not status._tasks


# Persistence


async def test_radio_settings_persist_and_clear(db: DatabaseManager) -> None:
    on = await db.set_radio(1, STREAM, 7, NOWPLAYING)
    assert (on.radio_stream_url, on.radio_channel_id, on.radio_nowplaying_url) == (
        STREAM,
        7,
        NOWPLAYING,
    )
    assert (on.voice_channel_id, on.control_channel_id) == (5, 99)
    off = await db.set_radio(1)
    assert (off.radio_stream_url, off.radio_channel_id, off.radio_nowplaying_url) == ("", None, "")


async def test_an_old_database_gains_the_radio_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, voice_channel_id INTEGER, "
            "control_channel_id INTEGER, volume REAL NOT NULL DEFAULT 0.5, loop_mode TEXT NOT NULL "
            "DEFAULT 'playlist', shuffle INTEGER NOT NULL DEFAULT 0, fallback_triggers TEXT NOT "
            "NULL DEFAULT 'on_empty_and_error', fallback_url TEXT NOT NULL, current_index INTEGER "
            "NOT NULL DEFAULT 0, on_fallback INTEGER NOT NULL DEFAULT 0, fallback_reason TEXT NOT "
            "NULL DEFAULT '')"
        )
        conn.execute("INSERT INTO guild_settings (guild_id, fallback_url) VALUES (1, 'x')")
    manager = DatabaseManager(path, default_fallback_url="https://lofi.radio")
    await manager.init_db()
    settings = await manager.get_guild_settings(1)
    assert (settings.radio_stream_url, settings.radio_channel_id, settings.autoplay) == (
        "",
        None,
        False,
    )


# Commands


def fake_interaction(command_name: str = "", guild_id: int = 1, channel_id: int = 99) -> Any:
    interaction = MagicMock()
    interaction.guild.id = guild_id
    interaction.channel_id = channel_id
    interaction.command.qualified_name = command_name
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def run(command: Any, *args: Any) -> None:
    await command.callback(*args)


def make_cog(db: DatabaseManager) -> Any:
    audio = MagicMock()
    audio.stop_and_disconnect = AsyncMock()
    audio.start_fallback = AsyncMock()
    radio_status = MagicMock()
    radio_status.set_status = AsyncMock()
    return MusicCommandsCog(
        bot=MagicMock(), db=db, spotify=MagicMock(), audio=audio, radio_status=radio_status
    )


@pytest.mark.parametrize("command", [MusicCommandsCog.radio_on, MusicCommandsCog.radio_off])
def test_radio_commands_need_manage_server(command: Any) -> None:
    check = command.checks[0]
    interaction = MagicMock()
    interaction.permissions = discord.Permissions.none()
    with pytest.raises(app_commands.MissingPermissions):
        check(interaction)
    interaction.permissions = discord.Permissions(manage_guild=True)
    assert check(interaction) is True


async def test_radio_on_saves_and_starts(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.cogs.music_commands.check_public_url", no_check)
    cog = make_cog(db)
    interaction = fake_interaction()
    channel = MagicMock(spec=discord.VoiceChannel, id=7, mention="<#7>")

    await run(MusicCommandsCog.radio_on, cog, interaction, f" {STREAM} ", channel, NOWPLAYING)

    settings = await db.get_guild_settings(1)
    assert (settings.radio_stream_url, settings.radio_channel_id) == (STREAM, 7)
    assert settings.radio_nowplaying_url == NOWPLAYING
    cog.audio.start_fallback.assert_awaited_once_with(
        interaction.guild, reason=RADIO_MODE_REASON, announce=False
    )
    cog.radio_status.start.assert_called_once_with(interaction.guild)
    assert "Radio is on" in interaction.followup.send.await_args.args[0]


async def test_radio_on_refuses_a_lan_stream(db: DatabaseManager) -> None:
    cog = make_cog(db)
    interaction = fake_interaction()
    channel = MagicMock(spec=discord.VoiceChannel, id=7)

    await run(
        MusicCommandsCog.radio_on,
        cog,
        interaction,
        "http://192.168.1.5:8000/radio.mp3",
        channel,
        None,
    )

    assert "private network" in interaction.followup.send.await_args.args[0]
    assert (await db.get_guild_settings(1)).radio_stream_url == ""
    cog.audio.start_fallback.assert_not_awaited()


async def test_radio_on_refuses_a_lan_nowplaying_url(db: DatabaseManager) -> None:
    cog = make_cog(db)
    interaction = fake_interaction()
    channel = MagicMock(spec=discord.VoiceChannel, id=7)

    await run(
        MusicCommandsCog.radio_on,
        cog,
        interaction,
        "http://93.184.216.34/radio.mp3",
        channel,
        "http://[::ffff:10.0.0.1]/api",
    )

    assert "private network" in interaction.followup.send.await_args.args[0]
    assert (await db.get_guild_settings(1)).radio_stream_url == ""


async def test_radio_off_stops_clears_status_and_forgets(db: DatabaseManager) -> None:
    await db.set_radio(1, STREAM, 7, NOWPLAYING)
    cog = make_cog(db)
    interaction = fake_interaction()
    channel = MagicMock(spec=discord.VoiceChannel)
    interaction.guild.get_channel = MagicMock(return_value=channel)

    await run(MusicCommandsCog.radio_off, cog, interaction)

    cog.radio_status.stop.assert_called_once_with(1)
    cog.radio_status.set_status.assert_awaited_once_with(channel, None)
    cog.audio.stop_and_disconnect.assert_awaited_once_with(interaction.guild)
    assert (await db.get_guild_settings(1)).radio_stream_url == ""


async def test_stop_clears_the_radio_channel_status(db: DatabaseManager) -> None:
    await db.set_radio(1, STREAM, 7, NOWPLAYING)
    cog = make_cog(db)
    interaction = fake_interaction("stop")
    channel = MagicMock(spec=discord.VoiceChannel)
    interaction.guild.get_channel = MagicMock(return_value=channel)

    await run(MusicCommandsCog.stop_cmd, cog, interaction)

    cog.radio_status.set_status.assert_awaited_once_with(channel, None)
    cog.audio.stop_and_disconnect.assert_awaited_once_with(interaction.guild)


async def test_a_radio_stream_that_is_down_is_retried(db: DatabaseManager) -> None:
    cog = make_cog(db)
    cog.audio.start_fallback.side_effect = RuntimeError("connection refused")
    guild = MagicMock(id=1)
    await cog._start_radio(guild)
    cog.audio.schedule_recovery.assert_called_once()
    assert cog.audio.schedule_recovery.call_args.args[0] is guild


def test_radio_commands_are_hidden_from_non_admins() -> None:
    permissions = MusicCommandsCog.radio_group.default_permissions
    assert permissions is not None and permissions.manage_guild


async def test_radio_off_when_already_off(db: DatabaseManager) -> None:
    cog = make_cog(db)
    interaction = fake_interaction()
    await run(MusicCommandsCog.radio_off, cog, interaction)
    assert "already off" in interaction.response.send_message.await_args.args[0]
    cog.audio.stop_and_disconnect.assert_not_awaited()


def test_blocked_in_radio_mode_covers_playlist_commands_only() -> None:
    for name in ("playlist add", "playlist resume", "fallback start", "search", "skip", "mode"):
        assert blocked_in_radio_mode(name) is True
    for name in ("play", "pause", "stop", "volume", "status", "setup", "radio off"):
        assert blocked_in_radio_mode(name) is False


async def test_radio_blocks_playlist_commands_only_in_its_own_server(db: DatabaseManager) -> None:
    await db.set_radio(1, STREAM, 7)
    await db.set_channels(2, voice_channel_id=6, control_channel_id=98)
    cog = make_cog(db)

    interaction = fake_interaction("playlist add")
    assert await cog._require_mod_channel(interaction) is False
    reply = interaction.response.send_message.await_args
    assert "radio" in reply.args[0]
    assert reply.kwargs == {"ephemeral": True}
    assert await cog._require_mod_channel(fake_interaction("volume")) is True

    other = fake_interaction("playlist add", guild_id=2, channel_id=98)
    assert await cog._require_mod_channel(other) is True

    await db.set_radio(1)
    assert await cog._require_mod_channel(fake_interaction("playlist add")) is True


async def test_play_restarts_the_servers_own_radio(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db.set_radio(1, STREAM, 7)
    monkeypatch.setattr("src.cogs.music_commands.check_public_url", no_check)
    cog = make_cog(db)
    interaction = fake_interaction("play")
    interaction.guild.voice_client = None

    await run(MusicCommandsCog.play_cmd, cog, interaction)

    cog.audio.start_fallback.assert_awaited_once_with(
        interaction.guild, reason=RADIO_MODE_REASON, announce=False
    )
    cog.audio.play_playlist_index.assert_not_called()
    assert interaction.followup.send.await_args.args[0] == "📻 Playing the radio stream."


async def test_play_refuses_a_stream_that_now_points_at_the_lan(db: DatabaseManager) -> None:
    await db.set_radio(1, "http://10.0.0.8/radio.mp3", 7)
    cog = make_cog(db)
    interaction = fake_interaction("play")
    interaction.guild.voice_client = None

    await run(MusicCommandsCog.play_cmd, cog, interaction)

    cog.audio.start_fallback.assert_not_awaited()
    assert "private network" in interaction.followup.send.await_args.args[0]


# Playback


async def test_startup_resumes_a_playing_radio_but_not_a_stopped_one(db: DatabaseManager) -> None:
    await db.set_radio(1, STREAM, 7)
    await db.set_autoplay(1, True)
    await db.set_radio(3, STREAM, 8)  # autoplay off, as after a /stop
    guilds = [MagicMock(id=gid) for gid in (1, 2, 3)]
    for guild in guilds:
        guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.VoiceChannel))
    manager = AudioManager(bot=MagicMock(guilds=guilds), db=db)
    manager._spawn = MagicMock()  # type: ignore[method-assign]

    await manager.restore_sessions()

    guilds[0].get_channel.assert_called_once_with(7)
    guilds[2].get_channel.assert_not_called()
    assert manager._wanted == {1}
    manager._spawn.assert_called_once()
    manager._spawn.call_args.args[1].close()


async def test_radio_turned_on_while_a_track_resolves_keeps_playing(db: DatabaseManager) -> None:
    await db.add_tracks(1, [TrackMetadata("spotify:track:1", "Aurora", "Purrple Cat")], "me")
    manager = AudioManager(bot=MagicMock(), db=db)
    play = AsyncMock()
    manager._play_stream = play  # type: ignore[method-assign]
    guild = MagicMock(id=1)

    async def resolve_while_radio_starts(_guild_id: int, _track: Any) -> Resolved:
        await db.set_radio(1, STREAM, 7)
        await db.set_fallback_state(1, on_fallback=True, reason=RADIO_MODE_REASON)
        return Resolved("https://cdn.example.com/a.mp3", "sc:1", "https://example.com/a", 1e12)

    manager._resolve_track = resolve_while_radio_starts  # type: ignore[method-assign, assignment]
    await manager.play_playlist_index(guild)

    play.assert_not_awaited()
    settings = await db.get_guild_settings(1)
    assert (settings.on_fallback, settings.fallback_reason) == (True, RADIO_MODE_REASON)


async def test_the_return_to_playlist_check_stops_once_radio_is_on(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db.add_tracks(1, [TrackMetadata("spotify:track:1", "Aurora", "Purrple Cat")], "me")
    await db.set_fallback_state(1, on_fallback=True, reason=f"{AUTO_RADIO_PREFIX} test")
    await db.set_radio(1, STREAM, 7)
    monkeypatch.setattr(audio_manager, "RADIO_RETRY_SECONDS", 0)
    manager = AudioManager(bot=MagicMock(), db=db)
    manager._wanted.add(1)
    manager.resolver.resolve = AsyncMock()  # type: ignore[method-assign]
    await manager._radio_return(MagicMock(id=1))
    manager.resolver.resolve.assert_not_awaited()


async def test_a_radio_server_resumes_its_radio(db: DatabaseManager) -> None:
    await db.set_radio(1, STREAM, 7)
    manager = AudioManager(bot=MagicMock(), db=db)
    manager.start_fallback = AsyncMock()  # type: ignore[method-assign]
    guild = MagicMock(id=1)
    await manager.resume_from_state(guild)
    manager.start_fallback.assert_awaited_once_with(guild, reason=RADIO_MODE_REASON, announce=False)


async def test_radio_plays_its_own_stream_and_rechecks_it(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db.set_radio(1, STREAM, 7)
    manager = AudioManager(bot=MagicMock(), db=db)
    play = AsyncMock()
    manager._play_stream = play  # type: ignore[method-assign]
    manager._resolve_fallback_stream_url = AsyncMock()  # type: ignore[method-assign]
    guild = MagicMock(id=1)
    checked: list[str] = []

    async def check(url: str) -> None:
        checked.append(url)

    monkeypatch.setattr("src.audio_manager.check_public_url", check)
    await manager.start_fallback(guild, reason=RADIO_MODE_REASON, announce=False)
    assert checked == [STREAM]
    assert play.await_args is not None and play.await_args.args[1] == STREAM
    manager._resolve_fallback_stream_url.assert_not_awaited()

    async def refuse(_url: str) -> None:
        raise RadioURLError("That address points to a private network")

    monkeypatch.setattr("src.audio_manager.check_public_url", refuse)
    with pytest.raises(RadioURLError):
        await manager.start_fallback(guild, reason=RADIO_MODE_REASON, announce=False)
    assert play.await_count == 1


def test_label_json_is_never_echoed_raw() -> None:
    data = json.loads('{"now_playing": {"song": {"title": "A", "artist": {"x": "<b>"}}}}')
    assert now_playing_label(data) == "A"
