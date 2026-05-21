"""
auto_compact_manager.py — Thread-safe singleton orchestrating the 3-layer cascade.
Ref: gitee.com/free/claude-code AutoCompactManager.auto_compact_if_needed()

Cascade:
  Layer 0: is_filter_call gate  (skip ALL compression for Qwen filter calls)
  Layer 1: MicroCompact         (standalone module, zero API calls)
  Layer 2: SessionMemoryCompact (ONE Qwen call)
  Layer 3: FullCompact          (multi-round + circuit breaker)
"""

import logging
import os
import threading
import time

from token_tracker import TokenTracker
from micro_compact import micro_compact
from context_compressor import session_memory_compact, full_compact

_LOG_DIR = r"C:\Users\sgarm\.openclaw\logs"
os.makedirs(_LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(_LOG_DIR, "compress.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("openclaw.auto_compact")

# WhatsApp bridge for CRITICAL alerts (imported lazily to avoid circular import)
_WHATSAPP_OWNER = "+918529911832"
_ALERT_COOLDOWN_SEC = 300   # 5-minute rate limit on CRITICAL WhatsApp alerts


class AutoCompactManager:
    """Thread-safe singleton. One instance per model_name."""

    _instances: dict = {}
    _class_lock = threading.Lock()

    def __new__(cls, model_name: str = "gemini-2.5-flash"):
        with cls._class_lock:
            if model_name not in cls._instances:
                instance = super().__new__(cls)
                cls._instances[model_name] = instance
            return cls._instances[model_name]

    def __init__(self, model_name: str = "gemini-2.5-flash"):
        # Guard: only initialise once
        if getattr(self, "_initialised", False):
            return
        self._initialised    = True
        self.model_name      = model_name
        self.tracker         = TokenTracker(model_name)
        self.session_start   = time.time()
        self._compact_count  = {"micro": 0, "session": 0, "full": 0}
        self._lock           = threading.Lock()
        self._last_alert_ts  = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def auto_compact_if_needed(self, messages: list, is_filter_call: bool = False) -> list:
        """
        Main entry point. Call from ollama_proxy.py before forwarding to Gemini/Qwen.
        Returns (possibly compacted) messages list.
        """
        # Layer 0: never touch filter calls
        if is_filter_call:
            return messages

        self.tracker.update(messages)
        ratio = self.tracker.ratio()
        level = self.tracker.status_level()

        logger.info(
            f"[TokenTracker] model={self.model_name} "
            f"tokens={self.tracker.current_tokens}/{self.tracker.max_tokens} "
            f"ratio={ratio:.3f} level={level}"
        )

        if not self.tracker.should_auto_compact():
            return messages

        logger.warning(
            f"[AutoCompact] TRIGGERED at {ratio:.1%} — starting cascade"
        )
        self._maybe_send_critical_alert(ratio)

        # Layer 1 — MicroCompact (always runs first)
        messages = micro_compact(messages, self.session_start)
        with self._lock:
            self._compact_count["micro"] += 1

        self.tracker.update(messages)
        if not self.tracker.should_auto_compact():
            logger.info("[AutoCompact] MicroCompact sufficient — cascade done")
            return messages

        # Layer 2 — SessionMemoryCompact
        messages = session_memory_compact(messages, self.tracker.max_tokens)
        with self._lock:
            self._compact_count["session"] += 1

        self.tracker.update(messages)
        if not self.tracker.should_auto_compact():
            logger.info("[AutoCompact] SessionCompact sufficient — cascade done")
            return messages

        # Layer 3 — FullCompact
        messages = full_compact(
            messages,
            self.tracker.max_tokens,
            token_counter=self.tracker.count_messages,
        )
        with self._lock:
            self._compact_count["full"] += 1

        logger.warning("[AutoCompact] FullCompact complete — cascade done")
        return messages

    def get_stats(self) -> dict:
        with self._lock:
            counts = dict(self._compact_count)
        return {
            "model": self.model_name,
            "max_tokens": self.tracker.max_tokens,
            "current_tokens": self.tracker.current_tokens,
            "ratio": round(self.tracker.ratio(), 4),
            "level": self.tracker.status_level(),
            "compactions": counts,
            "session_age_min": round(self.tracker.session_age_minutes(), 1),
        }

    def compress_status_reply(self) -> str:
        s = self.get_stats()
        c = s["compactions"]
        return (
            f"Compression Status\n"
            f"Model: {s['model']}\n"
            f"Tokens: {s['current_tokens']}/{s['max_tokens']} ({s['ratio']*100:.1f}%)\n"
            f"Level: {s['level']}\n"
            f"Compactions: micro={c['micro']} session={c['session']} full={c['full']}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_send_critical_alert(self, ratio: float) -> None:
        now = time.time()
        if now - self._last_alert_ts < _ALERT_COOLDOWN_SEC:
            return
        self._last_alert_ts = now
        msg = (
            f"[OpenClaw ALERT] Context at {ratio:.1%} — compaction triggered. "
            f"Model: {self.model_name}"
        )
        logger.warning(f"[Alert] Sending WhatsApp CRITICAL to {_WHATSAPP_OWNER}: {msg}")
        try:
            import requests
            requests.post(
                "http://localhost:5001/send",
                json={"to": _WHATSAPP_OWNER, "message": msg},
                timeout=5,
            )
        except Exception as e:
            logger.error(f"[Alert] WhatsApp send failed: {e}")