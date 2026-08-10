#!/usr/bin/env python3
"""Shared time utilities for Qdrant v2 (ingest + fix_created_at + tool).

Time model:
- ts_epoch = timestamp of the content date (changelog header with time / date from text / mtime)
- vector time features are based on ts_epoch — consistent
- (created_at / date were removed — ts_epoch is the only time field)
"""

import math
import os
import re
from datetime import datetime

FALLBACK_TS = int(datetime(2026, 5, 1).timestamp())
_DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")

# Old-path mapping (moved projects) → current locations.
# Add your own entries: {"old": "new"} — files whose path changed
# will resolve to their new ID (content_ts/resolve_path).
PATH_MAP = {}

SPECIAL_PATHS = {}


def time_features(ts):
    """Time features (8 dimensions) for v2 collections (dim 392)."""
    dt = datetime.fromtimestamp(ts)
    doy = dt.timetuple().tm_yday
    scale = 0.3
    return [
        (dt.year / 2100) * scale,
        (dt.month / 12) * scale,
        (dt.day / 31) * scale,
        (dt.hour / 24) * scale,
        math.sin(2 * math.pi * doy / 365) * scale,
        math.cos(2 * math.pi * doy / 365) * scale,
        math.sin(2 * math.pi * dt.hour / 24) * scale,
        math.cos(2 * math.pi * dt.hour / 24) * scale,
    ]


def resolve_path(fp):
    """Map an old path to its new location (if the project was moved)."""
    if not fp:
        return None
    if fp in SPECIAL_PATHS:
        return SPECIAL_PATHS[fp]
    for old, new in PATH_MAP.items():
        if fp == old or fp.startswith(old + "/"):
            return fp.replace(old, new, 1)
    return fp


def content_ts(payload):
    """Content timestamp: payload date > date in text > file mtime > fallback."""
    d = payload.get("date")
    if d:
        m_full = re.search(r"20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}", d)
        if m_full:
            try:
                return int(
                    datetime.strptime(m_full.group(0), "%Y-%m-%d %H:%M").timestamp()
                )
            except Exception:
                pass
        m = _DATE_RE.search(d)
        if m:
            try:
                return int(datetime.strptime(m.group(0), "%Y-%m-%d").timestamp())
            except Exception:
                pass
    text = payload.get("text", "")
    if text:
        dates = _DATE_RE.findall(text)
        if dates:
            try:
                return int(datetime.strptime(max(dates), "%Y-%m-%d").timestamp())
            except Exception:
                pass
    fp = payload.get("file_path", "")
    if fp:
        fp = resolve_path(fp)
        if fp and os.path.exists(fp):
            try:
                return int(os.path.getmtime(fp))
            except Exception:
                pass
    return FALLBACK_TS
