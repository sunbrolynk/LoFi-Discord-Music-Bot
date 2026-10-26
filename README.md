<p align="center">
  <img src="assets/banner.png" alt="LoFi Discord Music Bot Banner" width="100%" />
</p>

<p align="center">
  <img src="assets/icon.png" alt="LoFi Discord Music Bot Icon" width="140" />
</p>

<h1 align="center">LoFi Discord Music Bot</h1>

<p align="center">
  A multi-server Discord LoFi music bot with per-server Spotify playlists, dedicated mod-channel controls, automatic 24/7 radio fallback, and SHA-256 hash-pinned dependencies.
</p>

## Key Features

* **Multi-Server Isolation (`guild_id` SQLite Persistence)**: Every Discord server gets its own designated voice channel, mod control text channel, custom playlist, volume, loop/shuffle settings, and fallback configuration stored in `/app/data/lofi_bot.db`.
* **Mod-Channel Command Lockdown**: Once a server's channels are bound via `/setup`, bot commands are strictly restricted to that server's mod control text channel.
* **Spotify Track, Album & Playlist Resolution**: Add individual songs, full albums, or entire playlists at any time using `/playlist add <spotify_url>`.
* **Skips, Not Radio**: A track that can't be streamed is skipped and the next one plays. The bot resolves the next 2-3 tracks in the background while the current one plays, so a bad track is found and skipped before it's due, with no gap. Each skip is logged with its reason.
* **Configurable 24/7 Fallback Stream**: Switches to a 24/7 LoFi radio stream after 5 tracks in a row fail, or when SoundCloud and YouTube are both unreachable (`on_error`). It also switches when a playlist finishes (`on_empty`), on either (`on_empty_and_error`), or only by hand (`manual_only`). When the bot went to radio by itself, it checks the playlist every 5 minutes and goes back as soon as a track streams. `/playlist resume` and `/play` go back right away and give skipped tracks another chance.
* **Smart Matching and a Resolve Cache**: Searches SoundCloud and YouTube by artist and title. Each result is scored on how close its length is to the Spotify track and how well its title matches. Results that are over 20% off in length, or are a live, reaction, slowed, sped-up, remix or cover version the original isn't, get rejected. The source that worked is saved in the database, so a track resolves once and is never searched again. A source that fails is kept out for 24 hours.
* **Self-Healing 24/7 Playback**: If the voice connection drops, a stream dies, or a track can't be found, the bot reconnects and retries with backoff (5s up to 5 minutes) instead of going quiet. A watchdog restarts any server that should be playing but has been silent for 90 seconds, and after a container restart the bot rejoins and resumes every server that was playing. `/stop` is the only thing that keeps it off.
* **Logs and Healthcheck**: Logs go to stdout (Portainer's log view) and to rotating files in `/app/data/logs/bot.log` (5 MB x 6 files) on the data volume, so they survive restarts. The container reports `unhealthy` when the bot loses Discord or a server has been silent for 10 minutes.
* **Radio-Only Mode, Per Server**: `/radio on` makes the bot play nothing but one stream in one voice channel on that server, rejoining it on startup and reconnecting with backoff when the stream or voice connection drops. With an AzuraCast now-playing URL, the song shows as that voice channel's status. See [Radio-only mode](#radio-only-mode).
* **Defense-in-Depth Security & Push Gates**: Every dependency in `requirements.txt` and `requirements-dev.txt` is locked with SHA-256 hashes (`--require-hashes`), and local Git hooks (`pre-commit` and `pre-push`) block leaked secrets with a pinned gitleaks scan and the bot's own token scanner, refuse a push with uncommitted changes, audit both locks with pip-audit, enforce Ruff linting/formatting and Mypy type checks, and run the Pytest unit test suite before any code leaves your machine.

## Slash Command Reference

All commands (except `/setup` when initializing or rebinding channels) must be run inside the server's configured **Mod Control Channel**:

| Command | Parameters | Description |
| :--- | :--- | :--- |
| `/setup` | `voice_channel`, `control_channel` | Binds the LoFi voice output channel and mod control text channel for the current server (requires **Manage Server** permission). |
| `/radio on` | `stream_url`, `channel`, `nowplaying_url` *(optional)* | Turns on [radio-only mode](#radio-only-mode) for this server: plays only that stream in that voice channel (requires **Manage Server** permission; works outside the mod channel). |
| `/radio off` | None | Stops the radio, clears the channel status and turns the playlist commands back on (requires **Manage Server** permission). |
| `/playlist add` | `spotify_url` | Resolves a Spotify track, album, or playlist URL/URI and appends the tracks to this server's persistent playlist. |
| `/playlist list` | `page` *(optional)* | Displays the tracks saved in this server's playlist and highlights the active track cursor (`▶️`). |
| `/playlist remove` | `position` | Removes the track at the given 1-based position and re-sequences the remaining tracks. |
| `/playlist clear` | None | Clears all saved tracks from this server's playlist and resets the cursor. |
| `/playlist resume` | `start_from` (`saved_position` or `beginning`) | Immediately exits the 24/7 fallback radio stream and restarts this server's custom playlist. |
| `/fallback config` | `triggers`, `stream_url` *(optional)* | Configures when fallback activates (`on_empty_and_error`, `on_error_only`, `on_empty_only`, `manual_only`) and optionally sets a custom 24/7 Icecast/MP3 stream URL for this server. |
| `/fallback start` | None | Manually switches this server to the 24/7 fallback LoFi stream immediately. |
| `/play` | None | Starts or unpauses playback in this server's configured voice channel, or restarts its radio when radio is on. |
| `/pause` | None | Pauses active audio playback. |
| `/search` | `query` | Searches Spotify for a song and queues it to play next, ahead of the playlist. Plays it right away if nothing from the playlist is playing. The queue isn't saved across restarts. |
| `/skip` | None | Skips to the next queued song, or the next track in this server's playlist. |
| `/stop` | None | Stops playback and disconnects the bot from this server's voice channel. |
| `/mode` | `loop` (`playlist`, `track`, `off`), `shuffle` (`true`/`false`) | Updates loop and shuffle behavior for this server's playlist. |
| `/volume` | `level` (`1`–`100`) | Sets and saves the playback volume percentage for this server. |
| `/status` | None | Posts a diagnostic embed: active mode (playlist, fallback radio with the reason, or radio-only), the current track or radio song, the next track up, unplayable tracks skipped in the last hour, bound channels, and saved track count. |

## First-Time Server Onboarding

1. Invite the bot to your Discord server.
2. Run `/setup voice_channel:#lofi-radio control_channel:#bot-control` (requires **Manage Server** permission).
   If `#bot-control` is private, add the bot to it with **View Channel**, **Send Messages** and **Embed Links**. Without them the bot can't post its status notices there and logs one line saying so: `Can't post notices to mod channel ...`.
3. Go to `#bot-control` and add music:

        /playlist add spotify_url:https://open.spotify.com/playlist/37i9dQZF1DWWQRwui0ExPn

4. Start playback:

        /play

5. Customize fallback behavior if desired:

        /fallback config triggers:on_empty_and_error

6. Whenever the bot is on fallback radio, jump back into your custom playlist with:

        /playlist resume start_from:saved_position

## Radio-Only Mode

For a server that only relays one 24/7 station, someone with **Manage Server** runs:

        /radio on stream_url:https://radio.example.com/listen/lofi/radio.mp3 channel:#lofi-radio nowplaying_url:https://radio.example.com/api/nowplaying/lofi

The setting is saved for that server only. The bot joins the channel and plays the stream, and rejoins it after a restart unless it was stopped with `/stop`. If the stream ends or fails it tries the same stream again with backoff (it never switches to the backup stations), and the usual voice reconnect and watchdog apply. In that server `/playlist`, `/fallback`, `/search`, `/skip` and `/mode` reply that they're off; `/play` restarts the stream after a `/stop`, and `/pause`, `/volume` and `/status` work as usual. Run `/setup` once if you want a mod control channel for those. Other servers are unaffected. `/radio off` stops the radio and turns everything back on.

`nowplaying_url` is optional: an AzuraCast now-playing JSON URL, read every 30 seconds. The song shows as the voice channel's status, which needs the bot to have **Set Voice Channel Status** in that channel. Without it the bot logs one line and keeps playing; `/status` shows the song either way.

Both URLs must be public `http://` or `https://` addresses. The bot looks the host up when `/radio on` runs and again on every reconnect and every now-playing read, and refuses any host that resolves to a loopback, private, link-local, CGNAT, multicast or reserved address. The now-playing read doesn't follow redirects and stops at 64 KB. That check covers the address the bot itself connects to, but the stream player (FFmpeg) can still follow redirects and playlist links to other addresses after it, so on a home network the real block is a firewall rule on the container's outgoing traffic that refuses your private ranges.

One security setting belongs to whoever runs the bot, not to the servers: `RADIO_TRUSTED_HOSTS`, a comma-separated list of exact hostnames (for example `RADIO_TRUSTED_HOSTS=radio.example.com`). A radio URL whose host is exactly one of them may resolve to a private address, for a stream host that split DNS points at your own network. Loopback, link-local, multicast and unspecified addresses stay refused even then, and a subdomain or longer name (`evil.radio.example.com`) isn't trusted. Entries are matched case-insensitively with a trailing dot ignored; a wildcard, IP address, port or scheme stops the bot at startup with a message naming the entry. It's empty by default, and radio itself is still turned on and off from Discord. Anyone with **Manage Server** in any server the bot is in can point their radio at a trusted host, so list only hosts you'd let every such server reach.

### Running several copies

Each copy needs its own `DISCORD_BOT_TOKEN` and its own `DATABASE_PATH` folder: the settings database, logs and healthcheck heartbeat all live there. In Docker, give each copy its own stack (or service) with its own data volume; locally, point each one at a different folder, e.g. `DATABASE_PATH=data/radio-lofi/lofi_bot.db`.

## Deploying with Portainer Stacks

1. Once GitHub Actions finishes building your push to `main`, open your GitHub profile -> **Packages** -> `lofi-discord-music-bot` -> **Package settings**, and set package visibility to **Public** so Portainer can pull `ghcr.io/sunbrolynk/lofi-discord-music-bot:latest` without authentication.
2. In Portainer, go to **Stacks** -> **Add stack**, name it `lofi-discord-music-bot`, and paste the contents of `docker-compose.yml`:

        services:
          lofi-music-bot:
            image: ghcr.io/sunbrolynk/lofi-discord-music-bot:latest
            container_name: lofi-discord-music-bot
            restart: unless-stopped
            user: "0:0"
            environment:
              - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
              - SPOTIFY_CLIENT_ID=${SPOTIFY_CLIENT_ID}
              - SPOTIFY_CLIENT_SECRET=${SPOTIFY_CLIENT_SECRET}
              - DEFAULT_FALLBACK_STREAM_URL=${DEFAULT_FALLBACK_STREAM_URL:-https://lofi.radio}
              - BACKUP_FALLBACK_STREAM_URLS=${BACKUP_FALLBACK_STREAM_URLS:-https://stream.laut.fm/lofi}
              - DATABASE_PATH=/app/data/lofi_bot.db
              - LOG_LEVEL=${LOG_LEVEL:-INFO}
              - RADIO_TRUSTED_HOSTS=${RADIO_TRUSTED_HOSTS:-}
            volumes:
              - lofi_bot_data:/app/data

        volumes:
          lofi_bot_data:

3. Scroll down to **Environment variables** in Portainer and add:
   * `DISCORD_BOT_TOKEN` = your Discord bot token
   * `SPOTIFY_CLIENT_ID` = your Spotify Client ID
   * `SPOTIFY_CLIENT_SECRET` = your Spotify Client Secret
   * `DEFAULT_FALLBACK_STREAM_URL` = `https://lofi.radio` *(optional override; new servers start with it, `/fallback config` changes one server)*
   * `BACKUP_FALLBACK_STREAM_URLS` = `https://stream.laut.fm/lofi` *(optional; comma-separated stations tried in order when a server's own fallback is down)*
   * `RADIO_TRUSTED_HOSTS` *(optional; exact hostnames a radio URL may use on your own network, see [Radio-only mode](#radio-only-mode))*

   Before playing a fallback station the bot checks it. A station that answers 404 or doesn't answer is skipped for 30 minutes and logged once. If every backup is down, they all get checked again rather than going silent.
4. Click **Deploy the stack**.

### Updating the bot

1. Merge to `main` and wait for the **CI & Publish Container to GHCR** workflow to go green.
2. In Portainer, open the `lofi-discord-music-bot` stack, click **Editor**, and check it matches `docker-compose.yml` above.
3. Click **Update the stack** with **Re-pull image and redeploy** turned on. A plain container restart keeps the old image.

### Reading the logs

* Live: Portainer -> **Containers** -> `lofi-discord-music-bot` -> **Logs**.
* History: `docker exec lofi-discord-music-bot tail -n 200 /app/data/logs/bot.log`, or the **Console** tab in Portainer. Older files are `bot.log.1` to `bot.log.5`.
* Set `LOG_LEVEL=DEBUG` in the stack's environment variables for voice-connection detail from discord.py.

Lines to look for when playback stops: `Recovering playback`, `Stream resolve attempt`, `Stream ended with error`, and anything from `discord.voice_state`.

## Complete Rebuild-From-Scratch Guide (Disaster Recovery)

If you ever need to rebuild the credentials, local WSL workspace, or deployment from scratch, follow these steps:

### 1. Discord Developer Portal Setup

1. Visit the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Upload `assets/icon.png` as the App Icon and `assets/banner.png` as the Banner.
3. Go to **Bot**:
   * Click **Reset Token** to generate your `DISCORD_BOT_TOKEN`.
   * Under **Privileged Gateway Intents**, no privileged intents are required (the bot uses standard `guilds` and `voice_states` intents).
4. Go to **OAuth2** -> **URL Generator**:
   * Under **Scopes**, check `bot` and `applications.commands`.
   * Under **Bot Permissions**, check:
     * `View Channels`
     * `Send Messages`
     * `Embed Links`
     * `Connect`
     * `Speak`
     * `Use Voice Activity`
   * Copy the generated URL and open it in your browser to invite the bot to any server you run.

### 2. Spotify Developer Portal Setup

1. Visit the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and click **Create app**.
2. Enter an app name and description, set a placeholder Redirect URI (`http://localhost:8888/callback`), and check **Web API**.
3. Open **Settings** to copy your `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`.

### 3. Local WSL + VS Code Setup

    # 1. Install Ubuntu/Debian system packages in WSL
    sudo apt-get update && sudo apt-get install -y \
      git python3 python3-venv python3-pip ffmpeg libffi-dev libnacl-dev

    # 2. Clone the repository
    git clone https://github.com/sunbrolynk/LoFi-Discord-Music-Bot.git
    cd LoFi-Discord-Music-Bot

    # 3. Create the virtual environment and install hash-pinned dependencies
    python3 -m venv .venv
    source .venv/bin/activate
    pip install "pip==26.2.1" "click==8.1.8" "pip-tools==7.6.2"
    pip install --require-hashes -r requirements.txt
    pip install --require-hashes -r requirements-dev.txt

    # 4. Activate the pre-commit and pre-push gates (the gitleaks scan needs Docker)
    pre-commit install --hook-type pre-commit --hook-type pre-push

    # 5. (Optional) Run locally in WSL
    cp .env.example .env
    # Edit .env with your real credentials, then start:
    python -m src.bot

### 4. Updating Dependencies Safely in the Future

Whenever you change `requirements.in` or `requirements-dev.in`, regenerate the SHA-256 hashes before committing:

    source .venv/bin/activate
    pip-compile --generate-hashes --allow-unsafe --output-file=requirements.txt requirements.in
    pip-compile --generate-hashes --allow-unsafe --output-file=requirements-dev.txt requirements-dev.in
    pip install --require-hashes -r requirements.txt
    pip install --require-hashes -r requirements-dev.txt

## License

GNU General Public License v3.0, the same as ABM. See [LICENSE](LICENSE).
