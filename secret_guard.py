#!/usr/bin/env python3
"""Secret Guard — scans and redacts secrets before they are written to Qdrant.

Agents (opencode, Claude Code, Hermes) may store content containing API keys,
passwords, or tokens (the `store` operation in qdrant-agent-memory-tool.py).
This module is a protective layer: before text reaches Qdrant, it is scanned
against secret patterns, and any found secrets are replaced with [REDACTED]
and logged to data/secret_guard.log.
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

# Static patterns (no magic numbers — python.md rule)
_PLACEHOLDER_MARKERS = ("paste_", "your_", "twoj_", "xxx", "example", "test", "changeme", "klucz", "przyklad", "placeholder")

# Blacklist of known leaked values.
# NOTE: examples in the repository are dummies. Add your real values locally
# (e.g. tokens that leaked into session history) — do not commit them.
_BLACKLIST = (
    "twoj-wyciekly-sekret-1",
    "twoj-wyciekly-sekret-2",
)

# Secret patterns — each is a (name, regex) tuple
_SECRET_PATTERNS = (
    ("api_key_sk", re.compile(r"sk-(?:or|proj|ant)-[A-Za-z0-9_-]{16,}")),
    ("api_key_short", re.compile(r"\bsk-[A-Za-z0-9]{16,}")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}")),
    ("env_api_key", re.compile(r"\b(?:DEEPSEEK|OPENROUTER|GROQ|ANTHROPIC|OPENAI|MAILERLITE|QDRANT|LANGFUSE|CEREBRAS)_(?:API_)?KEY\s*=\s*\S+", re.IGNORECASE)),
    ("env_token", re.compile(r"\b(?:SERVICE|CEREBRAS|LANGFUSE|GH|GITHUB|GITLAB)_TOKEN\s*=\s*\S+", re.IGNORECASE)),
    ("password", re.compile(r"(?:hasl[oa]|password|passwd|secret|pwd|token)[=: ]+([A-Za-z0-9!@#%&*_\-]{8,})", re.IGNORECASE)),
    ("hash_argon", re.compile(r"\$argon2(i|id)?\$[^\s]{20,}")),
    ("hash_bcrypt", re.compile(r"\$2[aby]\$[^\s]{20,}")),
)

# Default log path — next to the script (data/secret_guard.log). Overridden by init.
_LOG_PATH = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "secret_guard.log"

_REDACTED = "[REDACTED]"


def _looks_placeholder(value: str) -> bool:
    """True if the value looks like a placeholder/example (not a real secret)."""
    low = (value or "").lower()
    return any(m in low for m in _PLACEHOLDER_MARKERS)


def _init_log(log_path: Path) -> None:
    """Set the global log path (lets tests point at a temp file)."""
    global _LOG_PATH
    _LOG_PATH = log_path


def scan(text: str) -> list[dict]:
    """Scan text and return a list of detected secrets.

    Each result: {"pattern": str, "start": int, "end": int, "match": str}
    Placeholder/example values are skipped (no false positives).
    """
    if not text:
        return []
    hits: list[dict] = []
    for name, rx in _SECRET_PATTERNS:
        for m in rx.finditer(text):
            matched = m.group(0)
            # For the password pattern only keep the value after the separator
            if name == "password" and len(m.groups()) >= 1:
                value = m.group(1)
                matched_value = value
            else:
                matched_value = matched
            if _looks_placeholder(matched_value):
                continue
            hits.append({"pattern": name, "start": m.start(), "end": m.end(), "match": matched})
    # Blacklist — also catches known leaks (even without context)
    for leaked in _BLACKLIST:
        pos = 0
        while True:
            idx = text.find(leaked, pos)
            if idx == -1:
                break
            hits.append({"pattern": "blacklist", "start": idx, "end": idx + len(leaked), "match": leaked})
            pos = idx + len(leaked)
    # Sort by position (so scrub works from the end)
    hits.sort(key=lambda h: h["start"])
    # Dedupe: hits fully contained in another hit are redundant
    # (e.g. env_api_key matches the whole "KEY=sk-...", api_key_short only the inner sk-).
    # Keep only those that do not start inside an already-kept hit.
    deduped: list[dict] = []
    for h in hits:
        if deduped and h["start"] < deduped[-1]["end"]:
            continue
        deduped.append(h)
    return deduped


def scrub(text: str, source: str = "") -> str:
    """Redact secrets in text to [REDACTED] and log the event.

    Returns the redacted text. If nothing was found — returns the original.
    """
    hits = scan(text)
    if not hits:
        return text

    redacted = text
    # Replace from the end so positions of later hits do not shift
    for h in reversed(hits):
        redacted = redacted[: h["start"]] + _REDACTED + redacted[h["end"] :]

    _log_scrub(source=source, count=len(hits), text=redacted)
    return redacted


def _log_scrub(source: str, count: int, text: str) -> None:
    """Write a redaction event to the log (without the secret itself)."""
    # Context: 60 chars around the first [REDACTED] — shows what was redacted
    # without revealing the secret.
    ctx = ""
    idx = text.find(_REDACTED)
    if idx != -1:
        ctx = text[max(0, idx - 60) : idx + 80]
    entry = (
        f"[{datetime.now(timezone.utc).isoformat()}] source={source or '?'} "
        f"secrets={count} context={ctx!r}"
    )
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


if __name__ == "__main__":
    import sys

    # CLI mode: scrub <text> [source]
    sample = sys.argv[1] if len(sys.argv) > 1 else "API key: sk-or-v1-abcdefghijklmnopqrstuvwxyz123456"
    src = sys.argv[2] if len(sys.argv) > 2 else "cli"
    print(scrub(sample, src))
