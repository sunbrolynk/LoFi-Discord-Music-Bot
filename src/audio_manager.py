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
from collections.abc import Awaitable, Callable, Iterator, Sequence
from urllib.parse import quote, urljoin

import aiohttp
import discord

from src.config import DEFAULT_BACKUP_STREAM_URLS
from src.database import DatabaseManager, GuildSettings, PlaylistTrack, TrackMetadata
from src.resolver import Resolved, SourcesUnreachable, TrackResolver, track_key

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

PREFETCH_COUNT = 3
MAX_CONSECUTIVE_FAILURES = 5
FAILED_TRACK_TTL_SECONDS = 3600.0
EXPIRY_MARGIN_SECONDS = 600.0
QUICK_FAILURE_SECONDS = 10.0
RADIO_RETRY_SECONDS = 300.0
# Fallback reasons starting with this were chosen by the bot, so it may leave radio by itself
AUTO_RADIO_PREFIX = "Auto:"
RADIO_MODE_REASON = "Radio mode"
DEAD_STREAM_TTL_SECONDS = 1800.0
STREAM_CHECK_TIMEOUT_SECONDS = 10.0

AUDIO_FILE_RE = re.compile(
    r"[\"'\`]([^\"'\`<>\n]+?\.(?:mp3|ogg|m4a)(?:\?[^\"'\`\s<>]*)?)[\"'\`]",
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
        full_url = urljoin(base_url, quote(match, safe=":/?=&%#"))
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


ActiveTrack = PlaylistTrack | TrackMetadata
EndHandler = Callable[[Exception | None], Awaitable[None]]


class AudioManager:
    """Coordinates voice connections, playlist playback, and fallback streams per guild."""

    def __init__(
        self,
        bot: discord.Client,
        db: DatabaseManager,
        backup_streams: Sequence[str] = DEFAULT_BACKUP_STREAM_URLS,
    ) -> None:
        self.bot = bot
        self.db = db
        self.backup_streams = list(backup_streams)
        self._dead_streams: dict[str, float] = {}
        self.resolver = TrackResolver(db)
        self._prefetched: dict[int, dict[str, Resolved]] = {}
        self._failed_tracks: dict[int, dict[str, tuple[str, float]]] = {}
        self._plan: dict[int, list[int]] = {}
        self._prefetch_tasks: dict[int, asyncio.Task[None]] = {}
        self._prefetch_generation: dict[int, int] = {}
        self._radio_return_tasks: dict[int, asyncio.Task[None]] = {}
        self._consecutive_failures: dict[int, int] = {}
        self._skips: dict[int, deque[float]] = {}
        self._track_started_at: dict[int, float] = {}
        self._playing_source: dict[int, str] = {}
        self._notice_problems: set[int] = set()
        self._fallback_playing: dict[int, str] = {}
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
        if not isinstance(channel, discord.TextChannel):
            self._log_notice_problem(guild, f"channel {settings.control_channel_id}")
            return
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            self._log_notice_problem(guild, f"#{channel.name} ({channel.id})")
            return
        embed_color = color if color is not None else discord.Color.purple()
        embed = discord.Embed(title=title, description=description, color=embed_color)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            self._log_notice_problem(guild, f"#{channel.name} ({channel.id})")
        except discord.DiscordException as exc:
            logger.warning("Failed to send mod channel notice in %s: %s", guild.id, exc)
        else:
            self._notice_problems.discard(guild.id)

    def _log_notice_problem(self, guild: discord.Guild, channel_label: str) -> None:
        """Log one actionable line per guild until notices work again."""
        if guild.id in self._notice_problems:
            return
        self._notice_problems.add(guild.id)
        logger.warning(
            "Can't post notices to mod channel %s in server '%s' (%s): give the bot "
            "View Channel, Send Messages and Embed Links there. Notices are skipped until then.",
            channel_label,
            guild.name,
            guild.id,
        )

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
        use_backup: bool = False,
    ) -> str:
        """Return a reachable fallback stream: the server's own source, then the backups."""
        if not use_backup:
            for _ in range(3):
                candidate = await self._primary_fallback_url(fallback_url)
                if candidate is None:
                    break
                if await self._stream_reachable(candidate):
                    return candidate
        now = time.monotonic()
        alive = [u for u in self.backup_streams if self._dead_streams.get(u, 0.0) <= now]
        for url in alive:
            if await self._stream_reachable(url):
                return url
        # Nothing passed: recheck the ones skipped for an earlier failure rather than go silent
        for url in self.backup_streams:
            if url not in alive and await self._stream_reachable(url, recheck=True):
                return url
        raise RuntimeError("No fallback stream is reachable; every station failed its check.")

    async def _primary_fallback_url(self, fallback_url: str) -> str | None:
        """Turn a server's fallback setting into a stream URL, or None if it can't."""
        cleaned = fallback_url.strip()
        if not cleaned.startswith(("http://", "https://")):
            cleaned = f"https://{cleaned}"

        if cleaned.lower().endswith((".mp3", ".ogg", ".m4a", ".aac", ".m3u8")):
            return cleaned

        if "lofi.radio" in cleaned.lower():
            if not self._lofi_radio_cache:
                self._lofi_radio_cache = await self._scrape_lofi_radio(cleaned)
            now = time.monotonic()
            live = [u for u in self._lofi_radio_cache if self._dead_streams.get(u, 0.0) <= now]
            if live:
                return live[secrets.randbelow(len(live))]
            query = LOFI_RADIO_QUERIES[secrets.randbelow(len(LOFI_RADIO_QUERIES))]
            artist, _, title = query.partition(" - ")
            try:
                return (await self.resolver.resolve(TrackMetadata("", title, artist))).stream_url
            except Exception as exc:
                logger.warning("Purrple Cat lookup failed (%s); using a backup stream.", exc)
                return None

        try:
            return await self.resolver.extract(cleaned)
        except Exception as exc:
            logger.warning("Fallback URL %s failed (%s); using a backup stream.", cleaned, exc)
            return None

    @staticmethod
    async def _scrape_lofi_radio(page_url: str) -> list[str]:
        """Collect lofi.radio's song files, which it serves from its songs/ folder."""
        discovered: list[str] = []
        songs_base = urljoin(page_url.rstrip("/") + "/", "songs/")
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(page_url) as resp:
                    if resp.status != 200:
                        logger.warning("lofi.radio returned HTTP %s.", resp.status)
                        return discovered
                    html = await resp.text()
                discovered.extend(extract_audio_urls_from_text(html, songs_base))
                script_urls = [urljoin(page_url, s) for s in SCRIPT_SRC_RE.findall(html)]
                for script_url in script_urls[:5]:
                    async with session.get(script_url) as s_resp:
                        if s_resp.status == 200:
                            js_text = await s_resp.text()
                            for u in extract_audio_urls_from_text(js_text, songs_base):
                                if u not in discovered:
                                    discovered.append(u)
        except Exception as exc:
            logger.warning("Could not scrape lofi.radio: %s", exc)
        return discovered

    async def _stream_reachable(self, url: str, recheck: bool = False) -> bool:
        """Probe a stream URL, remembering a dead one for a while so it isn't hit every time."""
        now = time.monotonic()
        if not recheck and self._dead_streams.get(url, 0.0) > now:
            return False
        timeout = aiohttp.ClientTimeout(total=STREAM_CHECK_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"Range": "bytes=0-0"}) as resp:
                    status = resp.status
        except (TimeoutError, aiohttp.ClientError) as exc:
            reason = str(exc) or type(exc).__name__
        else:
            if status < 400:
                self._dead_streams.pop(url, None)
                return True
            reason = f"HTTP {status}"
        self.mark_stream_dead(url, reason)
        return False

    def mark_stream_dead(self, url: str, reason: str) -> None:
        """Skip a fallback stream for DEAD_STREAM_TTL_SECONDS."""
        self._dead_streams[url] = time.monotonic() + DEAD_STREAM_TTL_SECONDS
        logger.warning(
            "Fallback stream %s is down (%s); skipping it for %d min.",
            url,
            reason,
            DEAD_STREAM_TTL_SECONDS // 60,
        )

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
        if settings.on_fallback and not settings.fallback_reason.startswith(AUTO_RADIO_PREFIX):
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

    async def start_radio(self, channel: discord.VoiceChannel, stream_url: str) -> None:
        """Bind radio mode's channel and stream, then play it as a fallback that never ends."""
        guild = channel.guild
        settings = await self.db.get_guild_settings(guild.id)
        await self.db.set_channels(guild.id, channel.id, settings.control_channel_id)
        await self.db.update_fallback_config(
            guild.id, triggers="manual_only", fallback_url=stream_url
        )
        logger.info("Radio mode: playing %s in voice channel %s.", stream_url, channel.id)
        # Wanted before the first try, so a stream that's down at startup is retried with backoff
        self._wanted.add(guild.id)
        self._spawn(guild, self.start_fallback(guild, reason=RADIO_MODE_REASON, announce=False))

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
        use_backup: bool = False,
    ) -> None:
        """Switch a server immediately to its 24/7 fallback lofi radio stream."""
        with self._starting(guild.id):
            await self._start_fallback(guild, reason, announce, use_backup)

    async def _start_fallback(
        self,
        guild: discord.Guild,
        reason: str,
        announce: bool,
        use_backup: bool,
    ) -> None:
        await self._set_wanted(guild.id, True)
        settings = await self.db.set_fallback_state(
            guild.id,
            on_fallback=True,
            reason=reason,
        )
        stream_url = await self._resolve_fallback_stream_url(
            settings.fallback_url,
            use_backup=use_backup,
        )
        logger.info("Starting fallback stream in guild %s: %s", guild.id, stream_url)

        async with self._get_lock(guild.id):
            if guild.id not in self._wanted:
                return
            self._active_tracks[guild.id] = None
            self._fallback_started_at[guild.id] = time.monotonic()
            self._fallback_playing[guild.id] = stream_url
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
        """Queue the next fallback track, or a backup station if the stream died early."""
        generation = self._generation.get(guild.id)
        settings = await self.db.get_guild_settings(guild.id)
        if self._generation.get(guild.id) != generation:
            return
        if not settings.on_fallback or guild.id not in self._wanted:
            return
        if not self._voice_connected(guild):
            self.schedule_recovery(guild, "voice connection lost during fallback stream")
            return

        elapsed = time.monotonic() - self._fallback_started_at.get(guild.id, 0.0)
        # A stream that dies within seconds is broken; mark it, back off, and use a backup
        use_backup = error is not None or elapsed < 5.0
        if use_backup:
            dead_url = self._fallback_playing.get(guild.id)
            if dead_url:
                self.mark_stream_dead(dead_url, f"stopped after {elapsed:.0f}s: {error}")
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
            use_backup=use_backup,
        )

    def _known_failure(self, guild_id: int, track: ActiveTrack) -> str | None:
        entry = self._failed_tracks.get(guild_id, {}).get(track_key(track))
        if entry is not None and entry[1] > time.monotonic():
            return entry[0]
        return None

    def _record_skip(
        self, guild: discord.Guild, track: ActiveTrack, reason: str, upcoming: bool = False
    ) -> None:
        """Remember a track that can't play for a while and log the one line saying why."""
        now = time.monotonic()
        self._failed_tracks.setdefault(guild.id, {})[track_key(track)] = (
            reason,
            now + FAILED_TRACK_TTL_SECONDS,
        )
        self._skips.setdefault(guild.id, deque()).append(now)
        self.skips_last_hour(guild.id)
        logger.warning(
            "Skipping '%s - %s' in guild %s%s: %s",
            track.artist,
            track.title,
            guild.id,
            " (found while prefetching)" if upcoming else "",
            reason,
        )

    def forget_failures(self, guild_id: int) -> None:
        """Give every skipped track another chance, e.g. when someone restarts the playlist."""
        self._failed_tracks.pop(guild_id, None)
        self._consecutive_failures.pop(guild_id, None)

    def skips_last_hour(self, guild_id: int) -> int:
        """Return how many tracks were skipped as unplayable in the last hour."""
        skips = self._skips.get(guild_id)
        if not skips:
            return 0
        cutoff = time.monotonic() - 3600.0
        while skips and skips[0] < cutoff:
            skips.popleft()
        return len(skips)

    def next_up(self, guild_id: int, playlist: Sequence[PlaylistTrack]) -> ActiveTrack | None:
        """Return the track expected to play next: the queue first, then the planned index."""
        queue = self._queues.get(guild_id)
        if queue:
            return queue[0]
        for index in self._plan.get(guild_id, []):
            if index < len(playlist) and self._known_failure(guild_id, playlist[index]) is None:
                return playlist[index]
        return None

    def _next_after(
        self, guild_id: int, index: int, settings: GuildSettings, total: int
    ) -> int | None:
        """Return the index that follows `index`, using the prefetch plan when there is one."""
        plan = [i for i in self._plan.get(guild_id, []) if i < total]
        if plan and plan[0] == index:
            plan.pop(0)
        self._plan[guild_id] = plan
        if plan:
            return plan[0]
        return compute_next_index(index, total, settings.loop_mode, settings.shuffle)

    def _replan(self, guild_id: int, started: int, settings: GuildSettings, total: int) -> None:
        """Plan the next PREFETCH_COUNT indices, keeping choices already made (e.g. shuffle)."""
        old = [i for i in self._plan.get(guild_id, []) if i < total]
        plan = old[1:] if old and old[0] == started else []
        last = plan[-1] if plan else started
        # Never plan as far as the track that's playing now; small playlists wrap quickly
        while len(plan) < min(PREFETCH_COUNT, total - 1):
            following = compute_next_index(last, total, settings.loop_mode, settings.shuffle)
            if following is None:
                break
            plan.append(following)
            last = following
        self._plan[guild_id] = plan

    async def _resolve_track(self, guild_id: int, track: ActiveTrack) -> Resolved:
        """Use a prefetched stream if it isn't about to expire, else resolve now."""
        ready = self._prefetched.get(guild_id, {}).pop(track_key(track), None)
        if ready is not None:
            if not ready.expiring(time.time(), EXPIRY_MARGIN_SECONDS):
                return ready
            try:
                return await self.resolver.refresh(ready)
            except Exception as exc:
                logger.info("Refreshing %s failed (%s); resolving again.", ready.source_id, exc)
        return await self.resolver.resolve(track)

    def _start_prefetch(self, guild: discord.Guild) -> None:
        # Superseded runs stop themselves; cancelling one inside an aiosqlite call leaks its thread
        generation = self._prefetch_generation.get(guild.id, 0) + 1
        self._prefetch_generation[guild.id] = generation
        self._prefetch_tasks[guild.id] = self.bot.loop.create_task(
            self._prefetch(guild, generation)
        )

    async def _prefetch(self, guild: discord.Guild, generation: int) -> None:
        """Resolve the planned tracks ahead of time so a bad one is skipped before it's due."""
        guild_id = guild.id
        try:
            playlist = await self.db.get_playlist(guild_id)
            planned = [i for i in self._plan.get(guild_id, []) if i < len(playlist)]
            keep = {track_key(playlist[i]) for i in planned}
            ready = self._prefetched.setdefault(guild_id, {})
            for key in [k for k in ready if k not in keep]:
                del ready[key]
            for index in planned:
                if self._prefetch_generation.get(guild_id) != generation:
                    return
                track = playlist[index]
                key = track_key(track)
                if self._known_failure(guild_id, track) is not None:
                    continue
                have = ready.get(key)
                if have is not None and not have.expiring(time.time(), EXPIRY_MARGIN_SECONDS):
                    continue
                try:
                    ready[key] = await self.resolver.resolve(track)
                except SourcesUnreachable as exc:
                    logger.warning("Prefetch paused in guild %s: %s", guild_id, exc)
                    return
                except Exception as exc:
                    self._record_skip(guild, track, str(exc) or type(exc).__name__, upcoming=True)
        except Exception:
            logger.exception("Prefetch failed in guild %s.", guild_id)

    async def _start_track(
        self,
        guild: discord.Guild,
        track: ActiveTrack,
        resolved: Resolved,
        settings: GuildSettings,
        on_end: EndHandler,
        index: int | None,
        total: int,
    ) -> bool:
        """Play a resolved track; return False if playback was stopped meanwhile."""
        async with self._get_lock(guild.id):
            if guild.id not in self._wanted:
                return False
            if index is not None:
                await self.db.set_current_index(guild.id, index)
            await self.db.set_fallback_state(guild.id, on_fallback=False, reason="")
            self._active_tracks[guild.id] = track
            self._playing_source[guild.id] = resolved.source_id
            self._track_started_at[guild.id] = time.monotonic()
            self._consecutive_failures[guild.id] = 0
            await self._play_stream(guild, resolved.stream_url, settings.volume, on_end)
        logger.info(
            "Now playing '%s - %s' in guild %s from %s.",
            track.artist,
            track.title,
            guild.id,
            resolved.source_id,
        )
        if index is not None:
            self._replan(guild.id, index, settings, total)
        self._start_prefetch(guild)
        return True

    async def _go_to_radio(self, guild: discord.Guild, why: str) -> None:
        """Switch to radio on the bot's own call, and keep trying to get back to the playlist."""
        await self.start_fallback(guild, reason=f"{AUTO_RADIO_PREFIX} {why}")
        existing = self._radio_return_tasks.get(guild.id)
        if existing is None or existing.done():
            self._radio_return_tasks[guild.id] = self.bot.loop.create_task(
                self._radio_return(guild)
            )

    async def _radio_return(self, guild: discord.Guild) -> None:
        """While on automatic radio, periodically try the playlist and leave once a track works."""
        guild_id = guild.id
        try:
            while guild_id in self._wanted:
                await asyncio.sleep(RADIO_RETRY_SECONDS)
                settings = await self.db.get_guild_settings(guild_id)
                if (
                    guild_id not in self._wanted
                    or not settings.on_fallback
                    or not settings.fallback_reason.startswith(AUTO_RADIO_PREFIX)
                ):
                    return
                playlist = await self.db.get_playlist(guild_id)
                index: int | None = settings.current_index
                attempts = 0
                for _ in range(len(playlist)):
                    if index is None or attempts >= PREFETCH_COUNT:
                        break
                    index = compute_next_index(index, len(playlist), "playlist", settings.shuffle)
                    if index is None:
                        break
                    track = playlist[index]
                    if self._known_failure(guild_id, track) is not None:
                        continue
                    attempts += 1
                    try:
                        ready = await self.resolver.resolve(track)
                    except SourcesUnreachable as exc:
                        logger.info("Still on radio in guild %s: %s", guild_id, exc)
                        break
                    except Exception as exc:
                        self._record_skip(guild, track, str(exc) or type(exc).__name__)
                        continue
                    self._prefetched.setdefault(guild_id, {})[track_key(track)] = ready
                    logger.info("Leaving radio in guild %s: '%s' resolved.", guild_id, track.title)
                    await self.play_playlist_index(guild, index=index)
                    await self.notify_control_channel(
                        guild,
                        title="🎶 Back to the Playlist",
                        description=f"Tracks are streaming again, starting with **{track.title}**.",
                    )
                    return
        except Exception:
            logger.exception("Radio return check failed in guild %s.", guild_id)

    async def play_playlist_index(
        self,
        guild: discord.Guild,
        index: int | None = None,
    ) -> None:
        """Play the track at `index` (or the saved cursor), skipping tracks that can't stream."""
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

        target: int | None = settings.current_index if index is None else index
        if target is None or target < 0 or target >= len(playlist):
            target = 0
        total = len(playlist)
        radio_allowed = should_fallback_on_error(settings.fallback_triggers)

        with self._starting(guild.id):
            for _ in range(total):
                if guild.id not in self._wanted:
                    return
                track = playlist[target]
                if self._known_failure(guild.id, track) is None:
                    try:
                        resolved = await self._resolve_track(guild.id, track)
                    except SourcesUnreachable as exc:
                        if radio_allowed:
                            await self._go_to_radio(guild, f"search sites unreachable ({exc})")
                            return
                        raise
                    except Exception as exc:
                        self._record_skip(guild, track, str(exc) or type(exc).__name__)
                    else:
                        await self._start_track(
                            guild,
                            track,
                            resolved,
                            settings,
                            functools.partial(self._handle_track_finished, guild, target),
                            target,
                            total,
                        )
                        return

                failures = self._consecutive_failures.get(guild.id, 0) + 1
                self._consecutive_failures[guild.id] = failures
                if failures >= MAX_CONSECUTIVE_FAILURES and radio_allowed:
                    await self._go_to_radio(guild, f"{failures} tracks in a row failed to stream")
                    return
                following = self._next_after(guild.id, target, settings, total)
                if following is None:
                    break
                target = following

        if radio_allowed:
            await self._go_to_radio(guild, "no track in the playlist could be streamed")
            return
        await self.notify_control_channel(
            guild,
            title="⚠️ Nothing Playable",
            description="No track in the playlist could be streamed. Retrying shortly.",
            color=discord.Color.red(),
        )

    async def _play_next_queued(self, guild: discord.Guild) -> bool:
        """Play the next playable searched track; return whether audio started."""
        queue = self._queues.get(guild.id)
        previous = self._active_tracks.get(guild.id)
        if isinstance(previous, PlaylistTrack):
            self._queue_interrupted[guild.id] = True
        elif previous is None:
            self._queue_interrupted[guild.id] = False
        with self._starting(guild.id):
            while queue:
                track = queue.popleft()
                await self._set_wanted(guild.id, True)
                settings = await self.db.get_guild_settings(guild.id)
                try:
                    resolved = await self._resolve_track(guild.id, track)
                except Exception as exc:
                    self._record_skip(guild, track, str(exc) or type(exc).__name__)
                    continue
                # Advance past the cursor only if the queue cut in after a playlist track
                finished: int | None = (
                    settings.current_index if self._queue_interrupted.get(guild.id) else None
                )
                return await self._start_track(
                    guild,
                    track,
                    resolved,
                    settings,
                    functools.partial(self._handle_track_finished, guild, finished),
                    None,
                    0,
                )
        return False

    async def _handle_track_finished(
        self,
        guild: discord.Guild,
        finished_index: int | None,
        error: Exception | None,
    ) -> None:
        """Move on to the next track, or resume the saved cursor if `finished_index` is None."""
        settings = await self.db.get_guild_settings(guild.id)
        if settings.on_fallback or guild.id not in self._wanted:
            return

        if not self._voice_connected(guild):
            self.schedule_recovery(guild, "voice connection lost during playback")
            return

        if error is not None:
            track = self._active_tracks.get(guild.id)
            played = time.monotonic() - self._track_started_at.get(guild.id, 0.0)
            if track is not None and played < QUICK_FAILURE_SECONDS:
                reason = f"stream died after {played:.0f}s: {error}"
                self._record_skip(guild, track, reason)
                source_id = self._playing_source.get(guild.id)
                if source_id:
                    await self.resolver.forget(track, source_id, reason)

        if await self._play_next_queued(guild) or guild.id not in self._wanted:
            return

        if finished_index is None:
            await self.play_playlist_index(guild)
            return

        playlist = await self.db.get_playlist(guild.id)
        next_idx = self._next_after(guild.id, finished_index, settings, len(playlist))

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
        self.forget_failures(guild.id)
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
        next_idx = self._next_after(guild.id, settings.current_index, settings, len(playlist))
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
        # Background tasks see the guild is no longer wanted and stop on their own
        self._prefetch_generation[guild.id] = self._prefetch_generation.get(guild.id, 0) + 1
        self._plan.pop(guild.id, None)
        self._prefetched.pop(guild.id, None)
        async with self._get_lock(guild.id):
            self._generation[guild.id] = self._generation.get(guild.id, 0) + 1
            self._active_tracks[guild.id] = None
            await self.db.set_fallback_state(guild.id, on_fallback=False, reason="")
            voice_client = guild.voice_client
            if isinstance(voice_client, discord.VoiceClient):
                if voice_client.is_playing() or voice_client.is_paused():
                    voice_client.stop()
                await voice_client.disconnect(force=True)
