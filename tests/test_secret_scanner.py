"""Unit tests for the pre-commit/pre-push secret scanner."""

from __future__ import annotations

from pathlib import Path

from scripts.check_secrets import is_blocked_file, scan_text


def test_blocks_dotenv_and_sqlite_files() -> None:
    """Ensure .env and .db files are blocked while .env.example is allowed."""
    assert is_blocked_file(Path(".env")) is True
    assert is_blocked_file(Path(".env.production")) is True
    assert is_blocked_file(Path("data/lofi_bot.db")) is True
    assert is_blocked_file(Path(".env.example")) is False


def test_detects_discord_bot_token() -> None:
    """Ensure realistic Discord bot tokens are caught."""
    fake_token = "MTIzNDU2Nzg5MDEyMzQ1Njc4OQ" + ".GaBcDe." + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    violations = scan_text(f"TOKEN = '{fake_token}'", filepath="config.py")
    assert len(violations) == 1
    assert "Discord Bot Token" in violations[0]


def test_detects_spotify_secret_assignment() -> None:
    """Ensure hardcoded Spotify secrets are caught while placeholders pass."""
    leaked = "SPOTIFY_CLIENT_SECRET=a1b2c3d4e5f60718293a4b5c6d7e8f90"  # nosecret
    violations = scan_text(leaked, filepath=".env.bad")
    assert len(violations) == 1
    assert "SPOTIFY_CLIENT_SECRET" in violations[0]

    safe = "SPOTIFY_CLIENT_SECRET=your_spotify_client_secret_here"
    assert scan_text(safe, filepath=".env.example") == []
