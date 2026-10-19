"""Tests for health-checking fallback stations and mod channel notices."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from src.audio_manager import AudioManager, extract_audio_urls_from_text
from src.config import parse_url_list


@pytest.fixture
async def stations() -> Any:
    hits: dict[str, int] = {}

    async def handler(request: web.Request) -> web.Response:
        hits[request.path] = hits.get(request.path, 0) + 1
        return web.Response(status=404 if request.path.startswith("/dead") else 200)

    app = web.Application()
    app.router.add_get("/{name}", handler)
    server = TestServer(app)
    await server.start_server()
    yield server, hits
    await server.close()


async def test_dead_station_is_skipped_and_remembered(stations: Any) -> None:
    server, hits = stations
    dead, live = str(server.make_url("/dead.mp3")), str(server.make_url("/live"))
    manager = AudioManager(bot=SimpleNamespace(), db=None, backup_streams=[live])  # type: ignore[arg-type]

    assert await manager._resolve_fallback_stream_url(dead) == live
    assert await manager._resolve_fallback_stream_url(dead) == live
    assert hits["/dead.mp3"] == 1


async def test_all_backups_down_are_rechecked_instead_of_going_silent(stations: Any) -> None:
    server, hits = stations
    live = str(server.make_url("/live"))
    manager = AudioManager(bot=SimpleNamespace(), db=None, backup_streams=[live])  # type: ignore[arg-type]
    manager.mark_stream_dead(live, "earlier failure")
    assert await manager._resolve_fallback_stream_url(live, use_backup=True) == live


def test_lofi_radio_song_names_resolve_under_songs_folder() -> None:
    html = 'let songs = ["3 A.M. Radio.mp3","Visions.mp3"];'
    assert extract_audio_urls_from_text(html, "https://lofi.radio/songs/") == [
        "https://lofi.radio/songs/3%20A.M.%20Radio.mp3",
        "https://lofi.radio/songs/Visions.mp3",
    ]


def test_parse_url_list_accepts_commas_and_spaces() -> None:
    raw = "https://a.example/one, https://b.example/two  not-a-url"
    assert parse_url_list(raw) == ("https://a.example/one", "https://b.example/two")
    assert parse_url_list("") == ()


class ModChannel(discord.TextChannel):
    def __init__(self, allowed: bool) -> None:  # noqa: D107
        self.id = 99
        self.name = "mod-room"
        self.allowed = allowed
        self.sends = 0

    def permissions_for(self, _member: Any) -> Any:  # type: ignore[override]
        return SimpleNamespace(
            view_channel=self.allowed, send_messages=self.allowed, embed_links=self.allowed
        )

    async def send(self, *_: Any, **__: Any) -> Any:  # type: ignore[override]
        self.sends += 1
        if not self.allowed:
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"),  # type: ignore[arg-type]
                {"code": 50001, "message": "Missing Access"},
            )


async def test_missing_mod_channel_access_logs_one_line_and_skips_sends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = ModChannel(allowed=False)
    guild = SimpleNamespace(id=1, name="Lofi Server", me=object(), get_channel=lambda _id: channel)

    class Db:
        async def get_guild_settings(self, _guild_id: int) -> Any:
            return SimpleNamespace(control_channel_id=99)

    manager = AudioManager(bot=SimpleNamespace(), db=Db())  # type: ignore[arg-type]
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        await manager.notify_control_channel(guild, "t", "d")  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1
    assert "#mod-room (99)" in lines[0]
    assert "View Channel, Send Messages and Embed Links" in lines[0]
    assert channel.sends == 0

    channel.allowed = True
    await manager.notify_control_channel(guild, "t", "d")  # type: ignore[arg-type]
    assert channel.sends == 1
    assert manager._notice_problems == set()


async def test_recovered_backup_is_rechecked_when_the_others_fail(stations: Any) -> None:
    server, _ = stations
    recovered, broken = str(server.make_url("/live")), str(server.make_url("/dead"))
    manager = AudioManager(
        bot=SimpleNamespace(),  # type: ignore[arg-type]
        db=None,  # type: ignore[arg-type]
        backup_streams=[recovered, broken],
    )
    manager.mark_stream_dead(recovered, "earlier failure")
    assert await manager._resolve_fallback_stream_url(broken, use_backup=True) == recovered
