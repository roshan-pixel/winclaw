import time
import json

class TokenTracker:
    """
    Counts tokens per message list and exposes threshold checks.
    Ref: gitee.com/free/claude-code main, TokenTracker.shouldAutoCompact() >93%.

    Token counting strategy (mixed Hindi/English/code):
      - ASCII text:      1 token per ~4 chars  (GPT standard)
      - Unicode (Devanagari / CJK / emoji):  each char ~1.5 tokens
      - JSON/code blobs: 1 token per ~3.5 chars (denser)
    The unified heuristic below samples the first 200 chars to decide which
    multiplier to apply for the entire string.
    """

    MODEL_LIMITS = {
        "gemini-2.5-flash": 1_048_576,
        "qwen-godmode":     32_768,
        "default":          32_768,
    }
    COMPACT_THRESHOLD = 0.93

    def __init__(self, model_name: str = "gemini-2.5-flash"):
        self.model_name = model_name
        self.max_tokens = self.MODEL_LIMITS.get(model_name, self.MODEL_LIMITS["default"])
        self.current_tokens = 0
        self.session_start_time = time.time()

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        sample = text[:200]
        non_ascii = sum(1 for c in sample if ord(c) > 127)
        non_ascii_ratio = non_ascii / max(len(sample), 1)
        byte_len = len(text.encode("utf-8"))
        if non_ascii_ratio > 0.1:
            return max(1, byte_len // 3)
        else:
            return max(1, byte_len // 4)

    def count_messages(self, messages: list) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += self.count_tokens(block.get("text", "") or json.dumps(block))
            elif isinstance(content, str):
                total += self.count_tokens(content)
        return total

    def update(self, messages: list) -> int:
        self.current_tokens = self.count_messages(messages)
        return self.current_tokens

    def should_auto_compact(self) -> bool:
        if self.max_tokens == 0:
            return False
        return (self.current_tokens / self.max_tokens) >= self.COMPACT_THRESHOLD

    def ratio(self) -> float:
        if self.max_tokens == 0:
            return 0.0
        return self.current_tokens / self.max_tokens

    def session_age_minutes(self) -> float:
        return (time.time() - self.session_start_time) / 60.0

    def status_level(self) -> str:
        r = self.ratio()
        if r >= self.COMPACT_THRESHOLD:
            return "CRITICAL"
        elif r >= 0.80:
            return "WARNING"
        elif r >= 0.60:
            return "ELEVATED"
        else:
            return "GREEN"

    def summary(self) -> dict:
        return {
            "model": self.model_name,
            "current_tokens": self.current_tokens,
            "max_tokens": self.max_tokens,
            "ratio": round(self.ratio(), 4),
            "level": self.status_level(),
            "session_age_min": round(self.session_age_minutes(), 1),
        }