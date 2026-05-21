"""
MicroCompact — Layer 1 context compression.
Standalone module: NO circular imports. Safe to import from both
ollama_proxy.py and auto_compact_manager.py.

Logic (mirrors gitee.com/free/claude-code AutoCompactManager):
  - Keep system messages always
  - If session age > 10 min: keep last 2 tool result pairs
  - Otherwise: keep last 6 tool result pairs
  - Always preserve the most recent user message
  - Zero API calls
"""

import logging
import time

logger = logging.getLogger("openclaw.micro_compact")


def micro_compact(messages: list, session_start_time: float) -> list:
    """
    Truncate messages list in-place (returns new list).
    Preserves: all system messages + recent pairs + last user msg.
    """
    if not messages:
        return messages

    session_age_min = (time.time() - session_start_time) / 60.0
    keep_pairs = 2 if session_age_min > 10 else 6

    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system  = [m for m in messages if m.get("role") != "system"]

    if not non_system:
        return messages

    # Find last user message index (always keep it)
    last_user_idx = None
    for i in range(len(non_system) - 1, -1, -1):
        if non_system[i].get("role") == "user":
            last_user_idx = i
            break

    # Keep last N user+assistant pairs (never split a pair)
    kept = []
    pairs_found = 0
    i = len(non_system) - 1
    while i >= 0 and pairs_found < keep_pairs:
        msg = non_system[i]
        role = msg.get("role")
        if role == "assistant" and i > 0 and non_system[i-1].get("role") == "user":
            kept = [non_system[i-1], non_system[i]] + kept
            pairs_found += 1
            i -= 2
        elif role == "user" and i == last_user_idx and pairs_found == 0:
            # Dangling last user msg with no assistant yet
            kept = [msg] + kept
            i -= 1
        else:
            i -= 1

    result = system_msgs + kept
    original_len = len(messages)
    new_len = len(result)
    dropped = original_len - new_len

    if dropped > 0:
        logger.info(
            f"[MicroCompact] age={session_age_min:.1f}min keep_pairs={keep_pairs} "
            f"dropped={dropped} msgs ({original_len}->{new_len})"
        )

    return result