"""Per-server audio playback engine and automatic fallback state machine."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import sys
import time
from typing import Any
from urllib.parse import urljoin

import aiohttp
import discord
import yt_dlp

from src.database import DatabaseManager, PlaylistTrack

logger = logging.getLogger(__name__)

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTIONS = "-vn"

# Direct 24/7 Icecast LoFi stream used if a scraped web track ever fails
DIRECT_ICECAST_LOFI_URL = "https://stream.laut.fm/lofi"

YTDL_OPTIONS: dict[str, Any] = {
    "format": "bestaudio[protocol=https]/bestaudio[protocol=http]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",  # noqa: S104
}

AUDIO_FILE_RE = re.compile(
    r"[\"'\`]([^\"'\`\s<>]+?\.(?:mp3|ogg|m4a)(?:\?[^\"'\`\s<>]*)?)[\"'\`]",
    re.IGNORECASE,
)
SCRIPT_SRC_RE = re.compile(
    r"<script[^>]+src=[\"'\`]([^\"'\`]+\.js(?:\?[^\"'\`]*)?)[\"'\`]",
    re.IGNORECASE,
)

LOFI_RADIO_QUERIES = [
    "Purrple Cat - Aurora",
    "Purrple Cat - Midnight Tea",
    "Purrple Cat - Starlight",
    "Purrple Cat - Cozy Place",
    "Purrple Cat - Crescent Moon",
    "Purrple Cat - Wishing Well",
]


def should_fallback_on_empty(triggers: str) -> bool:
    """Return True if fallback should activate when the playlist is empty or finishes."""
    return triggers in {"on_empty_and_error", "on_empty_only"}


def should_fallback_on_error(triggers: str) -> bool:
    """Return True if fallback should activate when a track fails to stream."""
    return triggers in {"on_empty_and_error", "on_error_only"}


def extract_audio_urls_from_text(text: str, base_url: str) -> list[str]:
    """Extract and normalize direct audio file URLs (.mp3/.ogg/.m4a) from HTML or JS."""
    found: list[str] = []
    for match in AUDIO_FILE_RE.findall(text):
        full_url = urljoin(base_url, match)
        if full_url.startswith(("http://", "https://")) and full_url not in found:
            found.append(full_url)
    return found


def compute_next_index(
    current_index: int,
    total_tracks: int,
    loop_mode: str,
    shuffle: bool,
) -> int | None:
    """Calculate the next playlist track index (0-based) or None if playback should end."""
    if total_tracks <= 0:
        return None

    if loop_mode == "track":
        return min(current_index, total_tracks - 1)

    if shuffle and total_tracks > 1:
        candidates = [idx for idx in range(total_tracks) if idx != current_index]
        return candidates[secrets.randbelow(len(candidates))]

    next_idx = current_index + 1
    if next_idx < total_tracks:
        return next_idx

    if loop_mode == "playlist":
        return 0

    return None


def _extract_stream_url_sync(search_query: str) -> str:
    """Extract a direct progressive HTTP audio stream URL via SoundCloud or YouTube."""
    if search_query.startswith(("http://", "https://")):
        candidates = [search_query]
    else:
        # Try SoundCloud first (no datacenter IP blocks or 403s), then YouTube
        candidates = [f"scsearch1:{search_query}", f"ytsearch1:{search_query}"]

    last_err: Exception | None = None
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        for candidate in candidates:
            try:
                info = ydl.extract_info(candidate, download=False)
                if not info:
                    continue
                entries = info.get("entries")
                target = entries[0] if isinstance(entries, list) and len(entries) > 0 else info
                if not isinstance(target, dict):
                    continue
                stream_url = target.get("url")
                if stream_url:
                    logger.info(
                        "Resolved audio stream via '%s': %s",
                        candidate,
                        target.get("title", search_query),
                    )
                    return str(stream_url)
            except Exception as exc:
                last_err = exc
                logger.warning("Candidate '%s' failed: %s", candidate, exc)

    raise RuntimeError(f"No playable stream found for '{search_query}': {last_err}")


class AudioManager:
    """Coordinates voice connections, playlist playback, and fallback streams per guild."""

    def __init__(self, bot: discord.Client, db: DatabaseManager) -> None:
        self.bot = bot
        self.db = db
        self._active_tracks: dict[int, PlaylistTrack | None] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._lofi_radio_cache: list[str] = []
        self._fallback_started_at: dict[int, float] = {}

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    def get_active_track(self, guild_id: int) -> PlaylistTrack | None:
        """Return the currently playing PlaylistTrack for a guild, if any."""
        return self._active_tracks.get(guild_id)

    async def notify_control_channel(
        self,
        guild: discord.Guild,
        title: str,
        description: str,
        color: discord.Color | None = None,
    ) -> None:
        """Send a status embed to the server's configured mod control channel."""
        settings = await self.db.get_guild_settings(guild.id)
        if not settings.control_channel_id:
            return
        channel = guild.get_channel(settings.control_channel_id)
        if isinstance(channel, discord.TextChannel):
            embed_color = color if color is not None else discord.Color.purple()
            embed = discord.Embed(title=title, description=description, color=embed_color)
            try:
                await channel.send(embed=embed)
            except discord.DiscordException as exc:
                logger.warning("Failed to send mod channel notice in %s: %s", guild.id, exc)

    async def ensure_voice_client(self, guild: discord.Guild) -> discord.VoiceClient:
        """Connect to or move to the server's configured voice channel."""
        settings = await self.db.get_guild_settings(guild.id)
        if not settings.voice_channel_id:
            raise ValueError("No voice channel configured for this server. Run `/setup` first.")
        channel = guild.get_channel(settings.voice_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise ValueError(
                "Configured voice channel was not found. Re-run `/setup` to update it."
            )

        voice_client = guild.voice_client
        if isinstance(voice_client, discord.VoiceClient):
            if voice_client.channel != channel:
                await voice_client.move_to(channel)
            return voice_client

        return await channel.connect(self_deaf=True)

    async def _resolve_fallback_stream_url(
        self,
        fallback_url: str,
        force_direct_icecast: bool = False,
    ) -> str:
        """Resolve a playable stream URL from lofi.radio or a direct stream URL."""
        if force_direct_icecast:
            return DIRECT_ICECAST_LOFI_URL

        cleaned = fallback_url.strip()
        if not cleaned.startswith(("http://", "https://")):
            cleaned = f"https://{cleaned}"

        if cleaned.lower().endswith((".mp3", ".ogg", ".m4a", ".aac", ".m3u8")):
            return cleaned

        if "lofi.radio" in cleaned.lower():
            if self._lofi_radio_cache:
                return self._lofi_radio_cache[secrets.randbelow(len(self._lofi_radio_cache))]

            discovered: list[str] = []
            timeout = aiohttp.ClientTimeout(total=10)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(cleaned) as resp:
                        if resp.status == 200:
                            html = await resp.text()
                            discovered.extend(extract_audio_urls_from_text(html, cleaned))
                            script_urls = [urljoin(cleaned, s) for s in SCRIPT_SRC_RE.findall(html)]
                            for script_url in script_urls[:5]:
                                async with session.get(script_url) as s_resp:
                                    if s_resp.status == 200:
                                        js_text = await s_resp.text()
                                        for u in extract_audio_urls_from_text(js_text, cleaned):
                                            if u not in discovered:
                                                discovered.append(u)
            except Exception as exc:
                logger.warning("Could not scrape lofi.radio directly: %s", exc)

            if discovered:
                self._lofi_radio_cache = discovered
                return discovered[secrets.randbelow(len(discovered))]

            # Resolve Purrple Cat (lofi.radio's artist catalog) via SoundCloud/yt-dlp
            query = LOFI_RADIO_QUERIES[secrets.randbelow(len(LOFI_RADIO_QUERIES))]
            try:
                return await asyncio.to_thread(_extract_stream_url_sync, query)
            except Exception as exc:
                logger.warning("Purrple Cat lookup failed (%s); using direct Icecast stream.", exc)
                return DIRECT_ICECAST_LOFI_URL

        try:
            return await asyncio.to_thread(_extract_stream_url_sync, cleaned)
        except Exception:
            return DIRECT_ICECAST_LOFI_URL

    async def _resolve_with_retries(
        self,
        track: PlaylistTrack,
        max_attempts: int = 2,
    ) -> str:
        """Attempt to resolve a track's stream URL via SoundCloud/yt-dlp with retries."""
        query = f"{track.artist} - {track.title}"
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await asyncio.to_thread(_extract_stream_url_sync, query)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Stream resolve attempt %d/%d failed for '%s': %s",
                    attempt,
                    max_attempts,
                    query,
                    exc,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(1.0)
        raise RuntimeError(f"Failed to resolve stream after {max_attempts} attempts: {last_exc}")

    async def start_fallback(
        self,
        guild: discord.Guild,
        reason: str = "Manual fallback activated",
        announce: bool = True,
        force_direct_icecast: bool = False,
    ) -> None:
        """Switch a server immediately to its 24/7 fallback lofi radio stream."""
        settings = await self.db.set_fallback_state(
            guild.id,
            on_fallback=True,
            reason=reason,
        )
        stream_url = await self._resolve_fallback_stream_url(
            settings.fallback_url,
            force_direct_icecast=force_direct_icecast,
        )
        logger.info("Starting fallback stream in guild %s: %s", guild.id, stream_url)

        async with self._get_lock(guild.id):
            voice_client = await self.ensure_voice_client(guild)
            self._active_tracks[guild.id] = None
            self._fallback_started_at[guild.id] = time.monotonic()

            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()

            source = discord.FFmpegPCMAudio(
                stream_url,
                before_options=FFMPEG_BEFORE_OPTIONS,
                options=FFMPEG_OPTIONS,
                stderr=sys.stderr.buffer,
            )
            transformed = discord.PCMVolumeTransformer(source, volume=settings.volume)

            def _after_fallback(error: Exception | None) -> None:
                if error:
                    logger.warning("Fallback stream ended with error in %s: %s", guild.id, error)
                asyncio.run_coroutine_threadsafe(
                    self._handle_fallback_track_finished(guild, error),
                    self.bot.loop,
                )

            voice_client.play(transformed, after=_after_fallback)

        if announce:
            await self.notify_control_channel(
                guild,
                title="🌙 Switched to 24/7 Fallback LoFi Stream",
                description=(
                    f"**Reason:** {reason}\n"
                    f"**Radio Source:** `{settings.fallback_url}`\n"
                    "Run `/playlist resume` in this channel anytime to jump back to your playlist."
                ),
            )

    async def _handle_fallback_track_finished(
        self,
        guild: discord.Guild,
        error: Exception | None = None,
    ) -> None:
        """Queue the next fallback track or switch to direct Icecast if the stream died early."""
        settings = await self.db.get_guild_settings(guild.id)
        if not settings.on_fallback:
            return
        voice_client = guild.voice_client
        if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            return

        elapsed = time.monotonic() - self._fallback_started_at.get(guild.id, 0.0)
        # If the stream died in under 5 seconds, switch to guaranteed direct Icecast stream
        use_direct = error is not None or elapsed < 5.0
        if use_direct:
            self._lofi_radio_cache.clear()
            await asyncio.sleep(2.0)

        try:
            await self.start_fallback(
                guild,
                reason=settings.fallback_reason or "24/7 Fallback Radio",
                announce=False,
                force_direct_icecast=use_direct,
            )
        except Exception as exc:
            logger.error("Failed to loop fallback stream in guild %s: %s", guild.id, exc)

    async def play_playlist_index(
        self,
        guild: discord.Guild,
        index: int | None = None,
    ) -> None:
        """Play the track at `index` (or saved cursor) in the server's playlist."""
        settings = await self.db.get_guild_settings(guild.id)
        playlist = await self.db.get_playlist(guild.id)

        if not playlist:
            if should_fallback_on_empty(settings.fallback_triggers):
                await self.start_fallback(
                    guild,
                    reason="Server playlist is currently empty.",
                )
                return
            raise ValueError(
                "Your server playlist is empty and automatic empty-fallback is disabled. "
                "Add tracks with `/playlist add` or start fallback with `/fallback start`."
            )

        target_index = settings.current_index if index is None else index
        if target_index < 0 or target_index >= len(playlist):
            target_index = 0

        track = playlist[target_index]
        await self.db.set_current_index(guild.id, target_index)

        try:
            stream_url = await self._resolve_with_retries(track)
        except Exception as exc:
            logger.error("Stream extraction failed in guild %s: %s", guild.id, exc)
            if should_fallback_on_error(settings.fallback_triggers):
                await self.start_fallback(
                    guild,
                    reason=f"Failed to stream '{track.title}' by {track.artist}.",
                )
                return
            await self.notify_control_channel(
                guild,
                title="⚠️ Track Stream Error",
                description=(
                    f"Could not stream **{track.title}** by **{track.artist}** "
                    "and error-fallback is disabled."
                ),
                color=discord.Color.red(),
            )
            return

        async with self._get_lock(guild.id):
            voice_client = await self.ensure_voice_client(guild)
            await self.db.set_fallback_state(guild.id, on_fallback=False, reason="")
            self._active_tracks[guild.id] = track

            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()

            source = discord.FFmpegPCMAudio(
                stream_url,
                before_options=FFMPEG_BEFORE_OPTIONS,
                options=FFMPEG_OPTIONS,
                stderr=sys.stderr.buffer,
            )
            transformed = discord.PCMVolumeTransformer(source, volume=settings.volume)

            def _after_playback(error: Exception | None) -> None:
                if error:
                    logger.error("Voice playback error in guild %s: %s", guild.id, error)
                asyncio.run_coroutine_threadsafe(
                    self._handle_track_finished(guild, target_index, error),
                    self.bot.loop,
                )

            voice_client.play(transformed, after=_after_playback)

    async def _handle_track_finished(
        self,
        guild: discord.Guild,
        finished_index: int,
        error: Exception | None,
    ) -> None:
        """Advance to the next track or trigger fallback when a track finishes."""
        settings = await self.db.get_guild_settings(guild.id)
        if settings.on_fallback:
            return

        voice_client = guild.voice_client
        if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            return

        if error is not None and should_fallback_on_error(settings.fallback_triggers):
            await self.start_fallback(
                guild,
                reason=f"Playback interrupted by stream error: {error}",
            )
            return

        playlist = await self.db.get_playlist(guild.id)
        next_idx = compute_next_index(
            current_index=finished_index,
            total_tracks=len(playlist),
            loop_mode=settings.loop_mode,
            shuffle=settings.shuffle,
        )

        if next_idx is None:
            self._active_tracks[guild.id] = None
            if should_fallback_on_empty(settings.fallback_triggers):
                await self.start_fallback(
                    guild,
                    reason="Reached the end of the server playlist (loop is off).",
                )
            else:
                await self.notify_control_channel(
                    guild,
                    title="⏹️ Playlist Finished",
                    description="Reached the end of the playlist. Run `/play` or `/playlist resume` to restart.",
                )
            return

        await self.play_playlist_index(guild, index=next_idx)

    async def resume_playlist(
        self,
        guild: discord.Guild,
        start_from: str = "saved_position",
    ) -> None:
        """Exit fallback radio mode and resume the server's custom Spotify playlist."""
        settings = await self.db.get_guild_settings(guild.id)
        target_index = 0 if start_from == "beginning" else settings.current_index
        await self.play_playlist_index(guild, index=target_index)

    async def skip(self, guild: discord.Guild) -> None:
        """Skip to the next track in the server's playlist."""
        settings = await self.db.get_guild_settings(guild.id)
        playlist = await self.db.get_playlist(guild.id)
        if not playlist:
            raise ValueError("Cannot skip because the server playlist is empty.")
        next_idx = compute_next_index(
            current_index=settings.current_index,
            total_tracks=len(playlist),
            loop_mode="playlist" if settings.loop_mode == "off" else settings.loop_mode,
            shuffle=settings.shuffle,
        )
        await self.play_playlist_index(guild, index=next_idx or 0)

    async def set_volume(self, guild: discord.Guild, level_percent: int) -> float:
        """Set playback volume (1-100) for a guild and apply immediately if playing."""
        normalized = max(0.01, min(1.0, level_percent / 100.0))
        await self.db.update_playback_settings(guild.id, volume=normalized)
        voice_client = guild.voice_client
        if isinstance(voice_client, discord.VoiceClient) and isinstance(
            voice_client.source, discord.PCMVolumeTransformer
        ):
            voice_client.source.volume = normalized
        return normalized

    async def stop_and_disconnect(self, guild: discord.Guild) -> None:
        """Stop playback and disconnect from the voice channel."""
        self._active_tracks[guild.id] = None
        await self.db.set_fallback_state(guild.id, on_fallback=False, reason="")
        voice_client = guild.voice_client
        if isinstance(voice_client, discord.VoiceClient):
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
            await voice_client.disconnect(force=True)
