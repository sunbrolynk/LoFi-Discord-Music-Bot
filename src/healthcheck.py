"""Container healthcheck: healthy while the bot keeps its heartbeat file fresh."""

from __future__ import annotations

import sys
import time

from src.config import database_path_from_env, heartbeat_path_for

MAX_HEARTBEAT_AGE_SECONDS = 120.0


def is_healthy(now: float | None = None) -> bool:
    """Return True if the heartbeat file was touched within the allowed age."""
    path = heartbeat_path_for(database_path_from_env())
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age <= MAX_HEARTBEAT_AGE_SECONDS


if __name__ == "__main__":
    sys.exit(0 if is_healthy() else 1)
