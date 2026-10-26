"""Slash command suite with per-server mod-control channel enforcement."""

from __future__ import annotations

import math
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from src.audio_manager import AudioManager
from src.database import DatabaseManager, PlaylistTrack
from src.spotify_client import SpotifyResolver

TRACKS_PER_PAGE = 10
# Commands that pick what plays or where; radio mode only ever plays its one stream in its one channel
RADIO_BLOCKED_COMMANDS = frozenset({"playlist", "fallback", "search", "skip", "mode", "setup"})


def blocked_in_radio_mode(command_name: str) -> bool:
    """Return True if a command (by qualified name) is turned off in radio mode."""
    return command_name.split(" ", 1)[0] in RADIO_BLOCKED_COMMANDS


def is_allowed_in_channel(
    interaction_channel_id: int,
    configured_control_channel_id: int | None,
) -> bool:
    """Return True if a command is executed inside the server's designated mod channel."""
    if configured_control_channel_id is None:
        return False
    return interaction_channel_id == configured_control_channel_id


class MusicCommandsCog(commands.Cog):
    """Per-server slash commands for LoFi playback, playlist management, and fallback control."""

    playlist_group = app_commands.Group(
        name="playlist",
        description="Manage this server's custom Spotify-backed LoFi playlist.",
    )
    fallback_group = app_commands.Group(
        name="fallback",
        description="Configure and control this server's 24/7 fallback LoFi radio stream.",
    )

    def __init__(
        self,
        bot: commands.Bot,
        db: DatabaseManager,
        spotify: SpotifyResolver,
        audio: AudioManager,
        radio_mode: bool = False,
        radio_channel_id: int | None = None,
        radio_stream_url: str = "",
    ) -> None:
        self.bot = bot
        self.db = db
        self.spotify = spotify
        self.audio = audio
        self.radio_mode = radio_mode
        self.radio_channel_id = radio_channel_id
        self.radio_stream_url = radio_stream_url

    async def _play_radio(self, guild: discord.Guild) -> str:
        """Restart radio mode's configured stream in its configured channel, never a server's own settings."""
        channel = self.bot.get_channel(self.radio_channel_id or 0)
        if not isinstance(channel, discord.VoiceChannel) or channel.guild.id != guild.id:
            return "📻 This bot's radio only plays in its own channel."
        await self.audio.start_radio(channel, self.radio_stream_url)
        return "📻 Playing the radio stream."

    async def _require_mod_channel(self, interaction: discord.Interaction) -> bool:
        """Verify the interaction occurred inside the guild's configured mod control channel."""
        if interaction.guild is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "Commands can only be used inside a Discord server.",
                ephemeral=True,
            )
            return False

        command = interaction.command
        if (
            self.radio_mode
            and command is not None
            and blocked_in_radio_mode(command.qualified_name)
        ):
            await interaction.response.send_message(
                "📻 This bot only plays its radio stream, so playlist commands are off.",
                ephemeral=True,
            )
            return False

        settings = await self.db.get_guild_settings(interaction.guild.id)
        if settings.control_channel_id is None:
            await interaction.response.send_message(
                "This server has not configured its channels yet. "
                "Ask a server administrator to run `/setup` first.",
                ephemeral=True,
            )
            return False

        if not is_allowed_in_channel(interaction.channel_id, settings.control_channel_id):
            await interaction.response.send_message(
                f"Bot controls for this server are restricted to <#{settings.control_channel_id}>.",
                ephemeral=True,
            )
            return False

        return True

    @app_commands.command(
        name="setup",
        description="Bind the LoFi voice channel and mod control text channel for this server.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_channels(
        self,
        interaction: discord.Interaction,
        voice_channel: discord.VoiceChannel,
        control_channel: discord.TextChannel,
    ) -> None:
        """Configure the voice output channel and mod control channel for the current guild."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command must be run inside a server.",
                ephemeral=True,
            )
            return

        settings = await self.db.set_channels(
            guild_id=interaction.guild.id,
            voice_channel_id=voice_channel.id,
            control_channel_id=control_channel.id,
        )
        embed = discord.Embed(
            title="✨ Server LoFi Channels Configured",
            description=(
                f"**Audio Voice Channel:** {voice_channel.mention}\n"
                f"**Mod Control Channel:** {control_channel.mention}\n"
                f"**Fallback Rules:** `{settings.fallback_triggers}`\n\n"
                f"All future bot commands must be sent inside {control_channel.mention}."
            ),
            color=discord.Color.purple(),
        )
        await interaction.response.send_message(embed=embed)

    @playlist_group.command(
        name="add",
        description="Add a Spotify track, album, or playlist URL to this server's LoFi playlist.",
    )
    async def playlist_add(
        self,
        interaction: discord.Interaction,
        spotify_url: str,
    ) -> None:
        """Resolve Spotify metadata and append tracks to the server's playlist."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        await interaction.response.defer()
        try:
            tracks = await self.spotify.resolve_url(spotify_url)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Could not resolve Spotify link: {exc}")
            return

        if not tracks:
            await interaction.followup.send("⚠️ No playable tracks were found at that Spotify link.")
            return

        total = await self.db.add_tracks(
            guild_id=interaction.guild.id,
            tracks=tracks,
            added_by=str(interaction.user),
        )
        settings = await self.db.get_guild_settings(interaction.guild.id)
        resume_hint = (
            "\n💡 Currently on the 24/7 fallback stream—run `/playlist resume` to switch back!"
            if settings.on_fallback
            else ""
        )
        await interaction.followup.send(
            f"✅ Added **{len(tracks)}** track(s) to this server's playlist "
            f"(Total saved: **{total}** tracks).{resume_hint}"
        )

    @playlist_group.command(
        name="list",
        description="View the tracks saved in this server's playlist.",
    )
    async def playlist_list(
        self,
        interaction: discord.Interaction,
        page: int = 1,
    ) -> None:
        """List saved playlist tracks for the current server with pagination."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        playlist = await self.db.get_playlist(interaction.guild.id)
        settings = await self.db.get_guild_settings(interaction.guild.id)

        if not playlist:
            await interaction.response.send_message(
                "This server's playlist is empty. Add songs with `/playlist add <spotify_url>`."
            )
            return

        total_pages = max(1, math.ceil(len(playlist) / TRACKS_PER_PAGE))
        safe_page = max(1, min(page, total_pages))
        start = (safe_page - 1) * TRACKS_PER_PAGE
        end = start + TRACKS_PER_PAGE

        lines: list[str] = []
        for track in playlist[start:end]:
            is_current = not settings.on_fallback and (track.position - 1) == settings.current_index
            marker = "▶️ " if is_current else ""
            lines.append(f"`{track.position}.` {marker}**{track.title}** — {track.artist}")

        embed = discord.Embed(
            title=f"💜 {interaction.guild.name} — LoFi Playlist",
            description="\n".join(lines),
            color=discord.Color.purple(),
        )
        embed.set_footer(text=f"Page {safe_page}/{total_pages} • {len(playlist)} total track(s)")
        await interaction.response.send_message(embed=embed)

    @playlist_group.command(
        name="remove",
        description="Remove a track at a specific position from this server's playlist.",
    )
    async def playlist_remove(
        self,
        interaction: discord.Interaction,
        position: int,
    ) -> None:
        """Remove a single track by its 1-based position in the server playlist."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        removed = await self.db.remove_track_at_position(interaction.guild.id, position)
        if removed is None:
            await interaction.response.send_message(f"⚠️ No track found at position `{position}`.")
            return

        await interaction.response.send_message(
            f"🗑️ Removed **`#{position}` {removed.title}** — {removed.artist}."
        )

    @playlist_group.command(
        name="clear",
        description="Clear all tracks from this server's saved playlist.",
    )
    async def playlist_clear(self, interaction: discord.Interaction) -> None:
        """Wipe all tracks from the current server's playlist."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        count = await self.db.clear_playlist(interaction.guild.id)
        await interaction.response.send_message(
            f"🧹 Cleared **{count}** track(s) from this server's playlist."
        )

    @playlist_group.command(
        name="resume",
        description="Exit the 24/7 fallback stream and kick off this server's custom playlist.",
    )
    async def playlist_resume(
        self,
        interaction: discord.Interaction,
        start_from: Literal["saved_position", "beginning"] = "saved_position",
    ) -> None:
        """Stop fallback radio playback and resume the server's saved Spotify playlist."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        await interaction.response.defer()
        try:
            await self.audio.resume_playlist(interaction.guild, start_from=start_from)
            active = self.audio.get_active_track(interaction.guild.id)
            track_desc = f"**{active.title}** — {active.artist}" if active else "your playlist"
            await interaction.followup.send(f"🎶 Exited fallback stream and resumed {track_desc}!")
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Could not resume playlist: {exc}")

    @fallback_group.command(
        name="config",
        description="Set when the 24/7 fallback stream activates and optionally change its URL.",
    )
    async def fallback_config(
        self,
        interaction: discord.Interaction,
        triggers: Literal[
            "on_empty_and_error",
            "on_error_only",
            "on_empty_only",
            "manual_only",
        ],
        stream_url: str | None = None,
    ) -> None:
        """Configure fallback trigger rules and custom 24/7 stream URL for this server."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        updated = await self.db.update_fallback_config(
            guild_id=interaction.guild.id,
            triggers=triggers,
            fallback_url=stream_url,
        )
        await interaction.response.send_message(
            f"⚙️ Updated fallback settings for **{interaction.guild.name}**:\n"
            f"• **Triggers:** `{updated.fallback_triggers}`\n"
            f"• **24/7 Stream URL:** `{updated.fallback_url}`"
        )

    @fallback_group.command(
        name="start",
        description="Manually switch this server to the 24/7 fallback LoFi radio stream.",
    )
    async def fallback_start(self, interaction: discord.Interaction) -> None:
        """Immediately switch the current server to its fallback stream."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        await interaction.response.defer()
        try:
            await self.audio.start_fallback(
                interaction.guild,
                reason=f"Manually started by {interaction.user}",
            )
            await interaction.followup.send("🌙 Switched to the 24/7 fallback LoFi stream.")
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Could not start fallback stream: {exc}")

    @app_commands.command(
        name="play",
        description="Start or resume playback in this server's configured LoFi voice channel.",
    )
    async def play_cmd(self, interaction: discord.Interaction) -> None:
        """Start or unpause playback in the server's configured voice channel."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        voice_client = interaction.guild.voice_client
        if isinstance(voice_client, discord.VoiceClient) and voice_client.is_paused():
            voice_client.resume()
            await interaction.response.send_message("▶️ Resumed playback.")
            return

        await interaction.response.defer()
        if self.radio_mode:
            try:
                await interaction.followup.send(await self._play_radio(interaction.guild))
            except Exception as exc:
                await interaction.followup.send(f"⚠️ Playback failed: {exc}")
            return
        self.audio.forget_failures(interaction.guild.id)
        try:
            await self.audio.play_playlist_index(interaction.guild)
            settings = await self.db.get_guild_settings(interaction.guild.id)
            if settings.on_fallback:
                await interaction.followup.send(
                    "🌙 Started the 24/7 fallback LoFi stream (playlist was empty)."
                )
            else:
                active = self.audio.get_active_track(interaction.guild.id)
                label = f"**{active.title}** — {active.artist}" if active else "server playlist"
                await interaction.followup.send(f"▶️ Now playing {label}.")
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Playback failed: {exc}")

    @app_commands.command(name="pause", description="Pause current LoFi playback.")
    async def pause_cmd(self, interaction: discord.Interaction) -> None:
        """Pause active voice playback."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        voice_client = interaction.guild.voice_client
        if isinstance(voice_client, discord.VoiceClient) and voice_client.is_playing():
            voice_client.pause()
            await interaction.response.send_message("⏸️ Paused playback.")
        else:
            await interaction.response.send_message("⚠️ Nothing is currently playing.")

    @app_commands.command(
        name="skip",
        description="Skip to the next track in this server's LoFi playlist.",
    )
    async def skip_cmd(self, interaction: discord.Interaction) -> None:
        """Skip to the next track in the server's playlist."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        await interaction.response.defer()
        try:
            await self.audio.skip(interaction.guild)
            active = self.audio.get_active_track(interaction.guild.id)
            label = f"**{active.title}** — {active.artist}" if active else "next track"
            await interaction.followup.send(f"⏭️ Skipped! Now playing {label}.")
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Could not skip: {exc}")

    @app_commands.command(
        name="stop",
        description="Stop playback and disconnect the bot from this server's voice channel.",
    )
    async def stop_cmd(self, interaction: discord.Interaction) -> None:
        """Stop audio and disconnect from the server's voice channel."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        await interaction.response.defer()
        await self.audio.stop_and_disconnect(interaction.guild)
        await interaction.followup.send("⏹️ Stopped playback and disconnected.")

    @app_commands.command(
        name="search",
        description="Search for a song and queue it to play next.",
    )
    async def search_cmd(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 200],
    ) -> None:
        """Find a song on Spotify and queue it ahead of the playlist."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        await interaction.response.defer()
        try:
            track = await self.spotify.search_track(query)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Search failed: {exc}")
            return
        if track is None:
            await interaction.followup.send(f"⚠️ No song found for `{query}`.")
            return

        try:
            slot = await self.audio.enqueue(interaction.guild, track)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Could not queue **{track.title}**: {exc}")
            return
        label = f"**{track.title}** — {track.artist}"
        if slot == 0 and self.audio.get_active_track(interaction.guild.id) is track:
            await interaction.followup.send(f"▶️ Now playing {label}.")
        elif slot == 0:
            await interaction.followup.send(f"⚠️ Couldn't stream {label}.")
        else:
            await interaction.followup.send(f"➕ Queued {label} (#{slot} in the queue).")

    @app_commands.command(
        name="mode",
        description="Set loop mode (playlist, track, off) and shuffle for this server.",
    )
    async def mode_cmd(
        self,
        interaction: discord.Interaction,
        loop: Literal["playlist", "track", "off"],
        shuffle: bool,
    ) -> None:
        """Update loop and shuffle settings for the current server."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        updated = await self.db.update_playback_settings(
            guild_id=interaction.guild.id,
            loop_mode=loop,
            shuffle=shuffle,
        )
        await interaction.response.send_message(
            f"🔁 Updated playback mode: **Loop** = `{updated.loop_mode}`, "
            f"**Shuffle** = `{updated.shuffle}`."
        )

    @app_commands.command(
        name="volume",
        description="Adjust playback volume (1-100) for this server.",
    )
    async def volume_cmd(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, 100],
    ) -> None:
        """Set the playback volume percentage for the current server."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        await self.audio.set_volume(interaction.guild, level)
        await interaction.response.send_message(f"🔊 Set server volume to **{level}%**.")

    @app_commands.command(
        name="status",
        description="Show current playback mode, active track, fallback state, and server settings.",
    )
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        """Display a diagnostic embed for the current server."""
        if not await self._require_mod_channel(interaction):
            return
        assert interaction.guild is not None

        settings = await self.db.get_guild_settings(interaction.guild.id)
        playlist = await self.db.get_playlist(interaction.guild.id)
        active = self.audio.get_active_track(interaction.guild.id)

        mode_label = (
            f"🌙 24/7 Fallback Stream ({settings.fallback_reason or 'Active'})"
            if settings.on_fallback
            else "🎶 Custom Server Playlist"
        )
        if settings.on_fallback:
            now_playing = f"`{settings.fallback_url}`"
        elif isinstance(active, PlaylistTrack):
            now_playing = (
                f"**{active.title}** — {active.artist} (`#{active.position}/{len(playlist)}`)"
            )
        elif active:
            now_playing = f"**{active.title}** — {active.artist} (searched)"
        else:
            now_playing = "Stopped / Idle"
        queued = self.audio.queue_length(interaction.guild.id)
        upcoming = self.audio.next_up(interaction.guild.id, playlist)
        up_next = f"**{upcoming.title}** — {upcoming.artist}" if upcoming else "Nothing planned"
        skips = self.audio.skips_last_hour(interaction.guild.id)
        voice_mention = (
            f"<#{settings.voice_channel_id}>" if settings.voice_channel_id else "Not set"
        )
        control_mention = (
            f"<#{settings.control_channel_id}>" if settings.control_channel_id else "Not set"
        )

        embed = discord.Embed(
            title=f"🎧 {interaction.guild.name} — LoFi Bot Status",
            color=discord.Color.purple(),
        )
        embed.add_field(name="Active Mode", value=mode_label, inline=False)
        embed.add_field(name="Now Playing", value=now_playing, inline=False)
        embed.add_field(name="Up Next", value=up_next, inline=False)
        embed.add_field(name="Skipped (last hour)", value=f"{skips} unplayable", inline=True)
        embed.add_field(name="Voice Channel", value=voice_mention, inline=True)
        embed.add_field(name="Mod Channel", value=control_mention, inline=True)
        embed.add_field(
            name="Saved Tracks",
            value=f"{len(playlist)} track(s), {queued} queued",
            inline=True,
        )
        embed.add_field(
            name="Playback Settings",
            value=(
                f"Volume: `{int(settings.volume * 100)}%` | "
                f"Loop: `{settings.loop_mode}` | "
                f"Shuffle: `{settings.shuffle}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Fallback Triggers",
            value=f"`{settings.fallback_triggers}`",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)
