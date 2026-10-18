"""Tests for logging setup, the healthcheck, the autoplay migration, and song search."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from src import healthcheck
from src.bot import configure_logging
from src.database import DatabaseManager
from src.spotify_client import SpotifyResolver


@pytest.fixture
def clean_root_logger() -> Any:
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for handler in root.handlers[len(before) :]:
        handler.close()
    root.handlers[:] = before


def test_configure_logging_writes_stdout_and_rotating_file(
    tmp_path: Path, clean_root_logger: logging.Logger
) -> None:
    configure_logging("INFO", tmp_path / "logs")
    logging.getLogger("lofi.test").error("stream died")
    for handler in clean_root_logger.handlers:
        handler.flush()
    assert "stream died" in (tmp_path / "logs" / "bot.log").read_text()


def test_configure_logging_survives_unwritable_dir(
    tmp_path: Path, clean_root_logger: logging.Logger
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    configure_logging("INFO", blocker / "logs")
    assert any(isinstance(h, logging.StreamHandler) for h in clean_root_logger.handlers)


def test_healthcheck_tracks_heartbeat_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lofi_bot.db"))
    assert healthcheck.is_healthy() is False
    beat = tmp_path / "heartbeat"
    beat.touch()
    assert healthcheck.is_healthy() is True
    stale = time.time() - healthcheck.MAX_HEARTBEAT_AGE_SECONDS - 5
    os.utime(beat, (stale, stale))
    assert healthcheck.is_healthy() is False


async def test_init_db_adds_autoplay_to_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, voice_channel_id INTEGER,"
            " control_channel_id INTEGER, volume REAL NOT NULL DEFAULT 0.5,"
            " loop_mode TEXT NOT NULL DEFAULT 'playlist', shuffle INTEGER NOT NULL DEFAULT 0,"
            " fallback_triggers TEXT NOT NULL DEFAULT 'on_empty_and_error',"
            " fallback_url TEXT NOT NULL, current_index INTEGER NOT NULL DEFAULT 0,"
            " on_fallback INTEGER NOT NULL DEFAULT 0, fallback_reason TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("INSERT INTO guild_settings (guild_id, fallback_url) VALUES (1, 'x')")
    db = DatabaseManager(path, default_fallback_url="x")
    await db.init_db()
    await db.init_db()
    assert (await db.get_guild_settings(1)).autoplay is False
    await db.set_autoplay(1, True)
    assert (await db.get_guild_settings(1)).autoplay is True


class FakeSpotify:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.queries: list[str] = []

    def search(self, q: str, limit: int, type: str) -> dict[str, Any]:  # noqa: A002
        self.queries.append(q)
        return {"tracks": {"items": self.items}}


async def test_search_track_returns_best_match_or_none() -> None:
    resolver = SpotifyResolver(client_id="dummy_client_id", client_secret="dummy_client_secret")
    fake = FakeSpotify([{"id": "t1", "name": "Aurora", "artists": [{"name": "Purrple Cat"}]}])
    resolver._client = fake  # type: ignore[assignment]
    track = await resolver.search_track("purrple cat aurora")
    assert track is not None
    assert (track.title, track.artist) == ("Aurora", "Purrple Cat")
    assert track.spotify_url == "https://open.spotify.com/track/t1"

    resolver._client = FakeSpotify([])  # type: ignore[assignment]
    assert await resolver.search_track("nothing") is None
