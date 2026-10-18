"""Pre-commit and pre-push secret scanner for LoFi Discord Music Bot."""

from __future__ import annotations

import re
import sys
from pathlib import Path

BLOCKED_FILENAMES = {".env", ".env.local", ".env.production", "lofi_bot.db"}

SAFE_PLACEHOLDERS = {
    "your_discord_bot_token_here",
    "your_spotify_client_id_here",
    "your_spotify_client_secret_here",
    "dummy_token_for_testing",
    "dummy_client_id",
    "dummy_client_secret",
}

# Discord bot token pattern (base64 user ID . timestamp . HMAC)
DISCORD_TOKEN_RE = re.compile(r"[MN][A-Za-z\d]{23,28}\.[\w-]{6}\.[\w-]{27,38}")

# Direct assignment of real credentials to sensitive config keys
SENSITIVE_ASSIGN_RE = re.compile(
    r"(?i)\b(discord_bot_token|spotify_client_secret|spotify_client_id)\b"
    r"\s*[:=]\s*[\"'\`]?([A-Za-z0-9_\-\.]{20,})[\"'\`]?"
)

# Generic private keys or GitHub personal access tokens
GENERIC_SECRET_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})"
)


def is_blocked_file(filepath: Path) -> bool:
    """Return True if the filename itself must never be committed."""
    if filepath.name in BLOCKED_FILENAMES:
        return True
    if filepath.name.startswith(".env") and filepath.name != ".env.example":
        return True
    if filepath.suffix in {".db", ".sqlite", ".sqlite3", ".pem", ".key"}:
        return True
    return False


def scan_text(content: str, filepath: str = "<memory>") -> list[str]:
    """Scan file text for leaked secrets and return a list of violation messages."""
    violations: list[str] = []

    for lineno, line in enumerate(content.splitlines(), start=1):
        # Allow explicit test fixtures marked with # nosecret
        if "# nosecret" in line:
            continue

        if DISCORD_TOKEN_RE.search(line):
            violations.append(f"{filepath}:{lineno}: Detected Discord Bot Token pattern.")

        if GENERIC_SECRET_RE.search(line):
            violations.append(f"{filepath}:{lineno}: Detected private key or GitHub token.")

        match = SENSITIVE_ASSIGN_RE.search(line)
        if match:
            key_name, value = match.group(1), match.group(2)
            if value.lower() not in SAFE_PLACEHOLDERS:
                violations.append(
                    f"{filepath}:{lineno}: Detected non-placeholder secret assigned to {key_name}."
                )

    return violations


def main(argv: list[str] | None = None) -> int:
    """Scan all file paths passed by pre-commit."""
    args = argv if argv is not None else sys.argv[1:]
    all_violations: list[str] = []

    for raw_path in args:
        path = Path(raw_path)
        if not path.exists() or path.is_dir():
            continue

        if is_blocked_file(path):
            all_violations.append(
                f"{raw_path}: Blocked sensitive file type ({path.name}) must not be committed."
            )
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Skip binary files such as .png assets
            continue

        all_violations.extend(scan_text(text, filepath=raw_path))

    if all_violations:
        print("\n[SECRET GATE BLOCKED] Potential secret(s) detected:", file=sys.stderr)
        for violation in all_violations:
            print(f"  - {violation}", file=sys.stderr)
        print(
            "\nRemove the secret and store it in your .env file or Portainer stack.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
