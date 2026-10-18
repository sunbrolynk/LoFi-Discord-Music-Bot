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
* **Configurable 24/7 Fallback Stream**: Automatically switches to a 24/7 LoFi radio stream if a track fails to resolve (`on_error`), when a playlist finishes (`on_empty`), both (`on_empty_and_error`), or only when manually triggered (`manual_only`). Jump right back into your custom playlist anytime with `/playlist resume`.
* **Defense-in-Depth Security & Push Gates**: Every dependency in `requirements.txt` and `requirements-dev.txt` is locked with SHA-256 hashes (`--require-hashes`), and local Git hooks (`pre-commit` and `pre-push`) block leaked secrets, enforce Ruff linting/formatting and Mypy type checks, and run the Pytest unit test suite before any code leaves your machine.

## Slash Command Reference

All commands (except `/setup` when initializing or rebinding channels) must be run inside the server's configured **Mod Control Channel**:

| Command | Parameters | Description |
| :--- | :--- | :--- |
| `/setup` | `voice_channel`, `control_channel` | Binds the LoFi voice output channel and mod control text channel for the current server (requires **Manage Server** permission). |
| `/playlist add` | `spotify_url` | Resolves a Spotify track, album, or playlist URL/URI and appends the tracks to this server's persistent playlist. |
| `/playlist list` | `page` *(optional)* | Displays the tracks saved in this server's playlist and highlights the active track cursor (`▶️`). |
| `/playlist remove` | `position` | Removes the track at the given 1-based position and re-sequences the remaining tracks. |
| `/playlist clear` | None | Clears all saved tracks from this server's playlist and resets the cursor. |
| `/playlist resume` | `start_from` (`saved_position` or `beginning`) | Immediately exits the 24/7 fallback radio stream and restarts this server's custom playlist. |
| `/fallback config` | `triggers`, `stream_url` *(optional)* | Configures when fallback activates (`on_empty_and_error`, `on_error_only`, `on_empty_only`, `manual_only`) and optionally sets a custom 24/7 Icecast/MP3 stream URL for this server. |
| `/fallback start` | None | Manually switches this server to the 24/7 fallback LoFi stream immediately. |
| `/play` | None | Starts or unpauses playback in this server's configured voice channel. |
| `/pause` | None | Pauses active audio playback. |
| `/skip` | None | Skips to the next track in this server's playlist. |
| `/stop` | None | Stops playback and disconnects the bot from this server's voice channel. |
| `/mode` | `loop` (`playlist`, `track`, `off`), `shuffle` (`true`/`false`) | Updates loop and shuffle behavior for this server's playlist. |
| `/volume` | `level` (`1`–`100`) | Sets and saves the playback volume percentage for this server. |
| `/status` | None | Posts a diagnostic embed showing active mode (Playlist vs. Fallback), current track, fallback reason, bound channels, and saved track count. |

## First-Time Server Onboarding

1. Invite the bot to your Discord server.
2. Run `/setup voice_channel:#lofi-radio control_channel:#bot-control` (requires **Manage Server** permission).
3. Go to `#bot-control` and add music:

        /playlist add spotify_url:https://open.spotify.com/playlist/37i9dQZF1DWWQRwui0ExPn

4. Start playback:

        /play

5. Customize fallback behavior if desired:

        /fallback config triggers:on_empty_and_error

6. Whenever the bot is on fallback radio, jump back into your custom playlist with:

        /playlist resume start_from:saved_position

## Deploying with Portainer Stacks

1. Once GitHub Actions finishes building your push to `main`, open your GitHub profile -> **Packages** -> `lofi-discord-music-bot` -> **Package settings**, and set package visibility to **Public** so Portainer can pull `ghcr.io/sunbrolynk/lofi-discord-music-bot:latest` without authentication.
2. In Portainer, go to **Stacks** -> **Add stack**, name it `lofi-discord-music-bot`, and paste the contents of `docker-compose.yml`:

        services:
          lofi-music-bot:
            image: ghcr.io/sunbrolynk/lofi-discord-music-bot:latest
            container_name: lofi-discord-music-bot
            restart: unless-stopped
            environment:
              - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
              - SPOTIFY_CLIENT_ID=${SPOTIFY_CLIENT_ID}
              - SPOTIFY_CLIENT_SECRET=${SPOTIFY_CLIENT_SECRET}
              - DEFAULT_FALLBACK_STREAM_URL=${DEFAULT_FALLBACK_STREAM_URL:-https://streams.ilovemusic.de/iloveradio17.mp3}
              - DATABASE_PATH=/app/data/lofi_bot.db
              - LOG_LEVEL=${LOG_LEVEL:-INFO}
            volumes:
              - lofi_bot_data:/app/data

        volumes:
          lofi_bot_data:

3. Scroll down to **Environment variables** in Portainer and add:
   * `DISCORD_BOT_TOKEN` = your Discord bot token
   * `SPOTIFY_CLIENT_ID` = your Spotify Client ID
   * `SPOTIFY_CLIENT_SECRET` = your Spotify Client Secret
   * `DEFAULT_FALLBACK_STREAM_URL` = `https://streams.ilovemusic.de/iloveradio17.mp3` *(optional override)*
4. Click **Deploy the stack**.

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
    pip install "pip==24.3.1" "click==8.1.8" "pip-tools==7.4.1"
    pip install --require-hashes -r requirements.txt
    pip install --require-hashes -r requirements-dev.txt

    # 4. Activate the pre-commit and pre-push gates
    pre-commit install --hook-type pre-commit --hook-type pre-push

    # 5. (Optional) Run locally in WSL
    cp .env.example .env
    # Edit .env with your real credentials, then start:s
    python -m src.bot

### 4. Updating Dependencies Safely in the Future

Whenever you change `requirements.in` or `requirements-dev.in`, regenerate the SHA-256 hashes before committing:

    source .venv/bin/activate
    pip-compile --generate-hashes --allow-unsafe --output-file=requirements.txt requirements.in
    pip-compile --generate-hashes --allow-unsafe --output-file=requirements-dev.txt requirements-dev.in
    pip install --require-hashes -r requirements.txt
    pip install --require-hashes -r requirements-dev.txt