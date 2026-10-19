"""Turns a playlist track into a playable stream: cached source, scored search, bad-source list."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar
from urllib.parse import parse_qs, urlparse

import yt_dlp

from src.database import DatabaseManager

logger = logging.getLogger(__name__)

SEARCH_CANDIDATES = 5
SEARCH_TIMEOUT_SECONDS = 30.0
EXTRACT_TIMEOUT_SECONDS = 45.0
BAD_SOURCE_TTL_SECONDS = 24 * 3600.0
DEFAULT_STREAM_LIFETIME_SECONDS = 3600.0
MAX_DURATION_DRIFT = 0.20
MIN_TITLE_MATCH = 0.5

YTDL_OPTIONS: Any = {
    "format": "bestaudio[protocol=https]/bestaudio[protocol=http]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 15,
    "source_address": "0.0.0.0",  # noqa: S104
}

SOURCES = (("scsearch", "soundcloud"), ("ytsearch", "youtube"))

# Versions of a song that aren't the song, unless the original title says so too
UNWANTED_VARIANTS = (
    "live",
    "reaction",
    "slowed",
    "sped up",
    "nightcore",
    "reverb",
    "karaoke",
    "cover",
    "remix",
    "8d",
    "1 hour",
)

WORD_RE = re.compile(r"[a-z0-9]+")
BRACKETS_RE = re.compile(r"[(\[].*?[)\]]")


class Track(Protocol):
    @property
    def spotify_url(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def artist(self) -> str: ...
    @property
    def duration_ms(self) -> int: ...


class ResolveError(RuntimeError):
    """No playable source was found for a track."""


class SourcesUnreachable(ResolveError):
    """Every search backend errored, so the problem is the network or the sites, not the track."""


@dataclass(frozen=True)
class Candidate:
    """One search result, before extraction."""

    source_id: str
    page: str
    title: str
    uploader: str
    duration: float | None


@dataclass(frozen=True)
class Resolved:
    """A playable stream plus the stable source it came from."""

    stream_url: str
    source_id: str
    page: str
    expires_at: float

    def expiring(self, now: float, margin: float) -> bool:
        """Return True if the stream URL expires within `margin` seconds."""
        return self.expires_at - now < margin


def track_key(track: Track) -> str:
    """Stable cache key for a track: its Spotify URL, else its normalized artist and title."""
    if track.spotify_url:
        return track.spotify_url
    return f"{track.artist} - {track.title}".strip().lower()


def stream_expiry(stream_url: str, now: float) -> float:
    """Read the expiry timestamp YouTube (`expire`) and SoundCloud (`expires`) put in URLs."""
    query = parse_qs(urlparse(stream_url).query)
    for key in ("expire", "expires"):
        value = query.get(key, [""])[0]
        if value.isdigit():
            return float(value)
    return now + DEFAULT_STREAM_LIFETIME_SECONDS


def _words(text: str) -> set[str]:
    return set(WORD_RE.findall(text.lower()))


def score_candidate(track: Track, candidate: Candidate) -> tuple[float | None, str]:
    """Score a search result from 0 to 1 against the track, or None with the reason it's wrong."""
    wanted_title = track.title.lower()
    text = f"{candidate.title} {candidate.uploader}".lower()

    for variant in UNWANTED_VARIANTS:
        pattern = rf"\b{re.escape(variant)}\b"
        if re.search(pattern, candidate.title.lower()) and not re.search(pattern, wanted_title):
            return None, f"'{variant}' version"

    expected = track.duration_ms / 1000.0
    duration_score = 0.5
    if expected > 0 and candidate.duration:
        drift = abs(candidate.duration - expected) / expected
        if drift > MAX_DURATION_DRIFT:
            return None, f"length {candidate.duration:.0f}s vs {expected:.0f}s"
        duration_score = 1.0 - drift / MAX_DURATION_DRIFT

    title_words = _words(BRACKETS_RE.sub(" ", track.title)) or _words(track.title)
    title_match = len(title_words & _words(text)) / len(title_words) if title_words else 0.0
    if title_match < MIN_TITLE_MATCH:
        return None, "title doesn't match"

    artist_words = _words(track.artist.split(",")[0])
    artist_match = len(artist_words & _words(text)) / len(artist_words) if artist_words else 0.0

    return 0.5 * title_match + 0.2 * artist_match + 0.3 * duration_score, ""


def _search_sync(query: str, prefix: str, source: str) -> list[Candidate]:
    with yt_dlp.YoutubeDL({**YTDL_OPTIONS, "extract_flat": "in_playlist"}) as finder:
        found = finder.extract_info(f"{prefix}{SEARCH_CANDIDATES}:{query}", download=False)
    candidates: list[Candidate] = []
    for entry in (found or {}).get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("url") or not entry.get("id"):
            continue
        duration = entry.get("duration")
        candidates.append(
            Candidate(
                source_id=f"{source}:{entry['id']}",
                page=str(entry["url"]),
                title=str(entry.get("title") or ""),
                uploader=str(entry.get("uploader") or entry.get("channel") or ""),
                duration=float(duration) if isinstance(duration, int | float) else None,
            )
        )
    return candidates


def _extract_sync(page: str) -> str:
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        info = ydl.extract_info(page, download=False) or {}
    entries = info.get("entries")
    if isinstance(entries, list) and entries:
        info = entries[0]
    stream_url = info.get("url") if isinstance(info, dict) else None
    if not stream_url:
        raise ValueError("no stream URL in the extracted media")
    return str(stream_url)


T = TypeVar("T")


async def _in_thread(func: Callable[..., T], *args: Any, timeout: float) -> T:
    return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)


class TrackResolver:
    """Resolves tracks to streams, remembering the source that worked and the ones that didn't."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    async def search(self, query: str, prefix: str, source: str) -> list[Candidate]:
        """Return up to SEARCH_CANDIDATES results from one site."""
        return await _in_thread(_search_sync, query, prefix, source, timeout=SEARCH_TIMEOUT_SECONDS)

    async def extract(self, page: str) -> str:
        """Return a fresh stream URL for a result page."""
        return await _in_thread(_extract_sync, page, timeout=EXTRACT_TIMEOUT_SECONDS)

    async def _try_source(self, source_id: str, page: str) -> Resolved | None:
        try:
            stream_url = await self.extract(page)
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            logger.warning("Source %s failed (%s); not using it for 24h.", source_id, reason)
            await self.db.add_bad_source(source_id, reason, time.time() + BAD_SOURCE_TTL_SECONDS)
            return None
        now = time.time()
        return Resolved(stream_url, source_id, page, stream_expiry(stream_url, now))

    async def forget(self, track: Track, source_id: str, reason: str) -> None:
        """Drop a source that broke while playing, so the next lookup searches again."""
        await self.db.delete_cached_source(track_key(track))
        await self.db.add_bad_source(source_id, reason, time.time() + BAD_SOURCE_TTL_SECONDS)

    async def refresh(self, resolved: Resolved) -> Resolved:
        """Get a new stream URL from the same source, without searching."""
        stream_url = await self.extract(resolved.page)
        return Resolved(
            stream_url, resolved.source_id, resolved.page, stream_expiry(stream_url, time.time())
        )

    async def resolve(self, track: Track) -> Resolved:
        """Return a stream for `track`: the cached source if it still works, else the best match."""
        key = track_key(track)
        bad = await self.db.get_bad_sources(time.time())

        cached = await self.db.get_cached_source(key)
        if cached is not None and cached[1] not in bad:
            hit = await self._try_source(cached[1], cached[0])
            if hit is not None:
                return hit
            bad.add(cached[1])
        if cached is not None:
            await self.db.delete_cached_source(key)

        query = f"{track.artist} {track.title}".strip()
        scored: list[tuple[float, int, Candidate]] = []
        rejected: list[str] = []
        errors = 0
        for order, (prefix, source) in enumerate(SOURCES):
            try:
                results = await self.search(query, prefix, source)
            except Exception as exc:
                errors += 1
                logger.warning("%s search failed for '%s': %s", source, query, exc)
                continue
            if not results:
                logger.info("%s search found nothing for '%s'.", source, query)
            for candidate in results:
                if candidate.source_id in bad:
                    rejected.append(f"{candidate.source_id}: failed recently")
                    continue
                score, reason = score_candidate(track, candidate)
                if score is None:
                    rejected.append(f"{candidate.source_id}: {reason}")
                    continue
                scored.append((score, order, candidate))

        if errors == len(SOURCES):
            raise SourcesUnreachable(f"every search failed for '{query}'")

        for _score, _order, candidate in sorted(scored, key=lambda item: (-item[0], item[1])):
            hit = await self._try_source(candidate.source_id, candidate.page)
            if hit is not None:
                await self.db.set_cached_source(key, candidate.page, candidate.source_id)
                logger.info(
                    "Resolved '%s' to %s (%s).", query, candidate.source_id, candidate.title
                )
                return hit

        summary = "; ".join(rejected[:4]) or "no search results"
        raise ResolveError(f"no playable match for '{query}' ({summary})")
