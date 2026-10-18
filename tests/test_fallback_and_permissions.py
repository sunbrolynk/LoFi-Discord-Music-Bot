"""Unit tests for fallback state machine rules and mod-channel access control."""

from __future__ import annotations

from src.audio_manager import (
    compute_next_index,
    should_fallback_on_empty,
    should_fallback_on_error,
)
from src.cogs.music_commands import is_allowed_in_channel


def test_fallback_trigger_matrix() -> None:
    """Verify each per-server fallback trigger mode activates under the right condition."""
    assert should_fallback_on_empty("on_empty_and_error") is True
    assert should_fallback_on_error("on_empty_and_error") is True

    assert should_fallback_on_empty("on_error_only") is False
    assert should_fallback_on_error("on_error_only") is True

    assert should_fallback_on_empty("on_empty_only") is True
    assert should_fallback_on_error("on_empty_only") is False

    assert should_fallback_on_empty("manual_only") is False
    assert should_fallback_on_error("manual_only") is False


def test_compute_next_index_loop_and_shuffle_modes() -> None:
    """Verify playlist cursor advancement across loop modes and shuffle."""
    # Loop playlist wraps around from index 2 -> 0
    assert compute_next_index(2, 3, loop_mode="playlist", shuffle=False) == 0
    # Loop track repeats current index
    assert compute_next_index(1, 3, loop_mode="track", shuffle=False) == 1
    # Loop off returns None at the end of the playlist so empty-fallback can trigger
    assert compute_next_index(2, 3, loop_mode="off", shuffle=False) is None
    # Shuffle picks a different valid track index when >1 track exists
    next_shuffled = compute_next_index(0, 4, loop_mode="playlist", shuffle=True)
    assert next_shuffled in {1, 2, 3}


def test_mod_control_channel_enforcement() -> None:
    """Verify commands are only allowed inside the server's configured mod channel."""
    assert (
        is_allowed_in_channel(interaction_channel_id=555, configured_control_channel_id=None)
        is False
    )
    assert (
        is_allowed_in_channel(interaction_channel_id=555, configured_control_channel_id=999)
        is False
    )
    assert (
        is_allowed_in_channel(interaction_channel_id=999, configured_control_channel_id=999) is True
    )
