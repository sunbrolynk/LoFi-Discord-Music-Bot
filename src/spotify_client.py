"""Spotify URL parser and async metadata resolver."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from src.database import TrackMetadata

SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-zA-Z\-]+/)?(track|album|playlist)/([a-zA-Z0-9]+)"
)
SPOTIFY_URI_RE = re.compile(r"spotify:(track|album|playlist):([a-zA-Z0-9]+)")


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


def _extract_track_metadata(item: dict[str, Any]) -> TrackMetadata | None:
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


class SpotifyResolver:
    """Resolves Spotify tracks, albums, and playlists into TrackMetadata objects."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        auth_manager = SpotifyClientCredentials(
            client_id=client_id,
            client_secret=client_secret,
        )
        self._client = spotipy.Spotify(auth_manager=auth_manager)

    def _resolve_sync(self, entity_type: str, spotify_id: str) -> list[TrackMetadata]:
        """Synchronously fetch metadata from Spotify (invoked via asyncio.to_thread)."""
        results: list[TrackMetadata] = []

        if entity_type == "track":
            raw_track = self._client.track(spotify_id)
            meta = _extract_track_metadata(raw_track)
            if meta:
                results.append(meta)
            return results

        if entity_type == "album":
            album_info = self._client.album(spotify_id)
            tracks_page = album_info.get("tracks")
            while tracks_page:
                for item in tracks_page.get("items", []):
                    meta = _extract_track_metadata(item)
                    if meta:
                        results.append(meta)
                if tracks_page.get("next"):
                    tracks_page = self._client.next(tracks_page)
                else:
                    break
            return results

        if entity_type == "playlist":
            playlist_page = self._client.playlist_items(
                spotify_id,
                additional_types=("track",),
            )
            while playlist_page:
                for entry in playlist_page.get("items", []):
                    track_obj = (entry or {}).get("track")
                    if isinstance(track_obj, dict):
                        meta = _extract_track_metadata(track_obj)
                        if meta:
                            results.append(meta)
                if playlist_page.get("next"):
                    playlist_page = self._client.next(playlist_page)
                else:
                    break
            return results

        raise ValueError(f"Unsupported entity type: {entity_type}")

    async def resolve_url(self, url_or_uri: str) -> list[TrackMetadata]:
        """Parse and resolve a Spotify URL/URI into a list of TrackMetadata."""
        entity_type, spotify_id = parse_spotify_url(url_or_uri)
        return await asyncio.to_thread(self._resolve_sync, entity_type, spotify_id)
