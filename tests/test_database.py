"""Unit tests for per-server SQLite isolation and playlist operations."""

from __future__ import annotations

from pathlib import Path

from src.database import DatabaseManager, TrackMetadata


async def test_multi_guild_settings_and_playlist_isolation(tmp_path: Path) -> None:
    """Ensure two servers maintain completely independent settings and playlists."""
    db = DatabaseManager(
        db_path=tmp_path / "test_lofi.db",
        default_fallback_url="https://example.com/default.mp3",
    )
    await db.init_db()

    guild_a = 1001
    guild_b = 2002

    await db.set_channels(guild_a, voice_channel_id=11, control_channel_id=12)
    await db.set_channels(guild_b, voice_channel_id=21, control_channel_id=22)

    await db.update_playback_settings(guild_a, volume=0.8, loop_mode="track", shuffle=True)
    await db.update_fallback_config(
        guild_b,
        triggers="manual_only",
        fallback_url="https://example.com/guild_b.mp3",
    )

    await db.add_tracks(
        guild_a,
        [
            TrackMetadata("https://open.spotify.com/track/a1", "Midnight Tea", "Artist A", 120000),
            TrackMetadata(
                "https://open.spotify.com/track/a2", "Purple Raindrops", "Artist B", 150000
            ),
        ],
        added_by="mod_a",
    )
    await db.add_tracks(
        guild_b,
        [
            TrackMetadata(
                "https://open.spotify.com/track/b1", "Starlight Study", "Artist C", 180000
            ),
        ],
        added_by="mod_b",
    )

    settings_a = await db.get_guild_settings(guild_a)
    settings_b = await db.get_guild_settings(guild_b)
    playlist_a = await db.get_playlist(guild_a)
    playlist_b = await db.get_playlist(guild_b)

    assert settings_a.voice_channel_id == 11
    assert settings_a.control_channel_id == 12
    assert settings_a.volume == 0.8
    assert settings_a.loop_mode == "track"
    assert settings_a.shuffle is True
    assert settings_a.fallback_triggers == "on_empty_and_error"

    assert settings_b.voice_channel_id == 21
    assert settings_b.control_channel_id == 22
    assert settings_b.fallback_triggers == "manual_only"
    assert settings_b.fallback_url == "https://example.com/guild_b.mp3"

    assert len(playlist_a) == 2
    assert len(playlist_b) == 1
    assert playlist_a[0].title == "Midnight Tea"
    assert playlist_b[0].title == "Starlight Study"


async def test_remove_track_resequences_positions_and_fallback_toggle(tmp_path: Path) -> None:
    """Ensure removing a track re-indexes remaining positions and fallback state toggles cleanly."""
    db = DatabaseManager(
        db_path=tmp_path / "test_reseq.db",
        default_fallback_url="https://example.com/default.mp3",
    )
    await db.init_db()
    guild_id = 3003

    await db.add_tracks(
        guild_id,
        [
            TrackMetadata("https://open.spotify.com/track/1", "Track One", "A"),
            TrackMetadata("https://open.spotify.com/track/2", "Track Two", "B"),
            TrackMetadata("https://open.spotify.com/track/3", "Track Three", "C"),
        ],
        added_by="mod",
    )

    removed = await db.remove_track_at_position(guild_id, 2)
    assert removed is not None
    assert removed.title == "Track Two"

    remaining = await db.get_playlist(guild_id)
    assert [t.position for t in remaining] == [1, 2]
    assert [t.title for t in remaining] == ["Track One", "Track Three"]

    await db.set_fallback_state(guild_id, on_fallback=True, reason="Stream error")
    state_on = await db.get_guild_settings(guild_id)
    assert state_on.on_fallback is True
    assert state_on.fallback_reason == "Stream error"

    await db.set_fallback_state(guild_id, on_fallback=False, reason="")
    state_off = await db.get_guild_settings(guild_id)
    assert state_off.on_fallback is False
