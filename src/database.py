"""Async SQLite persistence layer with strict per-server (guild_id) isolation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiosqlite

VALID_LOOP_MODES = {"playlist", "track", "off"}
VALID_FALLBACK_TRIGGERS = {
    "on_empty_and_error",
    "on_error_only",
    "on_empty_only",
    "manual_only",
}


@dataclass(frozen=True)
class TrackMetadata:
    """Resolved track metadata from Spotify."""

    spotify_url: str
    title: str
    artist: str
    duration_ms: int = 0


@dataclass(frozen=True)
class PlaylistTrack:
    """A single track saved in a server's persistent playlist."""

    id: int
    guild_id: int
    position: int
    spotify_url: str
    title: str
    artist: str
    duration_ms: int
    added_by: str


@dataclass(frozen=True)
class GuildSettings:
    """Per-server configuration and playback state."""

    guild_id: int
    voice_channel_id: int | None
    control_channel_id: int | None
    volume: float
    loop_mode: str
    shuffle: bool
    fallback_triggers: str
    fallback_url: str
    current_index: int
    on_fallback: bool
    fallback_reason: str


class DatabaseManager:
    """Manages per-guild settings and playlists in SQLite."""

    def __init__(self, db_path: Path, default_fallback_url: str) -> None:
        self.db_path = db_path
        self.default_fallback_url = default_fallback_url

    async def init_db(self) -> None:
        """Create tables and indexes if they do not exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    voice_channel_id INTEGER,
                    control_channel_id INTEGER,
                    volume REAL NOT NULL DEFAULT 0.5,
                    loop_mode TEXT NOT NULL DEFAULT 'playlist',
                    shuffle INTEGER NOT NULL DEFAULT 0,
                    fallback_triggers TEXT NOT NULL DEFAULT 'on_empty_and_error',
                    fallback_url TEXT NOT NULL,
                    current_index INTEGER NOT NULL DEFAULT 0,
                    on_fallback INTEGER NOT NULL DEFAULT 0,
                    fallback_reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_playlist_tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    spotify_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    added_by TEXT NOT NULL DEFAULT ''
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_playlist_guild_pos
                ON guild_playlist_tracks (guild_id, position)
                """
            )
            await db.commit()

    async def get_guild_settings(self, guild_id: int) -> GuildSettings:
        """Fetch settings for a guild, initializing defaults if needed."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    """
                    INSERT INTO guild_settings (guild_id, fallback_url)
                    VALUES (?, ?)
                    """,
                    (guild_id, self.default_fallback_url),
                )
                await db.commit()
                cursor = await db.execute(
                    "SELECT * FROM guild_settings WHERE guild_id = ?",
                    (guild_id,),
                )
                row = await cursor.fetchone()

            assert row is not None
            return GuildSettings(
                guild_id=int(row["guild_id"]),
                voice_channel_id=(
                    int(row["voice_channel_id"]) if row["voice_channel_id"] is not None else None
                ),
                control_channel_id=(
                    int(row["control_channel_id"])
                    if row["control_channel_id"] is not None
                    else None
                ),
                volume=float(row["volume"]),
                loop_mode=str(row["loop_mode"]),
                shuffle=bool(row["shuffle"]),
                fallback_triggers=str(row["fallback_triggers"]),
                fallback_url=str(row["fallback_url"]),
                current_index=int(row["current_index"]),
                on_fallback=bool(row["on_fallback"]),
                fallback_reason=str(row["fallback_reason"]),
            )

    async def set_channels(
        self,
        guild_id: int,
        voice_channel_id: int,
        control_channel_id: int,
    ) -> GuildSettings:
        """Bind the designated voice channel and mod control channel for a guild."""
        await self.get_guild_settings(guild_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE guild_settings
                SET voice_channel_id = ?, control_channel_id = ?
                WHERE guild_id = ?
                """,
                (voice_channel_id, control_channel_id, guild_id),
            )
            await db.commit()
        return await self.get_guild_settings(guild_id)

    async def update_playback_settings(
        self,
        guild_id: int,
        *,
        volume: float | None = None,
        loop_mode: str | None = None,
        shuffle: bool | None = None,
    ) -> GuildSettings:
        """Update per-guild volume, loop mode, or shuffle setting."""
        current = await self.get_guild_settings(guild_id)
        new_volume = current.volume if volume is None else max(0.01, min(1.0, volume))
        new_loop = current.loop_mode if loop_mode is None else loop_mode
        if new_loop not in VALID_LOOP_MODES:
            raise ValueError(f"Invalid loop_mode: {new_loop}")
        new_shuffle = current.shuffle if shuffle is None else shuffle

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE guild_settings
                SET volume = ?, loop_mode = ?, shuffle = ?
                WHERE guild_id = ?
                """,
                (new_volume, new_loop, int(new_shuffle), guild_id),
            )
            await db.commit()
        return await self.get_guild_settings(guild_id)

    async def update_fallback_config(
        self,
        guild_id: int,
        *,
        triggers: str,
        fallback_url: str | None = None,
    ) -> GuildSettings:
        """Update fallback trigger rules and optional custom stream URL for a guild."""
        if triggers not in VALID_FALLBACK_TRIGGERS:
            raise ValueError(f"Invalid fallback trigger setting: {triggers}")
        current = await self.get_guild_settings(guild_id)
        new_url = (
            fallback_url.strip() if fallback_url and fallback_url.strip() else current.fallback_url
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE guild_settings
                SET fallback_triggers = ?, fallback_url = ?
                WHERE guild_id = ?
                """,
                (triggers, new_url, guild_id),
            )
            await db.commit()
        return await self.get_guild_settings(guild_id)

    async def set_fallback_state(
        self,
        guild_id: int,
        *,
        on_fallback: bool,
        reason: str = "",
    ) -> GuildSettings:
        """Record whether the guild is currently playing the fallback radio stream."""
        await self.get_guild_settings(guild_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE guild_settings
                SET on_fallback = ?, fallback_reason = ?
                WHERE guild_id = ?
                """,
                (int(on_fallback), reason, guild_id),
            )
            await db.commit()
        return await self.get_guild_settings(guild_id)

    async def set_current_index(self, guild_id: int, index: int) -> None:
        """Update the saved playlist cursor index for a guild."""
        await self.get_guild_settings(guild_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE guild_settings SET current_index = ? WHERE guild_id = ?",
                (max(0, index), guild_id),
            )
            await db.commit()

    async def add_tracks(
        self,
        guild_id: int,
        tracks: list[TrackMetadata],
        added_by: str,
    ) -> int:
        """Append tracks to a guild's playlist and return the new total count."""
        await self.get_guild_settings(guild_id)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(position), 0) FROM guild_playlist_tracks WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            next_pos = (int(row[0]) if row else 0) + 1

            for offset, track in enumerate(tracks):
                await db.execute(
                    """
                    INSERT INTO guild_playlist_tracks
                        (guild_id, position, spotify_url, title, artist, duration_ms, added_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        next_pos + offset,
                        track.spotify_url,
                        track.title,
                        track.artist,
                        track.duration_ms,
                        added_by,
                    ),
                )
            await db.commit()

            count_cursor = await db.execute(
                "SELECT COUNT(*) FROM guild_playlist_tracks WHERE guild_id = ?",
                (guild_id,),
            )
            count_row = await count_cursor.fetchone()
            return int(count_row[0]) if count_row else 0

    async def get_playlist(self, guild_id: int) -> list[PlaylistTrack]:
        """Return all tracks in a guild's playlist ordered by position."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM guild_playlist_tracks
                WHERE guild_id = ?
                ORDER BY position ASC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [
                PlaylistTrack(
                    id=int(r["id"]),
                    guild_id=int(r["guild_id"]),
                    position=int(r["position"]),
                    spotify_url=str(r["spotify_url"]),
                    title=str(r["title"]),
                    artist=str(r["artist"]),
                    duration_ms=int(r["duration_ms"]),
                    added_by=str(r["added_by"]),
                )
                for r in rows
            ]

    async def remove_track_at_position(
        self,
        guild_id: int,
        position: int,
    ) -> PlaylistTrack | None:
        """Remove a track at 1-based position and re-sequence remaining positions."""
        playlist = await self.get_playlist(guild_id)
        target = next((t for t in playlist if t.position == position), None)
        if target is None:
            return None

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM guild_playlist_tracks WHERE id = ? AND guild_id = ?",
                (target.id, guild_id),
            )
            remaining = [t for t in playlist if t.id != target.id]
            for new_pos, track in enumerate(remaining, start=1):
                await db.execute(
                    "UPDATE guild_playlist_tracks SET position = ? WHERE id = ?",
                    (new_pos, track.id),
                )
            await db.commit()
        return target

    async def clear_playlist(self, guild_id: int) -> int:
        """Delete all saved tracks for a guild and reset its playlist cursor."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM guild_playlist_tracks WHERE guild_id = ?",
                (guild_id,),
            )
            await db.execute(
                "UPDATE guild_settings SET current_index = 0 WHERE guild_id = ?",
                (guild_id,),
            )
            await db.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0
