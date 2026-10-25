# Changelog

All notable changes to the LoFi Discord Music Bot are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Radio-only mode: with `RADIO_STREAM_URL` and `RADIO_CHANNEL_ID` set, the bot joins that voice
  channel on startup and plays only that stream, retrying it with backoff when it or the voice
  connection drops. Playlist, fallback, search, skip and mode commands reply that they're off.
- `RADIO_NOWPLAYING_URL`: polls an AzuraCast now-playing URL every 30 seconds and shows the song
  as the bot's "Listening to" status.

## [1.0.0]

The version in `pyproject.toml` when this file was started. Changes before it
are in the git history.
