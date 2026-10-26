"""Per-server radio: URL safety checks and the now-playing voice channel status."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import discord
from aiohttp.abc import ResolveResult
from aiohttp.resolver import ThreadedResolver

from src.database import DatabaseManager

logger = logging.getLogger(__name__)

NOWPLAYING_POLL_SECONDS = 30.0
NOWPLAYING_TIMEOUT_SECONDS = 10.0
NOWPLAYING_MAX_BYTES = 64_000
CHANNEL_STATUS_LIMIT = 500
MAX_URL_LENGTH = 2048
PRIVATE_NETWORK_MESSAGE = (
    "That address points to a private network, so the bot won't connect to it. "
    "Use the stream's public address."
)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
# Set from RADIO_TRUSTED_HOSTS at startup: exact hostnames that may resolve to the LAN
trusted_hosts: frozenset[str] = frozenset()
NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


class RadioURLError(ValueError):
    """A radio URL the bot refuses to fetch; the message is safe to show in Discord."""


def is_trusted_host(host: str) -> bool:
    """Return True if `host` is exactly one of RADIO_TRUSTED_HOSTS."""
    return host.lower().rstrip(".") in trusted_hosts


def is_blocked_address(ip: IPAddress, trusted: bool = False) -> bool:
    """Return True for any address that isn't public; a trusted host may still use private ones."""
    if isinstance(ip, ipaddress.IPv6Address):
        teredo = ip.teredo
        nat64 = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) if ip in NAT64_PREFIX else None
        for embedded in (ip.ipv4_mapped, ip.sixtofour, teredo[1] if teredo else None, nat64):
            if embedded is not None and is_blocked_address(embedded, trusted):
                return True
        if ip.is_site_local and not trusted:
            return True
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True
    return not trusted and (not ip.is_global or ip.is_private or ip.is_reserved)


async def check_public_url(url: str) -> None:
    """Raise RadioURLError unless `url` is http(s) and every address its host resolves to is public."""
    # Characters other URL parsers (FFmpeg's among them) may read differently are refused outright
    if len(url) > MAX_URL_LENGTH or any(c.isspace() or c == "\\" for c in url):
        raise RadioURLError("That doesn't look like a valid stream URL.")
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise RadioURLError("That doesn't look like a valid stream URL.") from exc
    if parts.scheme not in ("http", "https"):
        raise RadioURLError("Only http:// and https:// URLs are allowed.")
    host = parts.hostname or ""
    if not host or not host.isascii() or "@" in parts.netloc or "%" in host:
        raise RadioURLError("That URL's host isn't valid; use a plain hostname without a login.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise RadioURLError("I couldn't look up that host.") from exc
    addresses = {ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos}
    trusted = is_trusted_host(host)
    if not addresses or any(is_blocked_address(ip, trusted) for ip in addresses):
        raise RadioURLError(PRIVATE_NETWORK_MESSAGE)


class PublicResolver(ThreadedResolver):
    """Resolver that fails on any non-public address, so a host can't rebind to the LAN mid-fetch."""

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        hosts = await super().resolve(host, port, family)
        trusted = is_trusted_host(host)
        if any(is_blocked_address(ipaddress.ip_address(h["host"]), trusted) for h in hosts):
            raise OSError(PRIVATE_NETWORK_MESSAGE)
        return hosts


def now_playing_label(data: Any) -> str | None:
    """Return "title – artist" from AzuraCast now-playing JSON, or None if it has no title."""
    try:
        song = data["now_playing"]["song"]
        title, artist = song.get("title"), song.get("artist")
    except (KeyError, TypeError, AttributeError):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    title = " ".join(title.split())
    artist = " ".join(artist.split()) if isinstance(artist, str) else ""
    return (f"{title} – {artist}" if artist else title)[:CHANNEL_STATUS_LIMIT]


async def fetch_now_playing(url: str) -> str | None:
    """Fetch the now-playing label from a public `url`, or None if it can't be read."""
    timeout = aiohttp.ClientTimeout(total=NOWPLAYING_TIMEOUT_SECONDS)
    try:
        await check_public_url(url)
        connector = aiohttp.TCPConnector(resolver=PublicResolver())
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status != 200:
                    logger.debug("Now-playing returned HTTP %s.", resp.status)
                    return None
                body = b""
                # read(n) returns what has arrived so far, so keep reading to EOF or past the cap
                while chunk := await resp.content.read(NOWPLAYING_MAX_BYTES + 1 - len(body)):
                    body += chunk
                    if len(body) > NOWPLAYING_MAX_BYTES:
                        break
        if len(body) > NOWPLAYING_MAX_BYTES:
            logger.debug("Now-playing response is over %d bytes; ignored.", NOWPLAYING_MAX_BYTES)
            return None
        data = json.loads(body)
    except (TimeoutError, aiohttp.ClientError, OSError, ValueError, RecursionError) as exc:
        logger.debug("Now-playing fetch failed: %s", exc)
        return None
    return now_playing_label(data)


class RadioStatus:
    """Polls each radio server's now-playing URL and shows the song as its voice channel status."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._labels: dict[int, str] = {}
        self._warned: set[int] = set()

    def current(self, guild_id: int) -> str | None:
        """Return the song last read for a server, if any."""
        return self._labels.get(guild_id)

    def start(self, guild: discord.Guild) -> None:
        """Start (or restart) a server's poller."""
        self.stop(guild.id)
        self._tasks[guild.id] = asyncio.get_running_loop().create_task(self._poll(guild))

    async def start_all(self, guilds: Sequence[discord.Guild]) -> None:
        """Start a poller for every server whose radio has a now-playing URL."""
        for guild in guilds:
            settings = await self.db.get_guild_settings(guild.id)
            if settings.radio_stream_url and settings.radio_nowplaying_url:
                self.start(guild)

    def stop(self, guild_id: int) -> None:
        """Cancel a server's poller and forget its song."""
        task = self._tasks.pop(guild_id, None)
        if task is not None:
            task.cancel()
        self._labels.pop(guild_id, None)
        self._warned.discard(guild_id)

    async def _poll(self, guild: discord.Guild) -> None:
        while True:
            try:
                await self.update(guild)
            except Exception:
                logger.exception("Now-playing update failed in guild %s.", guild.id)
            await asyncio.sleep(NOWPLAYING_POLL_SECONDS)

    async def update(self, guild: discord.Guild) -> None:
        """Read the song once and put it on the channel status if it changed."""
        settings = await self.db.get_guild_settings(guild.id)
        if not settings.radio_nowplaying_url:
            return
        channel = guild.get_channel(settings.radio_channel_id or 0)
        voice = guild.voice_client
        # Discord only lets the bot set a status on the channel it's connected to
        if (
            not isinstance(channel, discord.VoiceChannel)
            or voice is None
            or voice.channel != channel
        ):
            return
        label = await fetch_now_playing(settings.radio_nowplaying_url)
        if label is None or label == self._labels.get(guild.id):
            return
        self._labels[guild.id] = label
        await self.set_status(channel, label)

    async def set_status(self, channel: discord.VoiceChannel, status: str | None) -> None:
        """Set or clear a voice channel status; a missing permission is logged once per server."""
        if status is None:
            self._labels.pop(channel.guild.id, None)
        try:
            await channel.edit(status=status)
        except discord.Forbidden:
            if channel.guild.id not in self._warned:
                self._warned.add(channel.guild.id)
                logger.warning(
                    "Can't set the voice channel status in guild %s: the bot needs the "
                    "Set Voice Channel Status permission there. The radio keeps playing.",
                    channel.guild.id,
                )
        except discord.HTTPException as exc:
            logger.warning(
                "Voice channel status update failed in guild %s: %s", channel.guild.id, exc
            )
