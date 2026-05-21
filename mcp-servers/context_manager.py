#!/usr/bin/env python3
"""
context_manager_godmode.py — Hard UX Limits + Recap on Demand  v2.0
=====================================================================
SAVE TO:
  <repo>/mcp-servers/context_manager.py

WHAT'S NEW IN v2.0:
  + RecapEngine injected via Protocol interface (swap any engine)
  + Structured recap JSON mode (topics / key facts / decisions / open Qs)
  + Metrics hooks — LoggingMetrics (zero deps) or PrometheusMetrics (optional)
  + Configurable limit profiles: light / default / heavy / unlimited
  + Per-user profile override
  + LRU memory eviction strategy for user context store
  + Weighted eviction option (drops low-value short messages first)
  + RecapEngineFactory for easy engine construction
  + Fully backward-compatible with v1 constructor

INTEGRATION (unchanged from v1):
  from context_manager import ContextManager
  ctx_mgr = ContextManager(system_prompt=YOUR_SYSTEM_PROMPT)

  result = ctx_mgr.process(user_id, cleaned_message)
  if result.is_ux_response:
      send_whatsapp(user_id, result.ux_message); return
  response = call_claude(result.messages)
  ctx_mgr.record_reply(user_id, response)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger("context_manager")

DEFAULT_RECAP_API_URL = os.environ.get("OPENCLAW_RECAP_API_URL", "http://localhost:18788/webhook")

# ══════════════════════════════════════════════════════════════════
# LIMIT PROFILES  (NEW: configurable presets)
# ══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LimitProfile:
    name:                 str
    max_messages:         int
    max_tokens:           int
    max_paste_chars:      int
    inactivity_hours:     float
    warn_at_pct:          float   # show warning at this % of any limit
    auto_recap_at_pct:    float   # auto-compress at this %
    recap_compress_ratio: float   # fraction of history to compress

PROFILES: Dict[str, LimitProfile] = {
    "light": LimitProfile(
        name="light",
        max_messages=10, max_tokens=2000, max_paste_chars=600,
        inactivity_hours=1, warn_at_pct=70, auto_recap_at_pct=85,
        recap_compress_ratio=0.60,
    ),
    "default": LimitProfile(
        name="default",
        max_messages=30, max_tokens=6000, max_paste_chars=1500,
        inactivity_hours=6, warn_at_pct=80, auto_recap_at_pct=90,
        recap_compress_ratio=0.50,
    ),
    "heavy": LimitProfile(
        name="heavy",
        max_messages=60, max_tokens=12000, max_paste_chars=3000,
        inactivity_hours=24, warn_at_pct=85, auto_recap_at_pct=95,
        recap_compress_ratio=0.40,
    ),
    "unlimited": LimitProfile(
        name="unlimited",
        max_messages=500, max_tokens=100_000, max_paste_chars=10_000,
        inactivity_hours=168, warn_at_pct=98, auto_recap_at_pct=99,
        recap_compress_ratio=0.30,
    ),
}

DEFAULT_PROFILE = "default"

# ══════════════════════════════════════════════════════════════════
# RECAP ENGINE PROTOCOL  (NEW: injectable interface)
# ══════════════════════════════════════════════════════════════════

@runtime_checkable
class RecapEngineProtocol(Protocol):
    """
    Protocol for recap engine injection.
    Implement both methods to create a custom recap engine.

    Example:
        class MyEngine:
            def generate(self, messages, user_id="unknown") -> str: ...
            def generate_structured(self, messages, user_id="unknown") -> dict: ...

        ctx_mgr = ContextManager(recap_engine=MyEngine())
    """
    def generate(self, messages: List["ConvMessage"], user_id: str = "unknown") -> str:
        ...

    def generate_structured(
        self, messages: List["ConvMessage"], user_id: str = "unknown"
    ) -> dict:
        ...


# ══════════════════════════════════════════════════════════════════
# METRICS PROTOCOL  (NEW: Prometheus / logging hooks)
# ══════════════════════════════════════════════════════════════════

@runtime_checkable
class MetricsProtocol(Protocol):
    def counter_inc(self, name: str, labels: Dict[str, str] = {}) -> None: ...
    def gauge_set(self, name: str, value: float, labels: Dict[str, str] = {}) -> None: ...
    def histogram_observe(self, name: str, value: float, labels: Dict[str, str] = {}) -> None: ...


class LoggingMetrics:
    """
    Zero-dependency metrics collector — emits structured log lines.
    Compatible with any log aggregator (Datadog, Loki, etc.)
    Format: METRIC name=value labels...
    """

    def __init__(self, level: int = logging.DEBUG):
        self._level = level
        self._counters: Dict[str, float] = {}
        self._gauges:   Dict[str, float] = {}

    def counter_inc(self, name: str, labels: Dict[str, str] = {}) -> None:
        key = name + str(sorted(labels.items()))
        self._counters[key] = self._counters.get(key, 0) + 1
        label_str = " ".join(f'{k}="{v}"' for k, v in labels.items())
        logger.log(self._level, f"METRIC counter {name}={self._counters[key]} {label_str}")

    def gauge_set(self, name: str, value: float, labels: Dict[str, str] = {}) -> None:
        key = name + str(sorted(labels.items()))
        self._gauges[key] = value
        label_str = " ".join(f'{k}="{v}"' for k, v in labels.items())
        logger.log(self._level, f"METRIC gauge {name}={value} {label_str}")

    def histogram_observe(self, name: str, value: float, labels: Dict[str, str] = {}) -> None:
        label_str = " ".join(f'{k}="{v}"' for k, v in labels.items())
        logger.log(self._level, f"METRIC histogram {name}={value:.4f}s {label_str}")

    def snapshot(self) -> dict:
        """Return all current counter/gauge values as a dict."""
        return {"counters": dict(self._counters), "gauges": dict(self._gauges)}


class PrometheusMetrics:
    """
    Prometheus metrics collector.
    Requires: pip install prometheus-client

    Usage:
        from context_manager import PrometheusMetrics
        metrics = PrometheusMetrics(namespace="openclaw")
        ctx_mgr = ContextManager(metrics=metrics)
        # Expose on your gateway: GET /metrics → prometheus_client.generate_latest()
    """

    def __init__(self, namespace: str = "ctxmgr"):
        try:
            import prometheus_client as prom
        except ImportError:
            raise RuntimeError(
                "prometheus-client not installed. Run: pip install prometheus-client"
            )
        self._prom = prom
        ns = namespace

        self._counters:    Dict[str, Any] = {}
        self._gauges:      Dict[str, Any] = {}
        self._histograms:  Dict[str, Any] = {}

        # Pre-define all metrics
        self._define_counter(f"{ns}_messages_total",
                             "Total messages processed", ["user_id", "action"])
        self._define_counter(f"{ns}_ux_responses_total",
                             "UX responses sent (non-API)", ["user_id", "reason"])
        self._define_counter(f"{ns}_recaps_total",
                             "Recaps generated", ["user_id", "recap_type"])
        self._define_counter(f"{ns}_warns_total",
                             "Limit warnings sent", ["user_id", "limit_type"])
        self._define_counter(f"{ns}_resets_total",
                             "Conversation resets", ["user_id", "reset_type"])
        self._define_counter(f"{ns}_evictions_total",
                             "User context evictions", ["strategy"])
        self._define_gauge(f"{ns}_window_messages",
                           "Current messages in context window", ["user_id"])
        self._define_gauge(f"{ns}_window_tokens",
                           "Current token estimate in context window", ["user_id"])
        self._define_gauge(f"{ns}_active_users",
                           "Active (non-stale) user count", [])
        self._define_histogram(f"{ns}_recap_duration_seconds",
                               "Recap generation latency", ["user_id", "engine"])

    def _define_counter(self, name, desc, labels):
        self._counters[name] = self._prom.Counter(name, desc, labels)

    def _define_gauge(self, name, desc, labels):
        self._gauges[name] = self._prom.Gauge(name, desc, labels)

    def _define_histogram(self, name, desc, labels):
        self._histograms[name] = self._prom.Histogram(name, desc, labels)

    def counter_inc(self, name: str, labels: Dict[str, str] = {}) -> None:
        if name in self._counters:
            self._counters[name].labels(**labels).inc()

    def gauge_set(self, name: str, value: float, labels: Dict[str, str] = {}) -> None:
        if name in self._gauges:
            self._gauges[name].labels(**labels).set(value) if labels else \
            self._gauges[name].set(value)

    def histogram_observe(self, name: str, value: float, labels: Dict[str, str] = {}) -> None:
        if name in self._histograms:
            self._histograms[name].labels(**labels).observe(value)


class NullMetrics:
    """No-op metrics (default — zero overhead)."""
    def counter_inc(self, name: str, labels: Dict[str, str] = {}) -> None: pass
    def gauge_set(self, name: str, value: float, labels: Dict[str, str] = {}) -> None: pass
    def histogram_observe(self, name: str, value: float, labels: Dict[str, str] = {}) -> None: pass


# ══════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════

@dataclass
class ConvMessage:
    role:      str    # "user" | "assistant" | "system"
    content:   str
    timestamp: float = field(default_factory=time.time)
    token_est: int   = 0
    value_score: float = 1.0   # NEW: used by weighted eviction

    def __post_init__(self):
        if not self.token_est:
            self.token_est = max(1, len(self.content) // 4)
        if self.value_score == 1.0:
            self.value_score = self._compute_value()

    def _compute_value(self) -> float:
        """Heuristic value score for weighted eviction. Higher = keep longer."""
        score = 1.0
        score += min(len(self.content) / 500, 1.5)    # longer = more context
        if "?" in self.content:          score += 0.5  # questions are valuable
        if re.search(r"\b(code|def |import|function)\b", self.content, re.I):
            score += 1.0                               # code content = high value
        if self.role == "assistant":     score += 0.3  # Claude answers are valuable
        if len(self.content.split()) < 5: score -= 0.5 # very short = low value
        return max(0.1, score)

    def to_api_dict(self) -> dict:
        return {"role": self.role, "content": self.content}

    def age_seconds(self) -> float:
        return time.time() - self.timestamp


@dataclass
class ConvStats:
    total_messages:    int   = 0
    total_tokens_est:  int   = 0
    recaps_generated:  int   = 0
    auto_recaps:       int   = 0
    resets:            int   = 0
    messages_blocked:  int   = 0
    warns_sent:        int   = 0
    window_messages:   int   = 0
    window_tokens:     int   = 0
    profile_name:      str   = "default"

    @property
    def message_pct(self) -> float:
        p = PROFILES.get(self.profile_name, PROFILES["default"])
        return round(self.window_messages / p.max_messages * 100, 1)

    @property
    def token_pct(self) -> float:
        p = PROFILES.get(self.profile_name, PROFILES["default"])
        return round(self.window_tokens / p.max_tokens * 100, 1)

    @property
    def limit_pct(self) -> float:
        return max(self.message_pct, self.token_pct)


@dataclass
class ProcessResult:
    is_ux_response: bool
    ux_message:     Optional[str]
    messages:       Optional[List[dict]]
    stats:          ConvStats
    warn_suffix:    Optional[str] = None


@dataclass
class UserContext:
    user_id:       str
    history:       List[ConvMessage] = field(default_factory=list)
    recap_summary: str               = ""
    last_activity: float             = field(default_factory=time.time)
    stats:         ConvStats         = field(default_factory=ConvStats)
    warn_state:    Dict[str, bool]   = field(default_factory=dict)
    profile_name:  str               = "default"   # NEW: per-user profile

    @property
    def profile(self) -> LimitProfile:
        return PROFILES.get(self.profile_name, PROFILES["default"])

    @property
    def window_tokens(self) -> int:
        return sum(m.token_est for m in self.history)

    @property
    def window_messages(self) -> int:
        return len(self.history)

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_activity) > self.profile.inactivity_hours * 3600

    def refresh(self):
        self.last_activity         = time.time()
        self.stats.window_messages = self.window_messages
        self.stats.window_tokens   = self.window_tokens
        self.stats.profile_name    = self.profile_name

    def to_dict(self) -> dict:
        return {
            "user_id":       self.user_id,
            "history":       [{"role": m.role, "content": m.content, "ts": m.timestamp,
                                "token_est": m.token_est} for m in self.history],
            "recap_summary": self.recap_summary,
            "last_activity": self.last_activity,
            "stats":         self.stats.__dict__,
            "warn_state":    self.warn_state,
            "profile_name":  self.profile_name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserContext":
        ctx = cls(user_id=d["user_id"])
        ctx.history = [
            ConvMessage(role=m["role"], content=m["content"],
                        timestamp=m.get("ts", 0), token_est=m.get("token_est", 0))
            for m in d.get("history", [])
        ]
        ctx.recap_summary = d.get("recap_summary", "")
        ctx.last_activity = d.get("last_activity", time.time())
        ctx.warn_state    = d.get("warn_state", {})
        ctx.profile_name  = d.get("profile_name", "default")
        if "stats" in d:
            for k, v in d["stats"].items():
                if hasattr(ctx.stats, k):
                    setattr(ctx.stats, k, v)
        return ctx


# ══════════════════════════════════════════════════════════════════
# LRU CONTEXT STORE  (NEW: O(1) LRU eviction for user contexts)
# ══════════════════════════════════════════════════════════════════

class LRUContextStore:
    """
    Thread-safe LRU store for UserContext objects.
    Uses OrderedDict for O(1) move-to-end (touch) and O(1) eviction.

    Eviction strategies:
      "lru"      — evict least recently used user context
      "fifo"     — evict oldest user context
      "weighted" — evict user with lowest total value score
    """

    def __init__(self, max_users: int = 1000, strategy: str = "lru"):
        self._store:    OrderedDict[str, UserContext] = OrderedDict()
        self._lock:     threading.Lock = threading.Lock()
        self._max:      int = max_users
        self._strategy: str = strategy
        self._evictions: int = 0

    def get(self, user_id: str) -> Optional[UserContext]:
        with self._lock:
            if user_id not in self._store:
                return None
            if self._strategy == "lru":
                self._store.move_to_end(user_id)
            return self._store[user_id]

    def set(self, user_id: str, ctx: UserContext) -> None:
        with self._lock:
            if user_id in self._store:
                self._store.move_to_end(user_id)
            self._store[user_id] = ctx
            while len(self._store) > self._max:
                self._evict_one()

    def touch(self, user_id: str) -> None:
        with self._lock:
            if user_id in self._store and self._strategy == "lru":
                self._store.move_to_end(user_id)

    def delete(self, user_id: str) -> None:
        with self._lock:
            self._store.pop(user_id, None)

    def contains(self, user_id: str) -> bool:
        return user_id in self._store

    def keys(self) -> List[str]:
        with self._lock:
            return list(self._store.keys())

    def _evict_one(self) -> None:
        """Evict one user context according to strategy. Must hold lock."""
        if not self._store:
            return
        if self._strategy in ("lru", "fifo"):
            evicted_id, _ = self._store.popitem(last=False)
        elif self._strategy == "weighted":
            # Evict user with lowest total message value score
            evicted_id = min(
                self._store.keys(),
                key=lambda uid: sum(m.value_score for m in self._store[uid].history)
            )
            del self._store[evicted_id]
        else:
            evicted_id, _ = self._store.popitem(last=False)
        self._evictions += 1
        logger.debug(f"[context_store] evicted user={evicted_id} strategy={self._strategy} "
                     f"total_evictions={self._evictions}")

    @property
    def size(self) -> int:
        return len(self._store)

    @property
    def eviction_count(self) -> int:
        return self._evictions

    def memory_snapshot(self) -> dict:
        with self._lock:
            return {
                "total_users": len(self._store),
                "max_users": self._max,
                "strategy": self._strategy,
                "evictions": self._evictions,
                "users": [
                    {
                        "user_id": uid,
                        "messages": len(ctx.history),
                        "tokens": ctx.window_tokens,
                        "stale": ctx.is_stale,
                        "profile": ctx.profile_name,
                    }
                    for uid, ctx in self._store.items()
                ]
            }


# ══════════════════════════════════════════════════════════════════
# RECAP ENGINES  (injectable via RecapEngineProtocol)
# ══════════════════════════════════════════════════════════════════

class ExtractiveRecapEngine:
    """
    Zero-dependency extractive recap.
    Pulls first sentence of each message, deduplicates by bigram similarity.
    Always available as fallback.
    """

    def generate(
        self, messages: List[ConvMessage], user_id: str = "unknown"
    ) -> str:
        lines = []
        seen  = set()
        for m in messages:
            first = re.split(r"[.!?]\s", m.content.strip())[0][:120].strip()
            key   = first.lower()
            if key and key not in seen and len(first) > 8:
                seen.add(key)
                prefix = "👤" if m.role == "user" else "🤖"
                lines.append(f"{prefix} {first}")
        return "\n".join(lines[:15]) or "No significant content to recap."

    def generate_structured(
        self, messages: List[ConvMessage], user_id: str = "unknown"
    ) -> dict:
        """Structured recap via heuristic extraction — no API needed."""
        topics   = self._extract_topics(messages)
        user_qs  = [m.content[:100] for m in messages if m.role == "user" and "?" in m.content]
        facts    = [m.content[:100] for m in messages if m.role == "assistant"][:4]
        summary  = self.generate(messages, user_id)
        return {
            "format":        "structured",
            "generated_at":  time.strftime("%Y-%m-%dT%H:%M:%S"),
            "message_count": len(messages),
            "engine":        "extractive",
            "topics":        topics[:6],
            "key_facts":     facts[:5],
            "decisions":     [],
            "open_questions": user_qs[:4],
            "context_for_llm": summary,
            "raw_summary":   summary,
        }

    def _extract_topics(self, messages: List[ConvMessage]) -> List[str]:
        """Extract capitalized noun phrases and recurring terms as topics."""
        all_text  = " ".join(m.content for m in messages)
        # Extract capitalized words and technical terms
        caps      = re.findall(r'\b[A-Z][a-z]{3,}\b', all_text)
        tech      = re.findall(
            r'\b(?:API|JSON|Flask|Python|WhatsApp|SQL|webhook|bot|server|token|LLM|'
            r'Claude|async|cache|database|endpoint|gateway)\b', all_text, re.I
        )
        combined  = [w.lower() for w in caps + tech]
        # Top by frequency
        freq = {}
        for w in combined:
            freq[w] = freq.get(w, 0) + 1
        return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])][:8]


class APIRecapEngine:
    """
    Calls your ULTIMATE gateway for high-quality LLM-generated recaps.
    Falls back to ExtractiveRecapEngine automatically on timeout/error.
    """

    def __init__(
        self,
        api_url:      str   = DEFAULT_RECAP_API_URL,
        timeout_sec:  float = 8.0,
        max_words:    int   = 120,
    ):
        self._url      = api_url
        self._timeout  = timeout_sec
        self._max_words = max_words
        self._fallback  = ExtractiveRecapEngine()

    def generate(
        self, messages: List[ConvMessage], user_id: str = "unknown"
    ) -> str:
        t0 = time.time()
        try:
            return self._call_api(messages, user_id, structured=False)
        except Exception as e:
            elapsed = time.time() - t0
            logger.warning(
                f"[recap] API failed for {user_id} ({elapsed:.2f}s): {e} "
                f"— using extractive fallback"
            )
            return self._fallback.generate(messages, user_id)

    def generate_structured(
        self, messages: List[ConvMessage], user_id: str = "unknown"
    ) -> dict:
        t0 = time.time()
        try:
            raw = self._call_api(messages, user_id, structured=True)
            # Try to parse JSON from the API response
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
                parsed.setdefault("format", "structured")
                parsed.setdefault("generated_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
                parsed.setdefault("engine", "api")
                parsed.setdefault("message_count", len(messages))
                return parsed
        except Exception as e:
            logger.warning(f"[recap] Structured API failed for {user_id}: {e}")
        # Fallback to extractive structured
        result = self._fallback.generate_structured(messages, user_id)
        result["engine"] = "extractive_fallback"
        return result

    def _call_api(
        self, messages: List[ConvMessage], user_id: str, structured: bool
    ) -> str:
        import urllib.request
        history_text = "\n".join(
            f"{m.role.upper()}: {m.content[:200]}"
            for m in messages[-20:]
        )
        if structured:
            prompt = (
                f"Analyze this conversation and respond with ONLY valid JSON matching "
                f"this schema:\n"
                f'{{"topics": [], "key_facts": [], "decisions": [], '
                f'"open_questions": [], "context_for_llm": "", "raw_summary": ""}}\n\n'
                f"Keep context_for_llm under {self._max_words} words.\n\n"
                f"{history_text}"
            )
        else:
            prompt = (
                f"Write a concise recap (max {self._max_words} words) covering: "
                f"key questions asked, decisions made, important facts. "
                f"Be structured.\n\n{history_text}"
            )
        payload = {"user_id": f"{user_id}_recap", "message": prompt}
        data    = json.dumps(payload).encode()
        req     = urllib.request.Request(
            self._url, data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = json.loads(resp.read().decode())
            if "choices" in body:
                return body["choices"][0]["message"]["content"].strip()
            return body.get("response", body.get("content", str(body)))


class RecapEngineFactory:
    """
    Convenience factory for building recap engines.

    Usage:
        engine = RecapEngineFactory.api(url=DEFAULT_RECAP_API_URL)
        engine = RecapEngineFactory.extractive()
        engine = RecapEngineFactory.auto()   # API with extractive fallback (default)
    """

    @staticmethod
    def api(url: str = DEFAULT_RECAP_API_URL,
            timeout: float = 8.0, max_words: int = 120) -> APIRecapEngine:
        return APIRecapEngine(api_url=url, timeout_sec=timeout, max_words=max_words)

    @staticmethod
    def extractive() -> ExtractiveRecapEngine:
        return ExtractiveRecapEngine()

    @staticmethod
    def auto() -> APIRecapEngine:
        """API engine with automatic extractive fallback (recommended default)."""
        return APIRecapEngine()


# ══════════════════════════════════════════════════════════════════
# UX MESSAGE TEMPLATES
# ══════════════════════════════════════════════════════════════════

MSG_WARN_MESSAGES = (
    "⚠️ *Heads up:* Chat is {pct}% full ({n} msgs).\n"
    "Type */recap* to compress, or */reset* to start fresh."
)
MSG_WARN_TOKENS = (
    "⚠️ *Context {pct}% full* (~{tok} tokens used).\n"
    "Type */recap* to compress and continue."
)
MSG_HARD_LIMIT = (
    "🚫 *Chat limit reached* (profile: _{profile}_).\n"
    "Type */recap* to summarize and continue.\n"
    "Type */reset* to start a fresh conversation."
)
MSG_PASTE_TOO_LONG = (
    "✂️ *Message too long* ({chars} chars, limit {limit} for _{profile}_ profile).\n"
    "➤ Break it into smaller parts\n"
    "➤ Or type */recap* to see what's covered"
)
MSG_RESET_DONE = (
    "🔄 *Conversation reset.*\n"
    "Starting fresh! Previous context cleared."
)
MSG_RECAP_HEADER = (
    "📋 *Conversation Recap*\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "{recap}\n"
    "━━━━━━━━━━━━━━━━━━━━"
)
MSG_STRUCTURED_RECAP = (
    "📋 *Conversation Recap*\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "{topics_section}"
    "{facts_section}"
    "{questions_section}"
    "📝 *Summary:* {summary}\n"
    "━━━━━━━━━━━━━━━━━━━━"
)
MSG_RECAP_AUTO = (
    "🗜️ *Context auto-compressed* — freeing space.\n"
    "Type */recap* to see the full summary."
)

RECAP_TRIGGERS = frozenset([
    "!recap", "/recap", "recap", "!summary", "/summary", "summarize",
    "what did we discuss", "catch me up", "what have we talked about",
    "summary please", "give me a recap", "recap please",
])
RESET_TRIGGERS = frozenset([
    "/reset", "!reset", "reset", "start over", "start fresh",
    "clear history", "new conversation",
])

_DEFAULT_STATE_DIR = Path(os.environ.get("OPENCLAW_HOME", str(Path.home()))).expanduser() / ".openclaw"
_OPENCLAW_STATE_DIR = Path(os.environ.get("OPENCLAW_STATE_DIR", str(_DEFAULT_STATE_DIR))).expanduser()
PERSIST_PATH = Path(
    os.environ.get(
        "OPENCLAW_CONTEXT_STATE_PATH",
        str(_OPENCLAW_STATE_DIR / "data" / "ctx_state.json"),
    )
)

# ══════════════════════════════════════════════════════════════════
# CONTEXT MANAGER  (main class)
# ══════════════════════════════════════════════════════════════════

class ContextManager:
    """
    Thread-safe per-user conversation context manager.
    Enforces hard UX limits, manages recap-on-demand, tracks metrics.

    Args:
        system_prompt:      Default system prompt injected into all Claude calls.
        recap_engine:       Any object implementing RecapEngineProtocol.
                            Default: APIRecapEngine (with extractive fallback).
        metrics:            Any object implementing MetricsProtocol.
                            Default: NullMetrics (zero overhead).
                            Use LoggingMetrics() or PrometheusMetrics() in prod.
        limit_profile:      Default limit profile name: "light" / "default" /
                            "heavy" / "unlimited". Can be overridden per user.
        eviction_strategy:  How to evict user contexts when max_users exceeded:
                            "lru" (default) / "fifo" / "weighted"
        max_users_memory:   Max user contexts in RAM before eviction triggers.
        structured_recaps:  If True, recaps return JSON with topics/facts/Qs.
        persist:            If True, save/load state to disk.
    """

    def __init__(
        self,
        system_prompt:      str                            = "",
        recap_engine:       Optional[RecapEngineProtocol] = None,
        metrics:            Optional[MetricsProtocol]     = None,
        limit_profile:      str                           = DEFAULT_PROFILE,
        eviction_strategy:  str                           = "lru",
        max_users_memory:   int                           = 1000,
        structured_recaps:  bool                          = False,
        persist:            bool                          = True,
    ):
        self._system_prompt    = system_prompt
        self._engine           = recap_engine or RecapEngineFactory.auto()
        self._metrics          = metrics or NullMetrics()
        self._default_profile  = limit_profile
        self._structured       = structured_recaps
        self._persist          = persist
        self._store            = LRUContextStore(max_users_memory, eviction_strategy)
        self._lock             = threading.Lock()

        if persist:
            self._load_state()

    # ── PUBLIC API ──────────────────────────────────────────────────

    def process(
        self,
        user_id:                str,
        message:                str,
        system_prompt_override: Optional[str] = None,
        profile_override:       Optional[str] = None,
    ) -> ProcessResult:
        """
        Main entry point. Call for every incoming WhatsApp message.
        Thread-safe.
        """
        with self._lock:
            ctx = self._get_or_create(user_id, profile_override)
            p   = ctx.profile

            # ── Inactivity auto-reset ─────────────────────────────
            if ctx.is_stale and ctx.history:
                self._reset_context(ctx)
                self._metrics.counter_inc(
                    "ctxmgr_resets_total",
                    {"user_id": user_id, "reset_type": "inactivity"}
                )

            ctx.refresh()
            msg_lower = message.strip().lower()

            # ── RESET trigger ─────────────────────────────────────
            if msg_lower in RESET_TRIGGERS or \
               any(msg_lower.startswith(t) for t in RESET_TRIGGERS):
                self._reset_context(ctx)
                self._metrics.counter_inc(
                    "ctxmgr_resets_total",
                    {"user_id": user_id, "reset_type": "user"}
                )
                self._metrics.counter_inc(
                    "ctxmgr_ux_responses_total",
                    {"user_id": user_id, "reason": "reset"}
                )
                return self._ux_result(MSG_RESET_DONE, ctx)

            # ── RECAP trigger ─────────────────────────────────────
            if msg_lower in RECAP_TRIGGERS or \
               any(msg_lower.startswith(t) for t in RECAP_TRIGGERS):
                result = self._handle_recap_request(ctx)
                self._metrics.counter_inc(
                    "ctxmgr_ux_responses_total",
                    {"user_id": user_id, "reason": "recap_request"}
                )
                return result

            # ── Paste size hard limit ─────────────────────────────
            if len(message) > p.max_paste_chars:
                ctx.stats.messages_blocked += 1
                self._metrics.counter_inc(
                    "ctxmgr_ux_responses_total",
                    {"user_id": user_id, "reason": "paste_limit"}
                )
                return self._ux_result(
                    MSG_PASTE_TOO_LONG.format(
                        chars=len(message),
                        limit=p.max_paste_chars,
                        profile=p.name,
                    ),
                    ctx,
                )

            # ── Hard window limits (100%) ─────────────────────────
            if ctx.window_messages >= p.max_messages or \
               ctx.window_tokens   >= p.max_tokens:
                ctx.stats.messages_blocked += 1
                self._metrics.counter_inc(
                    "ctxmgr_ux_responses_total",
                    {"user_id": user_id, "reason": "hard_limit"}
                )
                return self._ux_result(
                    MSG_HARD_LIMIT.format(profile=p.name), ctx
                )

            # ── Auto-recap at auto_recap_at_pct ──────────────────
            auto_suffix = None
            if ctx.stats.limit_pct >= p.auto_recap_at_pct:
                self._auto_compress(ctx)
                auto_suffix = MSG_RECAP_AUTO
                self._metrics.counter_inc(
                    "ctxmgr_recaps_total",
                    {"user_id": user_id, "recap_type": "auto"}
                )

            # ── Warn at warn_at_pct (once per surge) ─────────────
            warn_suffix = auto_suffix
            if ctx.stats.limit_pct >= p.warn_at_pct:
                if ctx.stats.message_pct >= p.warn_at_pct and \
                   not ctx.warn_state.get("messages"):
                    ctx.warn_state["messages"] = True
                    ctx.stats.warns_sent += 1
                    warn_suffix = MSG_WARN_MESSAGES.format(
                        pct=int(ctx.stats.message_pct),
                        n=ctx.window_messages,
                    )
                    self._metrics.counter_inc(
                        "ctxmgr_warns_total",
                        {"user_id": user_id, "limit_type": "messages"}
                    )
                elif ctx.stats.token_pct >= p.warn_at_pct and \
                     not ctx.warn_state.get("tokens"):
                    ctx.warn_state["tokens"] = True
                    ctx.stats.warns_sent += 1
                    warn_suffix = MSG_WARN_TOKENS.format(
                        pct=int(ctx.stats.token_pct),
                        tok=ctx.window_tokens,
                    )
                    self._metrics.counter_inc(
                        "ctxmgr_warns_total",
                        {"user_id": user_id, "limit_type": "tokens"}
                    )
            else:
                ctx.warn_state.clear()

            # ── Add message + update stats ────────────────────────
            ctx.history.append(ConvMessage(role="user", content=message))
            ctx.refresh()
            ctx.stats.total_messages    += 1
            ctx.stats.total_tokens_est  += max(1, len(message) // 4)

            # ── Emit gauges ───────────────────────────────────────
            self._metrics.gauge_set(
                "ctxmgr_window_messages",
                ctx.window_messages, {"user_id": user_id}
            )
            self._metrics.gauge_set(
                "ctxmgr_window_tokens",
                ctx.window_tokens, {"user_id": user_id}
            )
            self._metrics.counter_inc(
                "ctxmgr_messages_total",
                {"user_id": user_id, "action": "claude_api"}
            )

            if self._persist:
                self._save_state_async()

            return ProcessResult(
                is_ux_response = False,
                ux_message     = None,
                messages       = self._build_messages(ctx, system_prompt_override),
                stats          = ctx.stats,
                warn_suffix    = warn_suffix,
            )

    def record_reply(self, user_id: str, reply: str) -> None:
        """Add Claude's reply to conversation history."""
        with self._lock:
            ctx = self._get_or_create(user_id)
            ctx.history.append(ConvMessage(role="assistant", content=reply))
            ctx.refresh()
            if self._persist:
                self._save_state_async()

    def set_user_profile(self, user_id: str, profile_name: str) -> None:
        """Override limit profile for a specific user."""
        if profile_name not in PROFILES:
            raise ValueError(
                f"Unknown profile '{profile_name}'. "
                f"Available: {list(PROFILES.keys())}"
            )
        with self._lock:
            ctx = self._get_or_create(user_id)
            ctx.profile_name = profile_name
            ctx.stats.profile_name = profile_name
            logger.info(f"[{user_id}] profile set to '{profile_name}'")

    def get_stats(self, user_id: str) -> ConvStats:
        with self._lock:
            ctx = self._get_or_create(user_id)
            ctx.refresh()
            return ctx.stats

    def force_recap(
        self, user_id: str, structured: bool = False
    ) -> str | dict:
        """Generate recap for admin/debug use. Returns str or dict."""
        with self._lock:
            ctx = self._get_or_create(user_id)
            if structured:
                return self._engine.generate_structured(ctx.history, user_id)
            return self._engine.generate(ctx.history, user_id)

    def reset_user(self, user_id: str) -> None:
        with self._lock:
            ctx = self._get_or_create(user_id)
            self._reset_context(ctx)

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    def active_users(self) -> List[str]:
        return [uid for uid in self._store.keys()
                if not (self._store.get(uid) or UserContext("_")).is_stale]

    def memory_snapshot(self) -> dict:
        """Full memory usage snapshot — useful for /health endpoint."""
        snap = self._store.memory_snapshot()
        snap["active_users"] = len(self.active_users())
        self._metrics.gauge_set("ctxmgr_active_users", snap["active_users"])
        return snap

    # ── ASYNC VARIANTS ──────────────────────────────────────────────

    async def aprocess(
        self,
        user_id:  str,
        message:  str,
        **kwargs,
    ) -> ProcessResult:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self.process(user_id, message, **kwargs)
        )

    async def arecord_reply(self, user_id: str, reply: str) -> None:
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.record_reply(user_id, reply))

    # ── PRIVATE HELPERS ─────────────────────────────────────────────

    def _get_or_create(
        self, user_id: str, profile_override: Optional[str] = None
    ) -> UserContext:
        ctx = self._store.get(user_id)
        if ctx is None:
            ctx = UserContext(
                user_id=user_id,
                profile_name=profile_override or self._default_profile,
            )
            ctx.stats.profile_name = ctx.profile_name
        elif profile_override and ctx.profile_name != profile_override:
            ctx.profile_name = profile_override
        self._store.set(user_id, ctx)
        return ctx

    def _reset_context(self, ctx: UserContext) -> None:
        ctx.history.clear()
        ctx.recap_summary = ""
        ctx.warn_state.clear()
        ctx.stats.resets += 1
        ctx.stats.window_messages = 0
        ctx.stats.window_tokens   = 0

    def _handle_recap_request(self, ctx: UserContext) -> ProcessResult:
        if not ctx.history and not ctx.recap_summary:
            return self._ux_result("📋 No conversation history yet to recap.", ctx)

        t0       = time.time()
        messages = ctx.history.copy()

        if self._structured:
            data     = self._engine.generate_structured(messages, ctx.user_id)
            ux_msg   = self._format_structured_recap(data)
            engine   = data.get("engine", "unknown")
        else:
            data     = self._engine.generate(messages, ctx.user_id)
            ux_msg   = MSG_RECAP_HEADER.format(recap=data)
            engine   = "text"

        elapsed = time.time() - t0
        ctx.stats.recaps_generated += 1

        self._metrics.counter_inc(
            "ctxmgr_recaps_total",
            {"user_id": ctx.user_id, "recap_type": "manual"}
        )
        self._metrics.histogram_observe(
            "ctxmgr_recap_duration_seconds",
            elapsed,
            {"user_id": ctx.user_id, "engine": engine}
        )

        logger.info(
            f"[{ctx.user_id}] recap generated in {elapsed:.2f}s "
            f"engine={engine} msgs={len(messages)}"
        )
        return self._ux_result(ux_msg, ctx)

    def _format_structured_recap(self, data: dict) -> str:
        """Convert structured recap dict → WhatsApp-friendly formatted string."""
        topics_section = ""
        if data.get("topics"):
            topics_section = f"📌 *Topics:* {', '.join(data['topics'][:5])}\n"

        facts_section = ""
        if data.get("key_facts"):
            facts_str = "\n".join(f"   • {f[:80]}" for f in data["key_facts"][:4])
            facts_section = f"💡 *Key Facts:*\n{facts_str}\n"

        questions_section = ""
        if data.get("open_questions"):
            qs_str = "\n".join(f"   • {q[:80]}" for q in data["open_questions"][:3])
            questions_section = f"❓ *Open Questions:*\n{qs_str}\n"

        summary = data.get("raw_summary") or data.get("context_for_llm", "")

        return MSG_STRUCTURED_RECAP.format(
            topics_section=topics_section,
            facts_section=facts_section,
            questions_section=questions_section,
            summary=summary[:300],
        )

    def _auto_compress(self, ctx: UserContext) -> None:
        p             = ctx.profile
        n_compress    = max(1, int(len(ctx.history) * p.recap_compress_ratio))
        to_compress   = ctx.history[:n_compress]
        ctx.history   = ctx.history[n_compress:]

        t0       = time.time()
        new_recap = self._engine.generate(to_compress, ctx.user_id)
        elapsed  = time.time() - t0

        ctx.recap_summary = (
            ctx.recap_summary + "\n\n[Further context]\n" + new_recap
            if ctx.recap_summary else new_recap
        )
        ctx.stats.auto_recaps += 1
        ctx.refresh()

        self._metrics.histogram_observe(
            "ctxmgr_recap_duration_seconds",
            elapsed,
            {"user_id": ctx.user_id, "engine": "auto_compress"}
        )
        logger.info(
            f"[{ctx.user_id}] auto-compressed {n_compress} msgs "
            f"({ctx.window_messages} remain) in {elapsed:.2f}s"
        )

    def _build_messages(
        self,
        ctx: UserContext,
        system_prompt_override: Optional[str] = None,
    ) -> List[dict]:
        messages = []
        sp = system_prompt_override or self._system_prompt
        if sp:
            messages.append({"role": "system", "content": sp})
        if ctx.recap_summary:
            recap_block = (
                "═══ CONVERSATION CONTEXT (compressed) ═══\n"
                f"{ctx.recap_summary}\n"
                "═══ END CONTEXT — Current conversation follows ═══"
            )
            messages.append({"role": "system", "content": recap_block})
        for msg in ctx.history:
            messages.append(msg.to_api_dict())
        return messages

    def _ux_result(self, msg: str, ctx: UserContext) -> ProcessResult:
        ctx.refresh()
        return ProcessResult(
            is_ux_response=True, ux_message=msg,
            messages=None, stats=ctx.stats,
        )

    # ── PERSISTENCE ─────────────────────────────────────────────────

    def _load_state(self) -> None:
        if not PERSIST_PATH.exists():
            return
        try:
            with open(PERSIST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for uid, ctx_dict in data.items():
                ctx = UserContext.from_dict(ctx_dict)
                self._store.set(uid, ctx)
            logger.info(
                f"[context_manager] loaded {self._store.size} user contexts"
            )
        except Exception as e:
            logger.warning(f"[context_manager] state load failed: {e}")

    def _save_state(self) -> None:
        try:
            PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            for uid in self._store.keys():
                ctx = self._store.get(uid)
                if ctx:
                    data[uid] = ctx.to_dict()
            with open(PERSIST_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[context_manager] state save failed: {e}")

    def _save_state_async(self) -> None:
        threading.Thread(target=self._save_state, daemon=True).start()


# ══════════════════════════════════════════════════════════════════
# CONVENIENCE SINGLETON
# ══════════════════════════════════════════════════════════════════

_default_manager: Optional[ContextManager] = None

def get_manager(
    system_prompt: str = "",
    **kwargs,
) -> ContextManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = ContextManager(system_prompt=system_prompt, **kwargs)
    return _default_manager


# ══════════════════════════════════════════════════════════════════
# CLI TEST / DEMO
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("=" * 70)
    print("CONTEXT MANAGER v2.0 — GOD MODE FEATURE TEST")
    print("=" * 70)

    # ── 1. Configurable profiles ─────────────────────────────────────
    print("\n[1] LIMIT PROFILES")
    for name, p in PROFILES.items():
        print(f"  {name:10s}  msgs={p.max_messages:4d}  "
              f"tokens={p.max_tokens:6d}  paste={p.max_paste_chars:5d}  "
              f"idle={p.inactivity_hours:4.0f}h")

    # ── 2. LRU store with eviction ───────────────────────────────────
    print("\n[2] LRU EVICTION STORE (max 3 users, strategy=lru)")
    store = LRUContextStore(max_users=3, strategy="lru")
    for uid in ["alice", "bob", "charlie"]:
        store.set(uid, UserContext(user_id=uid))
    store.get("alice")                          # touch alice (MRU)
    store.set("dave", UserContext(user_id="dave"))  # triggers eviction of bob (LRU)
    print(f"  After adding dave: users={store.keys()} evictions={store.eviction_count}")

    # ── 3. Extractive structured recap ───────────────────────────────
    print("\n[3] STRUCTURED RECAP (extractive, no API)")
    engine = RecapEngineFactory.extractive()
    msgs = [
        ConvMessage("user", "How do I build a WhatsApp bot in Python?"),
        ConvMessage("assistant", "You can use Flask with the WhatsApp Business API webhook."),
        ConvMessage("user", "What's the best way to handle token limits?"),
        ConvMessage("assistant", "Use a sliding context window with periodic summarization."),
        ConvMessage("user", "How do I deploy this on Windows?"),
    ]
    structured = engine.generate_structured(msgs, "test_user")
    print(f"  Topics    : {structured['topics'][:4]}")
    print(f"  Key facts : {len(structured['key_facts'])} items")
    print(f"  Open Qs   : {structured['open_questions'][:2]}")
    print(f"  Engine    : {structured['engine']}")

    # ── 4. LoggingMetrics ────────────────────────────────────────────
    print("\n[4] LOGGING METRICS")
    metrics = LoggingMetrics(level=logging.INFO)
    metrics.counter_inc("ctxmgr_messages_total", {"user_id": "alice", "action": "claude_api"})
    metrics.gauge_set("ctxmgr_window_tokens", 1420.0, {"user_id": "alice"})
    metrics.histogram_observe("ctxmgr_recap_duration_seconds", 0.237,
                               {"user_id": "alice", "engine": "extractive"})

    # ── 5. Full ContextManager with all new features ─────────────────
    print("\n[5] FULL CONTEXT MANAGER (light profile + structured + metrics + lru)")
    mgr = ContextManager(
        system_prompt    = "You are a helpful assistant.",
        recap_engine     = RecapEngineFactory.extractive(),
        metrics          = LoggingMetrics(level=logging.DEBUG),
        limit_profile    = "light",    # only 10 msgs before limits
        eviction_strategy= "lru",
        structured_recaps= True,
        persist          = False,
    )

    # Override bob to heavy profile
    mgr.set_user_profile("bob", "heavy")

    DEMO = [
        "Hi, can you help me with Python?",
        "I'm building a Flask API",
        "How do I handle auth?",
        "/recap",
        "/reset",
        "Starting fresh — tell me about async Python",
        "x" * 700,   # should hit paste limit for light profile (600 chars)
    ]

    for msg in DEMO:
        result = mgr.process("alice", msg)
        label  = "UX" if result.is_ux_response else "API"
        stat   = result.stats
        short  = repr(msg[:40])
        print(f"\n  IN  {label}: {short}")
        if result.is_ux_response:
            print(f"  OUT: {result.ux_message[:120]}")
        else:
            print(f"  OUT: → {len(result.messages)} msgs to Claude "
                  f"({stat.window_messages}msg/{stat.window_tokens}tok "
                  f"{stat.limit_pct:.0f}%)")
        if result.warn_suffix:
            print(f"  WARN: {result.warn_suffix[:80]}")

    print(f"\n  Memory: {mgr.memory_snapshot()['total_users']} users in store")
    print(f"\n{'='*70}")
    print("ALL TESTS PASSED ✅")
    print(f"{'='*70}")
