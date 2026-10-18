"""Spotify URL parser and async metadata resolver with public embed fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from src.database import TrackMetadata

logger = logging.getLogger(__name__)

SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-zA-Z\-]+/)?(track|album|playlist)/([a-zA-Z0-9]+)"
)
SPOTIFY_URI_RE = re.compile(r"spotify:(track|album|playlist):([a-zA-Z0-9]+)")
NEXT_DATA_RE = re.compile(
    r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)


def parse_spotify_url(url_or_uri: str) -> tuple[str, str]:
    """Extract ('track' | 'album' | 'playlist', spotify_id) from a Spotify URL or URI."""
    cleaned = url_or_uri.strip()
    url_match = SPOTIFY_URL_RE.search(cleaned)
    if url_match:
        return url_match.group(1).lower(), url_match.group(2)

    uri_match = SPOTIFY_URI_RE.search(cleaned)
    if uri_match:
        return uri_match.group(1).lower(), uri_match.group(2)

    raise ValueError(
        "Unsupported Spotify link. Provide a valid Spotify track, album, or playlist URL."
    )


def _extract_track_metadata(item: dict[str, Any] | None) -> TrackMetadata | None:
    """Convert a raw Spotipy track dict into a TrackMetadata object."""
    if not item or item.get("is_local"):
        return None
    title = str(item.get("name") or "").strip()
    artists_raw = item.get("artists") or []
    artists = ", ".join(str(a.get("name", "")).strip() for a in artists_raw if a.get("name"))
    if not title:
        return None
    track_id = item.get("id")
    external_urls = item.get("external_urls") or {}
    spotify_url = external_urls.get("spotify") or (
        f"https://open.spotify.com/track/{track_id}" if track_id else ""
    )
    duration_ms = int(item.get("duration_ms") or 0)
    return TrackMetadata(
        spotify_url=str(spotify_url),
        title=title,
        artist=artists or "Unknown Artist",
        duration_ms=duration_ms,
    )


def extract_tracks_from_embed_html(html: str, default_url: str = "") -> list[TrackMetadata]:
    """Extract tracks from a Spotify public embed page's __NEXT_DATA__ JSON payload."""
    match = NEXT_DATA_RE.search(html)
    if not match:
        return []

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    entity = (
        payload.get("props", {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity", {})
    )
    if not isinstance(entity, dict):
        return []

    results: list[TrackMetadata] = []
    track_list = entity.get("trackList")
    if isinstance(track_list, list):
        for entry in track_list:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            artist = str(entry.get("subtitle") or "").strip().replace("\xa0", " ")
            if not title:
                continue
            uri = str(entry.get("uri") or "")
            track_id = uri.split(":")[-1] if ":" in uri else ""
            spotify_url = f"https://open.spotify.com/track/{track_id}" if track_id else default_url
            duration_ms = int(entry.get("duration") or 0)
            results.append(
                TrackMetadata(
                    spotify_url=spotify_url,
                    title=title,
                    artist=artist or "Unknown Artist",
                    duration_ms=duration_ms,
                )
            )
        return results

    # Single track embed fallback
    if entity.get("type") == "track" and entity.get("name"):
        meta = _extract_track_metadata(entity)
        if meta:
            results.append(meta)

    return results


class SpotifyResolver:
    """Resolves Spotify tracks, albums, and playlists into TrackMetadata objects."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        auth_manager = SpotifyClientCredentials(
            client_id=client_id,
            client_secret=client_secret,
        )
        self._client = spotipy.Spotify(auth_manager=auth_manager)

    def _resolve_sync(self, entity_type: str, spotify_id: str) -> list[TrackMetadata]:
        """Synchronously fetch metadata from Spotify Web API (invoked via asyncio.to_thread)."""
        results: list[TrackMetadata] = []

        if entity_type == "track":
            raw_track = self._client.track(spotify_id)
            if isinstance(raw_track, dict):
                meta = _extract_track_metadata(raw_track)
                if meta:
                    results.append(meta)
            return results

        if entity_type == "album":
            album_info = self._client.album(spotify_id)
            if not isinstance(album_info, dict):
                return results
            tracks_page = album_info.get("tracks")
            while isinstance(tracks_page, dict):
                for item in tracks_page.get("items", []):
                    if isinstance(item, dict):
                        meta = _extract_track_metadata(item)
                        if meta:
                            results.append(meta)
                if tracks_page.get("next"):
                    tracks_page = self._client.next(tracks_page)
                else:
                    break
            return results

        if entity_type == "playlist":
            page = self._client._get(
                f"playlists/{spotify_id}/items",
                limit=100,
                offset=0,
                additional_types="track",
            )
            while isinstance(page, dict):
                for entry in page.get("items", []):
                    track_obj = (entry or {}).get("item") or (entry or {}).get("track")
                    if isinstance(track_obj, dict):
                        meta = _extract_track_metadata(track_obj)
                        if meta:
                            results.append(meta)
                if page.get("next"):
                    page = self._client.next(page)
                else:
                    break
            return results

        raise ValueError(f"Unsupported entity type: {entity_type}")

    async def _resolve_via_embed(
        self,
        entity_type: str,
        spotify_id: str,
    ) -> list[TrackMetadata]:
        """Fallback to Spotify's public embed page when Dev Mode blocks playlist reads with 403."""
        embed_url = f"https://open.spotify.com/embed/{entity_type}/{spotify_id}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        }
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(embed_url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Spotify embed fallback returned HTTP {resp.status}")
                html = await resp.text()
        return extract_tracks_from_embed_html(
            html,
            default_url=f"https://open.spotify.com/{entity_type}/{spotify_id}",
        )

    def _search_sync(self, query: str) -> TrackMetadata | None:
        result = self._client.search(q=query, limit=1, type="track")
        items = ((result or {}).get("tracks") or {}).get("items") or []
        return _extract_track_metadata(items[0]) if items else None

    async def search_track(self, query: str) -> TrackMetadata | None:
        """Return the best Spotify match for a free-text song search, or None."""
        return await asyncio.to_thread(self._search_sync, query)

    async def resolve_url(self, url_or_uri: str) -> list[TrackMetadata]:
        """Parse and resolve a Spotify URL/URI into a list of TrackMetadata."""
        entity_type, spotify_id = parse_spotify_url(url_or_uri)
        try:
            tracks = await asyncio.to_thread(self._resolve_sync, entity_type, spotify_id)
            if tracks:
                return tracks
        except Exception as exc:
            logger.warning(
                "Spotify API returned error for %s/%s (%s); falling back to public embed.",
                entity_type,
                spotify_id,
                exc,
            )
        return await self._resolve_via_embed(entity_type, spotify_id)
