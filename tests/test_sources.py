"""Tests for picking playable search results, health-checking fallback stations and notices."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import discord
import pytest
import yt_dlp
from aiohttp import web
from aiohttp.test_utils import TestServer
from src import audio_manager
from src.audio_manager import AudioManager, NoStreamFound, extract_audio_urls_from_text
from src.config import parse_url_list

DEAD = "https://www.youtube.com/watch?v=dead"
GOOD = "https://www.youtube.com/watch?v=good"


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: flat searches list pages, pages resolve or fail."""

    calls: list[str] = []
    soundcloud: list[str] = []
    youtube: list[str] = [DEAD, GOOD]

    def __init__(self, options: dict[str, Any]) -> None:
        self.flat = bool(options.get("extract_flat"))

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def extract_info(self, target: str, download: bool = False) -> dict[str, Any]:
        FakeYDL.calls.append(target)
        if target.startswith("scsearch"):
            return {"entries": [{"url": u} for u in FakeYDL.soundcloud]}
        if target.startswith("ytsearch"):
            return {"entries": [{"url": u} for u in FakeYDL.youtube]}
        if target == DEAD:
            raise yt_dlp.utils.DownloadError("ERROR: [youtube] dead: This video is not available")
        return {"url": f"https://cdn.example/{target.rsplit('=', 1)[-1]}"}


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> type[FakeYDL]:
    FakeYDL.calls = []
    FakeYDL.soundcloud = []
    FakeYDL.youtube = [DEAD, GOOD]
    monkeypatch.setattr(audio_manager.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_search_skips_unavailable_video_and_logs_why_soundcloud_missed(
    fake_ydl: type[FakeYDL], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    url, failed = audio_manager._extract_stream_url_sync("Cat - Song", frozenset())
    assert url == "https://cdn.example/good"
    assert failed == [DEAD]
    assert "SoundCloud search found nothing for 'Cat - Song'" in caplog.text
    assert "This video is not available" in caplog.text


def test_search_raises_with_every_failed_page(fake_ydl: type[FakeYDL]) -> None:
    fake_ydl.youtube = [DEAD]
    with pytest.raises(NoStreamFound) as info:
        audio_manager._extract_stream_url_sync("Cat - Song", frozenset())
    assert info.value.failed == [DEAD]


async def test_retry_never_requests_a_page_that_already_failed(fake_ydl: type[FakeYDL]) -> None:
    manager = AudioManager(bot=SimpleNamespace(), db=None)  # type: ignore[arg-type]
    fake_ydl.youtube = [DEAD]
    with pytest.raises(NoStreamFound):
        await manager._extract_stream_url("Cat - Song")
    fake_ydl.calls.clear()
    fake_ydl.youtube = [DEAD, GOOD]
    assert await manager._extract_stream_url("Cat - Song") == "https://cdn.example/good"
    assert DEAD not in fake_ydl.calls


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


class ForbiddenChannel(discord.TextChannel):
    def __init__(self) -> None:  # noqa: D107
        self.id = 99
        self.name = "mod-room"

    async def send(self, *_: Any, **__: Any) -> Any:  # type: ignore[override]
        raise discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),  # type: ignore[arg-type]
            {"code": 50001, "message": "Missing Access"},
        )


async def test_missing_mod_channel_access_logs_one_clear_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = ForbiddenChannel()
    guild = SimpleNamespace(id=1, name="Lofi Server", get_channel=lambda _id: channel)

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
