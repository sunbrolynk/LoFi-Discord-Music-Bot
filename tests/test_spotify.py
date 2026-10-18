"""Unit tests for Spotify link parsing and metadata extraction."""

from __future__ import annotations

import pytest
from src.spotify_client import (
    _extract_track_metadata,
    extract_tracks_from_embed_html,
    parse_spotify_url,
)


def test_parse_spotify_urls_and_uris() -> None:
    """Verify track, album, and playlist URLs and URIs parse accurately."""
    assert parse_spotify_url("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=1234") == (
        "track",
        "4cOdK2wGLETKBW3PvgPWqT",
    )

    assert parse_spotify_url(
        "https://open.spotify.com/intl-de/playlist/37i9dQZF1DWWQRwui0ExPn"
    ) == ("playlist", "37i9dQZF1DWWQRwui0ExPn")

    assert parse_spotify_url("spotify:album:1ATL5GLyefJaxhQzSPVrLX") == (
        "album",
        "1ATL5GLyefJaxhQzSPVrLX",
    )


def test_parse_spotify_url_rejects_invalid_links() -> None:
    """Ensure non-Spotify links raise a descriptive ValueError."""
    with pytest.raises(ValueError, match="Unsupported Spotify link"):
        parse_spotify_url("https://example.com/not-spotify")


def test_extract_track_metadata_handles_valid_and_local_items() -> None:
    """Ensure valid track dicts convert cleanly while local files are skipped."""
    raw = {
        "id": "abc123xyz",
        "name": "Cozy Study Beats",
        "artists": [{"name": "ChilledCat"}, {"name": "Luna"}],
        "duration_ms": 142000,
        "external_urls": {"spotify": "https://open.spotify.com/track/abc123xyz"},
        "is_local": False,
    }
    meta = _extract_track_metadata(raw)
    assert meta is not None
    assert meta.title == "Cozy Study Beats"
    assert meta.artist == "ChilledCat, Luna"
    assert meta.duration_ms == 142000

    assert _extract_track_metadata({"is_local": True, "name": "Local File"}) is None


def test_extract_tracks_from_embed_html() -> None:
    """Ensure public Spotify embed __NEXT_DATA__ JSON parses into TrackMetadata."""
    sample_html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"state":{"data":{"entity":{"trackList":['
        '{"uri":"spotify:track:4cOdK2wGLETKBW3PvgPWqT","title":"Midnight Coffee",'
        '"subtitle":"Purrple Cat","duration":145000}]}}}}}}'
        "</script>"
    )
    tracks = extract_tracks_from_embed_html(sample_html)
    assert len(tracks) == 1
    assert tracks[0].title == "Midnight Coffee"
    assert tracks[0].artist == "Purrple Cat"
    assert tracks[0].spotify_url == "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
