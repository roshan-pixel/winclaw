"""
context_compressor.py — Layer 2 (SessionMemoryCompact) + Layer 3 (FullCompact)
Calls Qwen via ollama proxy at port 18788.
Ref: gitee.com/free/claude-code AutoCompactManager
"""

import json
import logging
import os
import requests

# Ensure logs dir exists at import time (double-guard per TASK-00 note)
_LOG_DIR = r"C:\Users\sgarm\.openclaw\logs"
os.makedirs(_LOG_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(_LOG_DIR, "compress.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("openclaw.compressor")

QWEN_URL     = "http://localhost:18788/v1/chat/completions"
QWEN_MODEL   = "qwen-godmode"
QWEN_TIMEOUT = 60   # seconds per Qwen call (session compact)
FULL_TIMEOUT = 30   # seconds per Qwen call (full compact rounds)
CIRCUIT_BREAK_LIMIT = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qwen_summarise(chunk: list) -> str:
    """Ask Qwen to summarise a list of messages. Returns summary string."""
    content_parts = []
    for m in chunk:
        role = m.get("role", "?")
        text = m.get("content", "")
        if isinstance(text, list):
            text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
        content_parts.append(f"{role.upper()}: {text}")
    combined = "\n".join(content_parts)

    payload = {
        "model": QWEN_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a compression assistant. "
                    "Summarise the conversation below into 3-5 bullet points. "
                    "Preserve: decisions made, facts stated, user preferences, tool results. "
                    "Output plain text only — no JSON, no markdown headers."
                ),
            },
            {"role": "user", "content": combined},
        ],
        "max_tokens": 400,
        "temperature": 0.0,
    }
    try:
        r = requests.post(QWEN_URL, json=payload, timeout=QWEN_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"[Compressor] Qwen summarise failed: {e}")
        raise


def _never_split_tool_pair(messages: list, split_idx: int) -> int:
    """
    Adjust split_idx so we never split a user+assistant tool pair.
    Returns corrected split_idx.
    """
    if split_idx <= 0 or split_idx >= len(messages):
        return split_idx
    # If we would split between a user msg and its assistant reply, move up one
    if (messages[split_idx - 1].get("role") == "user" and
            messages[split_idx].get("role") == "assistant"):
        return split_idx - 1
    return split_idx


# ---------------------------------------------------------------------------
# Layer 2: SessionMemoryCompact
# ---------------------------------------------------------------------------

def session_memory_compact(messages: list, max_tokens: int) -> list:
    """
    Keep recent 40% of messages; summarise the rest with ONE Qwen call.
    Uses 4/3 safety factor on the kept portion.
    Never splits tool pairs.
    Returns new message list with summary injected as system message.
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system  = [m for m in messages if m.get("role") != "system"]

    if len(non_system) < 4:
        logger.info("[SessionCompact] Too few messages to compact — skipping")
        return messages

    # Keep recent 40%, apply 4/3 safety factor
    keep_count = max(2, int(len(non_system) * 0.40 * (3 / 4)))
    split_idx  = len(non_system) - keep_count

    # Adjust so we don't split a tool pair
    for _ in range(len(non_system)):
        adjusted = _never_split_tool_pair(non_system, split_idx)
        if adjusted == split_idx:
            break
        split_idx = adjusted

    to_summarise = non_system[:split_idx]
    to_keep      = non_system[split_idx:]

    if not to_summarise:
        logger.info("[SessionCompact] Nothing to summarise after split adjustment")
        return messages

    logger.info(
        f"[SessionCompact] summarising {len(to_summarise)} msgs, keeping {len(to_keep)}"
    )

    try:
        summary_text = _qwen_summarise(to_summarise)
    except Exception:
        logger.warning("[SessionCompact] Qwen failed — returning original messages")
        return messages

    summary_msg = {
        "role": "system",
        "content": f"[COMPRESSED CONTEXT SUMMARY]\n{summary_text}",
    }

    result = system_msgs + [summary_msg] + to_keep
    logger.info(
        f"[SessionCompact] DONE — {len(messages)} -> {len(result)} messages"
    )
    return result


# ---------------------------------------------------------------------------
# Layer 3: FullCompact
# ---------------------------------------------------------------------------

def full_compact(messages: list, max_tokens: int, token_counter) -> list:
    """
    Round-based progressive discard with circuit breaker.
    Stops after CIRCUIT_BREAK_LIMIT consecutive Qwen failures.
    token_counter: callable(messages) -> int
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system  = [m for m in messages if m.get("role") != "system"]

    failures      = 0
    round_num     = 0
    current_msgs  = non_system[:]
    target_tokens = int(max_tokens * 0.70)  # aim for 70% after full compact

    logger.info(
        f"[FullCompact] START — {len(messages)} msgs, target <={target_tokens} tokens"
    )

    while failures < CIRCUIT_BREAK_LIMIT:
        round_num += 1
        total_msgs = system_msgs + current_msgs
        current_tokens = token_counter(total_msgs)

        if current_tokens <= target_tokens:
            logger.info(
                f"[FullCompact] round {round_num} — reached target "
                f"({current_tokens}<={target_tokens}) DONE"
            )
            break

        if len(current_msgs) < 4:
            logger.warning("[FullCompact] Too few messages left — hard drop remaining")
            # Hard drop: keep only last 2 non-system messages
            current_msgs = current_msgs[-2:] if len(current_msgs) >= 2 else current_msgs
            failures = 0  # reset per spec (circuit breaker resets after hard drop)
            break

        # Summarise oldest 25% of non-system messages
        chunk_size = max(2, len(current_msgs) // 4)
        split_idx  = chunk_size
        split_idx  = _never_split_tool_pair(current_msgs, split_idx)

        chunk    = current_msgs[:split_idx]
        remaining = current_msgs[split_idx:]

        try:
            summary_text = _qwen_summarise(chunk)
            summary_msg  = {
                "role": "system",
                "content": f"[COMPRESSED CONTEXT SUMMARY — round {round_num}]\n{summary_text}",
            }
            current_msgs = [summary_msg] + remaining
            failures = 0
            logger.info(
                f"[FullCompact] round {round_num} — summarised {len(chunk)} msgs, "
                f"remaining {len(remaining)}"
            )
        except Exception as e:
            failures += 1
            logger.error(
                f"[FullCompact] round {round_num} Qwen failure {failures}/{CIRCUIT_BREAK_LIMIT}: {e}"
            )

    if failures >= CIRCUIT_BREAK_LIMIT:
        logger.error("[FullCompact] Circuit breaker tripped — hard dropping oldest half")
        mid = len(current_msgs) // 2
        current_msgs = current_msgs[mid:]
        failures = 0

    result = system_msgs + current_msgs
    logger.info(f"[FullCompact] DONE — {len(messages)} -> {len(result)} messages")
    return result