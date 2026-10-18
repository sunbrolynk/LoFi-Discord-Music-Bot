"""Per-server audio playback engine and automatic fallback state machine."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
import secrets
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from urllib.parse import urljoin

import aiohttp
import discord
import yt_dlp

from src.database import DatabaseManager, GuildSettings, PlaylistTrack, TrackMetadata

logger = logging.getLogger(__name__)

# rw_timeout (microseconds) makes FFmpeg give up on a stalled connection instead of hanging silent
FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 15000000 -nostdin"
)
FFMPEG_OPTIONS = "-vn -loglevel warning"

RESOLVE_TIMEOUT_SECONDS = 90.0
VOICE_CONNECT_TIMEOUT_SECONDS = 30.0
IDLE_RECOVERY_SECONDS = 90.0
UNHEALTHY_IDLE_SECONDS = 600.0

# Direct 24/7 Icecast LoFi stream used if a scraped web track ever fails
DIRECT_ICECAST_LOFI_URL = "https://stream.laut.fm/lofi"

YTDL_OPTIONS: Any = {
    "format": "bestaudio[protocol=https]/bestaudio[protocol=http]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 15,
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


def backoff_delay(attempt: int, base: float = 5.0, cap: float = 300.0) -> float:
    """Return an exponential backoff delay in seconds for a zero-based attempt number."""
    return min(cap, base * 2.0 ** min(max(0, attempt), 32))


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


ActiveTrack = PlaylistTrack | TrackMetadata
EndHandler = Callable[[Exception | None], Awaitable[None]]


class AudioManager:
    """Coordinates voice connections, playlist playback, and fallback streams per guild."""

    def __init__(self, bot: discord.Client, db: DatabaseManager) -> None:
        self.bot = bot
        self.db = db
        self._active_tracks: dict[int, ActiveTrack | None] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._lofi_radio_cache: list[str] = []
        self._fallback_started_at: dict[int, float] = {}
        self._fallback_failures: dict[int, int] = {}
        # Bumped on every new source so the stopped source's after-callback is ignored
        self._generation: dict[int, int] = {}
        self._queues: dict[int, deque[TrackMetadata]] = {}
        self._queue_interrupted: dict[int, bool] = {}
        self._wanted: set[int] = set()
        self._idle_since: dict[int, float] = {}
        self._recovery_attempts: dict[int, int] = {}
        self._recovery_tasks: dict[int, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._in_flight: dict[int, int] = {}

    @contextlib.contextmanager
    def _starting(self, guild_id: int) -> Iterator[None]:
        """Mark a lookup-and-play as in flight so recovery doesn't start a second one."""
        self._in_flight[guild_id] = self._in_flight.get(guild_id, 0) + 1
        try:
            yield
        finally:
            self._in_flight[guild_id] -= 1

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    def get_active_track(self, guild_id: int) -> ActiveTrack | None:
        """Return the track currently playing for a guild, if any."""
        return self._active_tracks.get(guild_id)

    def queue_length(self, guild_id: int) -> int:
        """Return how many searched tracks are waiting to play in a guild."""
        return len(self._queues.get(guild_id, ()))

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

    @staticmethod
    def _voice_connected(guild: discord.Guild) -> bool:
        voice_client = guild.voice_client
        return isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected()

    @staticmethod
    def _voice_busy(guild: discord.Guild) -> bool:
        voice_client = guild.voice_client
        return (
            isinstance(voice_client, discord.VoiceClient)
            and voice_client.is_connected()
            and (voice_client.is_playing() or voice_client.is_paused())
        )

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
            if voice_client.is_connected():
                if voice_client.channel != channel:
                    await voice_client.move_to(channel)
                return voice_client
            logger.warning("Dropping stale voice connection in guild %s.", guild.id)
            await voice_client.disconnect(force=True)

        logger.info("Connecting to voice channel %s in guild %s.", channel.id, guild.id)
        return await channel.connect(
            self_deaf=True,
            timeout=VOICE_CONNECT_TIMEOUT_SECONDS,
            reconnect=True,
        )

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
                return await self._extract_stream_url(query)
            except Exception as exc:
                logger.warning("Purrple Cat lookup failed (%s); using direct Icecast stream.", exc)
                return DIRECT_ICECAST_LOFI_URL

        try:
            return await self._extract_stream_url(cleaned)
        except Exception as exc:
            logger.warning(
                "Fallback URL %s failed (%s); using direct Icecast stream.", cleaned, exc
            )
            return DIRECT_ICECAST_LOFI_URL

    @staticmethod
    async def _extract_stream_url(query: str) -> str:
        return await asyncio.wait_for(
            asyncio.to_thread(_extract_stream_url_sync, query),
            timeout=RESOLVE_TIMEOUT_SECONDS,
        )

    async def _resolve_with_retries(
        self,
        track: ActiveTrack,
        max_attempts: int = 2,
    ) -> str:
        """Attempt to resolve a track's stream URL via SoundCloud/yt-dlp with retries."""
        query = f"{track.artist} - {track.title}"
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._extract_stream_url(query)
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
                    await asyncio.sleep(backoff_delay(attempt - 1, base=2.0))
        raise RuntimeError(f"Failed to resolve stream after {max_attempts} attempts: {last_exc}")

    async def _set_wanted(self, guild_id: int, wanted: bool) -> None:
        if wanted:
            self._wanted.add(guild_id)
        else:
            self._wanted.discard(guild_id)
        await self.db.set_autoplay(guild_id, wanted)

    async def _play_stream(
        self,
        guild: discord.Guild,
        stream_url: str,
        volume: float,
        on_end: EndHandler,
    ) -> None:
        """Replace whatever is playing with `stream_url`; call under the guild lock."""
        voice_client = await self.ensure_voice_client(guild)
        generation = self._generation.get(guild.id, 0) + 1
        self._generation[guild.id] = generation

        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        source = discord.FFmpegPCMAudio(
            stream_url,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS,
            stderr=sys.stderr.buffer,
        )
        transformed = discord.PCMVolumeTransformer(source, volume=volume)

        def _after(error: Exception | None) -> None:
            self.bot.loop.call_soon_threadsafe(self._source_ended, guild, generation, on_end, error)

        voice_client.play(transformed, after=_after)

    def _source_ended(
        self,
        guild: discord.Guild,
        generation: int,
        on_end: EndHandler,
        error: Exception | None,
    ) -> None:
        if error is not None:
            logger.warning("Stream ended with error in guild %s: %s", guild.id, error)
        if self._generation.get(guild.id) != generation:
            return
        self._spawn(guild, on_end(error))

    def _spawn(self, guild: discord.Guild, coro: Awaitable[None]) -> None:
        task = self.bot.loop.create_task(self._guarded(guild, coro))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _guarded(self, guild: discord.Guild, coro: Awaitable[None]) -> None:
        try:
            await coro
        except Exception:
            logger.exception("Playback handler failed in guild %s.", guild.id)
            self.schedule_recovery(guild, "playback handler error")

    def schedule_recovery(self, guild: discord.Guild, reason: str) -> None:
        """Retry playback with exponential backoff until it works or the guild is stopped."""
        if guild.id not in self._wanted:
            return
        existing = self._recovery_tasks.get(guild.id)
        if existing is not None and not existing.done():
            return
        self._recovery_tasks[guild.id] = self.bot.loop.create_task(self._recover(guild, reason))

    async def _recover(self, guild: discord.Guild, reason: str) -> None:
        guild_id = guild.id
        try:
            while guild_id in self._wanted:
                attempt = self._recovery_attempts.get(guild_id, 0)
                self._recovery_attempts[guild_id] = attempt + 1
                delay = backoff_delay(attempt)
                logger.warning(
                    "Recovering playback in guild %s in %.0fs (attempt %d): %s",
                    guild_id,
                    delay,
                    attempt + 1,
                    reason,
                )
                await asyncio.sleep(delay)
                if (
                    guild_id not in self._wanted
                    or self._voice_busy(guild)
                    or self._in_flight.get(guild_id, 0)
                ):
                    return
                try:
                    await self.resume_from_state(guild)
                except Exception:
                    logger.exception(
                        "Recovery attempt %d failed in guild %s.", attempt + 1, guild_id
                    )
                else:
                    logger.info("Playback recovered in guild %s.", guild_id)
                    return
        finally:
            if self._recovery_tasks.get(guild_id) is asyncio.current_task():
                del self._recovery_tasks[guild_id]

    async def resume_from_state(self, guild: discord.Guild) -> None:
        """Restart whatever the guild was last playing: its fallback stream or its playlist."""
        settings = await self.db.get_guild_settings(guild.id)
        if settings.on_fallback:
            await self.start_fallback(
                guild,
                reason=settings.fallback_reason or "24/7 Fallback Radio",
                announce=False,
            )
        else:
            await self.play_playlist_index(guild)

    async def restore_sessions(self) -> None:
        """Rejoin and resume every guild that was playing before the bot restarted."""
        for guild in self.bot.guilds:
            settings = await self.db.get_guild_settings(guild.id)
            if not settings.autoplay:
                if settings.on_fallback:
                    await self.db.set_fallback_state(guild.id, on_fallback=False)
                continue
            channel = guild.get_channel(settings.voice_channel_id or 0)
            if not isinstance(channel, discord.VoiceChannel):
                logger.warning(
                    "Voice channel for guild %s no longer exists; not resuming playback.",
                    guild.id,
                )
                await self.db.set_autoplay(guild.id, False)
                continue
            logger.info("Resuming playback in guild %s after restart.", guild.id)
            self._wanted.add(guild.id)
            self._spawn(guild, self.resume_from_state(guild))

    def watchdog_tick(self, now: float) -> bool:
        """Recover guilds that should be playing but have gone quiet; False if one is stuck."""
        healthy = True
        for guild_id in list(self._wanted):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            if self._voice_busy(guild):
                self._idle_since.pop(guild_id, None)
                self._recovery_attempts.pop(guild_id, None)
                continue
            idle = now - self._idle_since.setdefault(guild_id, now)
            if idle >= IDLE_RECOVERY_SECONDS:
                self.schedule_recovery(guild, f"silent for {idle:.0f}s")
            if idle >= UNHEALTHY_IDLE_SECONDS:
                healthy = False
        return healthy

    async def start_fallback(
        self,
        guild: discord.Guild,
        reason: str = "Manual fallback activated",
        announce: bool = True,
        force_direct_icecast: bool = False,
    ) -> None:
        """Switch a server immediately to its 24/7 fallback lofi radio stream."""
        with self._starting(guild.id):
            await self._start_fallback(guild, reason, announce, force_direct_icecast)

    async def _start_fallback(
        self,
        guild: discord.Guild,
        reason: str,
        announce: bool,
        force_direct_icecast: bool,
    ) -> None:
        await self._set_wanted(guild.id, True)
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
            if guild.id not in self._wanted:
                return
            self._active_tracks[guild.id] = None
            self._fallback_started_at[guild.id] = time.monotonic()
            await self._play_stream(
                guild,
                stream_url,
                settings.volume,
                lambda error: self._handle_fallback_track_finished(guild, error),
            )

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
        generation = self._generation.get(guild.id)
        settings = await self.db.get_guild_settings(guild.id)
        if not settings.on_fallback or guild.id not in self._wanted:
            return
        if not self._voice_connected(guild):
            self.schedule_recovery(guild, "voice connection lost during fallback stream")
            return

        elapsed = time.monotonic() - self._fallback_started_at.get(guild.id, 0.0)
        # A stream that dies within seconds is broken; back off and use the direct Icecast stream
        use_direct = error is not None or elapsed < 5.0
        if use_direct:
            self._lofi_radio_cache.clear()
            failures = self._fallback_failures.get(guild.id, 0)
            self._fallback_failures[guild.id] = failures + 1
            await asyncio.sleep(backoff_delay(failures, base=2.0, cap=60.0))
            if self._generation.get(guild.id) != generation:
                return
        else:
            self._fallback_failures.pop(guild.id, None)

        await self.start_fallback(
            guild,
            reason=settings.fallback_reason or "24/7 Fallback Radio",
            announce=False,
            force_direct_icecast=use_direct,
        )

    async def _play_track(
        self,
        guild: discord.Guild,
        track: ActiveTrack,
        settings: GuildSettings,
        on_end: EndHandler,
    ) -> bool:
        """Resolve and play one track, or fall back to radio; return whether audio started."""
        with self._starting(guild.id):
            return await self._resolve_and_play(guild, track, settings, on_end)

    async def _resolve_and_play(
        self,
        guild: discord.Guild,
        track: ActiveTrack,
        settings: GuildSettings,
        on_end: EndHandler,
    ) -> bool:
        try:
            stream_url = await self._resolve_with_retries(track)
        except Exception as exc:
            logger.error("Stream extraction failed in guild %s: %s", guild.id, exc)
            if should_fallback_on_error(settings.fallback_triggers):
                await self.start_fallback(
                    guild,
                    reason=f"Failed to stream '{track.title}' by {track.artist}.",
                )
                return True
            await self.notify_control_channel(
                guild,
                title="⚠️ Track Stream Error",
                description=(
                    f"Could not stream **{track.title}** by **{track.artist}** "
                    "and error-fallback is disabled."
                ),
                color=discord.Color.red(),
            )
            return False

        async with self._get_lock(guild.id):
            if guild.id not in self._wanted:
                return False
            await self.db.set_fallback_state(guild.id, on_fallback=False, reason="")
            self._active_tracks[guild.id] = track
            await self._play_stream(guild, stream_url, settings.volume, on_end)
        logger.info("Now playing '%s - %s' in guild %s.", track.artist, track.title, guild.id)
        return True

    async def play_playlist_index(
        self,
        guild: discord.Guild,
        index: int | None = None,
    ) -> None:
        """Play the track at `index` (or saved cursor) in the server's playlist."""
        await self._set_wanted(guild.id, True)
        settings = await self.db.get_guild_settings(guild.id)
        playlist = await self.db.get_playlist(guild.id)

        if not playlist:
            if should_fallback_on_empty(settings.fallback_triggers):
                await self.start_fallback(
                    guild,
                    reason="Server playlist is currently empty.",
                )
                return
            await self._set_wanted(guild.id, False)
            raise ValueError(
                "Your server playlist is empty and automatic empty-fallback is disabled. "
                "Add tracks with `/playlist add` or start fallback with `/fallback start`."
            )

        target_index = settings.current_index if index is None else index
        if target_index < 0 or target_index >= len(playlist):
            target_index = 0

        await self.db.set_current_index(guild.id, target_index)
        await self._play_track(
            guild,
            playlist[target_index],
            settings,
            lambda error: self._handle_track_finished(guild, target_index, error),
        )

    async def _play_next_queued(self, guild: discord.Guild) -> bool:
        """Play the next playable searched track; return whether audio started."""
        queue = self._queues.get(guild.id)
        previous = self._active_tracks.get(guild.id)
        if isinstance(previous, PlaylistTrack):
            self._queue_interrupted[guild.id] = True
        elif previous is None:
            self._queue_interrupted[guild.id] = False
        while queue:
            track = queue.popleft()
            await self._set_wanted(guild.id, True)
            settings = await self.db.get_guild_settings(guild.id)
            # Advance past the cursor only if the queue cut in after a playlist track
            finished: int | None = (
                settings.current_index if self._queue_interrupted.get(guild.id) else None
            )
            started = await self._play_track(
                guild,
                track,
                settings,
                functools.partial(self._handle_track_finished, guild, finished),
            )
            if started:
                return True
        return False

    async def _handle_track_finished(
        self,
        guild: discord.Guild,
        finished_index: int | None,
        error: Exception | None,
    ) -> None:
        """Advance to the next track, or resume the saved cursor if `finished_index` is None."""
        settings = await self.db.get_guild_settings(guild.id)
        if settings.on_fallback or guild.id not in self._wanted:
            return

        if not self._voice_connected(guild):
            self.schedule_recovery(guild, "voice connection lost during playback")
            return

        if error is not None and should_fallback_on_error(settings.fallback_triggers):
            await self.start_fallback(
                guild,
                reason=f"Playback interrupted by stream error: {error}",
            )
            return

        if await self._play_next_queued(guild):
            return

        if finished_index is None:
            await self.play_playlist_index(guild)
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
                await self._set_wanted(guild.id, False)
                await self.notify_control_channel(
                    guild,
                    title="⏹️ Playlist Finished",
                    description="Reached the end of the playlist. Run `/play` or `/playlist resume` to restart.",
                )
            return

        await self.play_playlist_index(guild, index=next_idx)

    async def enqueue(self, guild: discord.Guild, track: TrackMetadata) -> int:
        """Queue a searched track; play it now if the playlist isn't playing. Returns its slot."""
        self._queues.setdefault(guild.id, deque()).append(track)
        settings = await self.db.get_guild_settings(guild.id)
        if self._voice_busy(guild) and not settings.on_fallback:
            return self.queue_length(guild.id)
        await self._play_next_queued(guild)
        return 0

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
        """Skip to the next queued track, or the next track in the server's playlist."""
        if await self._play_next_queued(guild):
            return
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
        """Stop playback, cancel any recovery, and disconnect from the voice channel."""
        await self._set_wanted(guild.id, False)
        self._queues.pop(guild.id, None)
        recovery = self._recovery_tasks.pop(guild.id, None)
        if recovery is not None:
            recovery.cancel()
        async with self._get_lock(guild.id):
            self._generation[guild.id] = self._generation.get(guild.id, 0) + 1
            self._active_tracks[guild.id] = None
            await self.db.set_fallback_state(guild.id, on_fallback=False, reason="")
            voice_client = guild.voice_client
            if isinstance(voice_client, discord.VoiceClient):
                if voice_client.is_playing() or voice_client.is_paused():
                    voice_client.stop()
                await voice_client.disconnect(force=True)
