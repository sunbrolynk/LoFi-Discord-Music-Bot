# Changelog

All notable changes to the LoFi Discord Music Bot are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Radio-only mode, per server: `/radio on` (Manage Server) saves a stream and a voice channel for
  that server, joins it and plays only that stream, retrying it with backoff when it or the voice
  connection drops, and rejoins it after a restart unless it was stopped with `/stop`. Playlist,
  fallback, search, skip and mode commands reply that they're off in that server only; `/play`
  restarts its stream and `/radio off` turns everything back on.
- An optional AzuraCast now-playing URL on `/radio on`, read every 30 seconds, shows the song as
  that voice channel's status and in `/status`. Without the Set Voice Channel Status permission
  the bot logs it once and keeps playing.
- Radio URLs must be public http(s) addresses: hosts that resolve to loopback, private,
  link-local, CGNAT, multicast or reserved addresses are refused at `/radio on` and on every
  reconnect and read, and the now-playing read follows no redirects and stops at 64 KB. The
  stream player can still follow redirects and playlist links past that check, so a firewall rule
  on the container's outgoing traffic is the real block on a home network.
- `RADIO_TRUSTED_HOSTS`: exact hostnames a radio URL may use even when they resolve to a private
  address, for a stream host behind split DNS. Loopback, link-local, multicast and unspecified
  addresses stay refused, and a bad entry stops the bot at startup.

### Removed

- The `RADIO_STREAM_URL`, `RADIO_CHANNEL_ID` and `RADIO_NOWPLAYING_URL` settings. A server that used
  them keeps that stream as its fallback with the playlist commands back on; run `/radio on` there
  to make it radio-only again.

## [1.0.0]

The version in `pyproject.toml` when this file was started. Changes before it
are in the git history.
