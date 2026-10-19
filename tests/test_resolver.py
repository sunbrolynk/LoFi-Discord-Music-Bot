"""Tests for matching search results to a track, the resolve cache and the bad-source list."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from src import resolver as resolver_module
from src.database import DatabaseManager, TrackMetadata
from src.resolver import (
    Candidate,
    ResolveError,
    SourcesUnreachable,
    TrackResolver,
    score_candidate,
    stream_expiry,
)

FEATHER = TrackMetadata("https://open.spotify.com/track/f1", "Feather", "Nujabes", 175_000)


def cand(source_id: str, title: str, duration: float | None, uploader: str = "") -> Candidate:
    return Candidate(source_id, f"https://page/{source_id}", title, uploader, duration)


def test_score_prefers_close_length_and_matching_title() -> None:
    exact, _ = score_candidate(FEATHER, cand("youtube:a", "Nujabes - Feather", 176))
    loose, _ = score_candidate(FEATHER, cand("youtube:b", "Feather", 190))
    assert exact is not None and loose is not None
    assert exact > loose


@pytest.mark.parametrize(
    ("title", "duration", "reason"),
    [
        ("Nujabes - Feather (1 Hour Long)", 3608, "'1 hour' version"),
        ("Nujabes - Feather (Extended)", 3608, "length"),
        ("Nujabes - Feather", 120, "length"),
        ("Nujabes - Feather (slowed + reverb)", 180, "'slowed' version"),
        ("Nujabes - Feather LIVE at Shibuya", 178, "'live' version"),
        ("reaction to nujabes feather", 176, "'reaction' version"),
        ("Aruarian Dance", 175, "title doesn't match"),
    ],
)
def test_score_rejects_obvious_mismatches(title: str, duration: float, reason: str) -> None:
    score, why = score_candidate(FEATHER, cand("youtube:x", title, duration))
    assert score is None
    assert reason in why


def test_score_keeps_a_variant_the_original_is() -> None:
    slowed = TrackMetadata("", "Feather (Slowed)", "Nujabes", 200_000)
    score, _ = score_candidate(slowed, cand("youtube:s", "Nujabes - Feather (slowed)", 201))
    assert score is not None


def test_score_without_spotify_duration_still_matches_title() -> None:
    no_length = TrackMetadata("", "Feather", "Nujabes", 0)
    score, _ = score_candidate(no_length, cand("soundcloud:1", "Feather", None))
    assert score is not None


def test_stream_expiry_reads_youtube_and_soundcloud_params() -> None:
    assert stream_expiry("https://rr4.googlevideo.com/videoplayback?expire=1792379167&x=1", 0) == (
        1792379167.0
    )
    assert stream_expiry("https://playback.soundcloud.cloud/a.m3u8?expires=1792364945", 0) == (
        1792364945.0
    )
    assert stream_expiry("https://radio.example/a.mp3", 100.0) == (
        100.0 + resolver_module.DEFAULT_STREAM_LIFETIME_SECONDS
    )


class Sites:
    """Fake search and extract: per-site results, pages that fail, and a call log."""

    def __init__(self) -> None:
        self.results: dict[str, list[Candidate]] = {"soundcloud": [], "youtube": []}
        self.down: set[str] = set()
        self.broken_pages: set[str] = set()
        self.searches: list[str] = []
        self.extracts: list[str] = []

    async def search(self, query: str, prefix: str, source: str) -> list[Candidate]:
        self.searches.append(source)
        if source in self.down:
            raise OSError("connection refused")
        return self.results[source]

    async def extract(self, page: str) -> str:
        self.extracts.append(page)
        if page in self.broken_pages:
            raise RuntimeError("This video is not available")
        return f"https://cdn.example/{page.rsplit('/', 1)[-1]}?expire=4102444800"


@pytest.fixture
async def resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    db = DatabaseManager(tmp_path / "bot.db", default_fallback_url="x")
    await db.init_db()
    sites = Sites()
    instance = TrackResolver(db)
    monkeypatch.setattr(instance, "search", sites.search)
    monkeypatch.setattr(instance, "extract", sites.extract)
    return instance, sites, db


async def test_cache_hit_skips_the_search(resolver: Any) -> None:
    instance, sites, _ = resolver
    sites.results["youtube"] = [cand("youtube:good", "Nujabes - Feather", 176)]
    first = await instance.resolve(FEATHER)
    assert first.source_id == "youtube:good"
    sites.searches.clear()

    second = await instance.resolve(FEATHER)
    assert second.source_id == "youtube:good"
    assert sites.searches == []


async def test_unavailable_result_is_skipped_and_never_retried(resolver: Any) -> None:
    instance, sites, db = resolver
    dead = cand("youtube:dead", "Nujabes - Feather", 176)
    good = cand("youtube:good", "Feather", 190)
    sites.results["youtube"] = [dead, good]
    sites.broken_pages.add(dead.page)

    assert (await instance.resolve(FEATHER)).source_id == "youtube:good"
    assert "youtube:dead" in await db.get_bad_sources(0.0)

    await db.delete_cached_source(FEATHER.spotify_url)
    sites.extracts.clear()
    await instance.resolve(FEATHER)
    assert dead.page not in sites.extracts


async def test_cached_source_that_stops_working_falls_back_to_search(resolver: Any) -> None:
    instance, sites, _ = resolver
    first = cand("youtube:first", "Nujabes - Feather", 176)
    sites.results["youtube"] = [first]
    await instance.resolve(FEATHER)

    sites.broken_pages.add(first.page)
    sites.results["soundcloud"] = [cand("soundcloud:2", "Feather", 175, "Nujabes")]
    assert (await instance.resolve(FEATHER)).source_id == "soundcloud:2"


async def test_only_mismatches_raise_with_the_reasons(resolver: Any) -> None:
    instance, sites, _ = resolver
    sites.results["youtube"] = [cand("youtube:long", "Nujabes - Feather (Extended)", 3600)]
    with pytest.raises(ResolveError, match="length"):
        await instance.resolve(FEATHER)


async def test_every_site_down_is_reported_as_unreachable(resolver: Any) -> None:
    instance, sites, _ = resolver
    sites.down = {"soundcloud", "youtube"}
    with pytest.raises(SourcesUnreachable):
        await instance.resolve(FEATHER)
