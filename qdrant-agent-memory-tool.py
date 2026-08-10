#!/usr/bin/env python3
"""CLI tool for Qdrant: search, show, edit, dedupe, list, stats, backup"""

import os
import sys
import re
import json
import time
import uuid
import hashlib
from datetime import datetime
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, Filter, FieldCondition, MatchValue, VectorParams

client = QdrantClient(
    url=os.getenv("QDRANT_URL"), api_key=os.getenv("QDRANT_API_KEY"), timeout=60
)
COLLECTION = os.getenv("COLLECTION_NAME")

BACKUP_DIR = os.path.join(os.path.dirname(__file__), "backups")

_DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")


# ─── Helpers ───────────────────────────────────────────────────────────
def _time_features(ts):
    """Time features (8 dimensions) — for collections with time embeddings (dim 392)."""
    import math

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


def _embed(text, with_time=True):
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    vec = list(model.embed([text]))[0].tolist()
    if with_time and COLLECTION.endswith("-v2"):
        vec += _time_features(time.time())
    return vec


def _get_point(pid):
    return client.retrieve(
        collection_name=COLLECTION, ids=[pid], with_payload=True, with_vectors=True
    )[0]


def _normalize_id(x):
    try:
        return int(x)
    except ValueError:
        return x


def _fmt_ts(ts):
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _extract_date(text):
    """Latest date in the text (regex) or None."""
    if not text:
        return None
    dates = _DATE_RE.findall(text)
    if not dates:
        return None
    return max(dates)


def _point_date(p):
    """Point date from ts_epoch (fallback: date from the text)."""
    ts = p.payload.get("ts_epoch")
    if ts:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    d = _extract_date(p.payload.get("text", ""))
    return d


def _point_ts(p):
    """Point unix timestamp (for time-decay) — from ts_epoch, fallback date from text."""
    if p.payload.get("ts_epoch"):
        return int(p.payload["ts_epoch"])
    d = _extract_date(p.payload.get("text", ""))
    if not d:
        return None
    try:
        return int(datetime.strptime(d, "%Y-%m-%d").timestamp())
    except Exception:
        return None


def _decay(age_days, lmbda=0.01):
    """Time-decay: fresher points get more weight."""
    return 1 / (1 + lmbda * age_days)


def _backup(points, op):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(
        BACKUP_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}-{op}.json"
    )
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            [{"id": str(p.id), "payload": p.payload} for p in points],
            f,
            ensure_ascii=False,
            indent=1,
        )
    print(f"  Backup: {path}")
    return path


def _scroll_all(limit=None):
    all_points = []
    offset = None
    while True:
        page, offset = client.scroll(
            collection_name=COLLECTION,
            limit=min(limit or 1000, 100),
            offset=offset,
            with_payload=True,
        )
        all_points.extend(page)
        if offset is None:
            break
    return all_points


# ─── Preview / audit ───────────────────────────────────────────────────
def show(pid):
    p = _get_point(_normalize_id(pid))
    print(f"ID: {p.id} ({type(p.id).__name__})")
    for k, v in p.payload.items():
        val = str(v)[:200]
        print(f"  {k}: {val}")
    vec = p.vector
    if hasattr(vec, "tolist"):
        vec = vec.tolist()
    print(f"  vector dim: {len(vec)}")


def stats():
    points = _scroll_all()
    by_source = defaultdict(lambda: {"count": 0, "ts": 0, "no_ts": 0})
    for p in points:
        src = p.payload.get("source", "?")
        by_source[src]["count"] += 1
        if p.payload.get("ts_epoch"):
            by_source[src]["ts"] += 1
        else:
            by_source[src]["no_ts"] += 1
    total = len(points)
    print(f"Total points: {total}")
    print(f"{'source':22s} {'count':>5s} {'ts_epoch':>9s} {'no_ts':>8s}")
    for src in sorted(by_source):
        s = by_source[src]
        print(f"{src:22s} {s['count']:5d} {s['ts']:9d} {s['no_ts']:8d}")


def list_source(source, limit=50):
    points = [p for p in _scroll_all() if p.payload.get("source") == source]
    print(f"Source '{source}': {len(points)} points")
    for p in points[:limit]:
        print(
            f"  ID={p.id}  date={_point_date(p) or '-':12s} {p.payload.get('text', '')[:80]}"
        )
    if len(points) > limit:
        print(f"  ... (+{len(points) - limit} more, use --limit)")


def find_by_file(path):
    points = [p for p in _scroll_all() if p.payload.get("file_path") == path]
    print(f"File '{path}': {len(points)} points")
    by_date = defaultdict(list)
    for p in points:
        d = _point_date(p) or "no-date"
        by_date[d].append(p)
    for d in sorted(by_date, reverse=True):
        for p in by_date[d]:
            print(f"  [{d}] ID={p.id}  {p.payload.get('text', '')[:80]}")


# ─── Editing ───────────────────────────────────────────────────────────
def edit(pid, new_text=None):
    pid = _normalize_id(pid)
    p = _get_point(pid)
    print("Current point:")
    print(f"  text: {p.payload.get('text', '')[:120]}")
    print(f"  ts_epoch: {_fmt_ts(p.payload.get('ts_epoch'))}")
    if new_text is None:
        new_text = input("  New text (Enter = no change): ").strip()
        if not new_text:
            print("  Cancelled")
            return
    vec = _embed(new_text)
    payload = dict(p.payload)
    payload["text"] = new_text
    if COLLECTION.endswith("-v2"):
        payload["ts_epoch"] = int(time.time())
    from qdrant_client.models import PointStruct

    client.upsert(
        collection_name=COLLECTION,
        points=[PointStruct(id=pid, vector=vec, payload=payload)],
    )
    print(f"  Updated ID={pid} (text + vector + ts_epoch)")


def edit_payload(pid, kv):
    pid = _normalize_id(pid)
    p = _get_point(pid)
    updates = {}
    for pair in kv:
        if "=" not in pair:
            print(f"  Skipping '{pair}' — key=value required")
            continue
        k, v = pair.split("=", 1)
        updates[k.strip()] = v.strip()
    print(f"Updating payload ID={pid}: {updates}")
    client.set_payload(collection_name=COLLECTION, payload=updates, points=[pid])
    print(f"  Payload updated: {updates}")


def update_vector(pid):
    pid = _normalize_id(pid)
    p = _get_point(pid)
    text = p.payload.get("text", "")
    if not text:
        print("  No text in point — cannot recompute vector")
        return
    vec = _embed(text)
    from qdrant_client.models import PointVectors

    client.update_vectors(
        collection_name=COLLECTION,
        points=[PointVectors(id=pid, vector=vec)],
    )
    print(f"  Vector recomputed for ID={pid} (dim={len(vec)})")


def reindex_source(source):
    points = [p for p in _scroll_all() if p.payload.get("source") == source]
    if not points:
        print(f"  No points for source='{source}'")
        return
    print(f"Recomputing vectors: {len(points)} points source='{source}'")
    _backup(points, "reindex")
    from qdrant_client.models import PointStruct

    texts = [p.payload.get("text", "") for p in points]
    vecs = [_embed(t, with_time=False) for t in texts]
    upserts = []
    now = int(time.time())
    for p, v in zip(points, vecs):
        payload = dict(p.payload)
        if COLLECTION.endswith("-v2"):
            payload["ts_epoch"] = now
            v = v + _time_features(now)
        upserts.append(PointStruct(id=p.id, vector=v, payload=payload))
    client.upsert(collection_name=COLLECTION, points=upserts)
    print(f"  Recomputed and overwritten {len(upserts)} points")


# ─── Duplicates ────────────────────────────────────────────────────────
def _find_dup_groups():
    points = _scroll_all()
    by_hash = defaultdict(list)
    for p in points:
        h = hashlib.md5(p.payload.get("text", "").encode()).hexdigest()
        by_hash[h].append(p)
    return [g for g in by_hash.values() if len(g) > 1]


def find_dupes():
    groups = _find_dup_groups()
    if not groups:
        print("  No duplicates")
        return
    print(
        f"Found {len(groups)} duplicate groups ({sum(len(g) for g in groups)} points total):"
    )
    for g in groups:
        print(f"  Group ({len(g)} points):")
        for p in g:
            d = _point_date(p) or "no-date"
            print(
                f"    [{d}] ID={p.id} src={p.payload.get('source', '?')} {p.payload.get('text', '')[:60]}"
            )


def dedupe():
    groups = _find_dup_groups()
    if not groups:
        print("  No duplicates")
        return
    total_dup = sum(len(g) for g in groups)
    print(f"Found {len(groups)} groups / {total_dup} duplicate points")

    to_delete = []
    for g in groups:
        # Sort by ts_epoch (fallback: date from text); empty = "0000"
        def sort_key(p):
            d = _point_date(p)
            if not d:
                return "0000-00-00"
            # normalize YYYY-MM-DD
            m = _DATE_RE.search(d)
            return m.group(0) if m else "0000-00-00"

        sorted_g = sorted(g, key=sort_key, reverse=True)
        keep = sorted_g[0]
        dupes = sorted_g[1:]
        print(
            f"  Keeping [{_point_date(keep) or 'no-date'}] ID={keep.id} {keep.payload.get('text', '')[:60]}"
        )
        for p in dupes:
            print(
                f"    DELETING [{_point_date(p) or 'no-date'}] ID={p.id} {p.payload.get('text', '')[:60]}"
            )
        to_delete.extend(p.id for p in dupes)

    if not to_delete:
        print("  Nothing to delete")
        return
    # backup points that will be deleted
    points = _scroll_all()
    del_points = [p for p in points if p.id in set(to_delete)]
    _backup(del_points, "dedupe")
    client.delete(collection_name=COLLECTION, points_selector=to_delete)
    print(f"  Deleted {len(to_delete)} duplicates")


# ─── Deleting (existing) ───────────────────────────────────────────────
def search(text, limit=10, fresh=True, since=None, window_days=None, lmbda=0.01):
    vec = _embed(text)
    # Optional time filter — native DATETIME index
    from qdrant_client.models import Range as QdRange

    time_filter = None
    now_ts = time.time()
    gte = None
    if since:
        try:
            gte = int(datetime.strptime(since, "%Y-%m-%d").timestamp())
        except Exception:
            print(f"  Invalid --since: {since} (use YYYY-MM-DD)")
            return
    if window_days:
        gte = now_ts - window_days * 86400
    if gte is not None:
        time_filter = Filter(
            must=[FieldCondition(key="ts_epoch", range=QdRange(gte=gte))]
        )

    results = client.query_points(
        collection_name=COLLECTION,
        query=vec,
        limit=limit * 3,  # overshoot — trimmed after decay
        with_payload=True,
        query_filter=time_filter,
    ).points

    # Time-decay
    scored = []
    for r in results:
        ts = _point_ts(r)
        score = r.score
        if fresh and ts is not None:
            age_days = (now_ts - ts) / 86400
            score = r.score * _decay(age_days, lmbda)
        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    for score, r in scored[:limit]:
        ts = _point_ts(r)
        age = (now_ts - ts) / 86400 if ts else None
        age_s = f"{age:.0f}d" if age is not None else "-"
        print(
            f"  score={score:.3f} ({age_s}) ID={r.id}  src={r.payload.get('source', '?')}"
        )
        print(f"      text: {r.payload.get('text', '')[:120]}")
    return scored[:limit]


def delete_by_ids(ids):
    """Delete points by ID — ALWAYS with a backup (before deleting)."""
    parsed = [_normalize_id(x) for x in ids]
    points = [p for p in _scroll_all() if p.id in set(parsed)]
    if points:
        _backup(points, "delete-id")
    client.delete(collection_name=COLLECTION, points_selector=parsed)
    print(f"  Deleted {len(ids)} points (backup done)")


def delete_by_source(source):
    client.delete(
        collection_name=COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=source))]
        ),
    )
    print(f"  Deleted all entries for source={source}")


def delete_by_text_contains(text):
    text_lower = text.lower()
    all_points = _scroll_all()
    matches = [
        p for p in all_points if text_lower in (p.payload.get("text", "") or "").lower()
    ]
    if not matches:
        print("  No matching entries")
        return
    ids = [p.id for p in matches]
    print(f"  Found {len(ids)} matching entries:")
    for p in matches:
        print(
            f"    ID={p.id}  source={p.payload.get('source', '?')}  text={p.payload.get('text', '')[:80]}"
        )
    _backup(matches, "delete-text")
    client.delete(collection_name=COLLECTION, points_selector=ids)
    print(f"  Deleted {len(ids)} points (backup done)")


def delete_fragment(
    fragment: str | None = None,
    regex: str | None = None,
    source: str | None = None,
    dry_run: bool = True,
) -> None:
    """Delete points matching a fragment (substring) and/or a regex pattern.

    Match modes (combined):
      - fragment: case-insensitive substring in payload['text']
      - regex:    re.search on payload['text'] (compiled with re.IGNORECASE)
    Optional --source filter narrows to a single source.
    Backup is ALWAYS taken before the actual deletion.
    """
    if not fragment and not regex:
        print("  Provide a fragment and/or --regex")
        return

    frag_lower = (fragment or "").lower()
    pattern = None
    if regex:
        try:
            pattern = re.compile(regex, re.IGNORECASE)
        except re.error as e:
            print(f"  Invalid regex pattern: {e}")
            return

    def matches(p):
        if source and p.payload.get("source") != source:
            return False
        text = p.payload.get("text", "") or ""
        hit_frag = frag_lower and frag_lower in text.lower()
        hit_regex = pattern is not None and pattern.search(text) is not None
        return hit_frag or hit_regex

    all_points = _scroll_all()
    matches_by_id = {}
    for p in all_points:
        if matches(p):
            matches_by_id[p.id] = p

    by_frag = []
    by_regex = []
    for p in matches_by_id.values():
        text = p.payload.get("text", "") or ""
        if frag_lower and frag_lower in text.lower():
            by_frag.append(p)
        if pattern is not None and pattern.search(text) is not None:
            by_regex.append(p)

    if not matches_by_id:
        print("  No matching entries")
        return

    print(
        f"  Matches: fragment={len(by_frag)}, regex={len(by_regex)}, "
        f"total={len(matches_by_id)}"
    )
    if by_frag:
        print("  ── by fragment ──")
        for p in by_frag:
            print(
                f"    ID={p.id}  src={p.payload.get('source', '?')}  {p.payload.get('text', '')[:80]}"
            )
    if by_regex:
        print("  ── by regex ──")
        for p in by_regex:
            print(
                f"    ID={p.id}  src={p.payload.get('source', '?')}  {p.payload.get('text', '')[:80]}"
            )

    if dry_run:
        print("  [dry-run] Nothing deleted. Add --yes to execute.")
        return

    _backup(list(matches_by_id.values()), "delete-fragment")
    client.delete(
        collection_name=COLLECTION, points_selector=list(matches_by_id.keys())
    )
    print(f"  Deleted {len(matches_by_id)} points (backup done)")


def list_sources():
    all_points = _scroll_all()
    sources = set()
    for p in all_points:
        s = p.payload.get("source")
        if s:
            sources.add(s)
    for s in sorted(sources):
        print(f"  {s}")


def backup(path=None):
    """Export the whole collection to a JSON file (safe copy before cleanup)."""
    points = _scroll_all()
    if path is None:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        path = os.path.join(
            BACKUP_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}-backup.json"
        )
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            [{"id": str(p.id), "payload": p.payload} for p in points],
            f,
            ensure_ascii=False,
            indent=1,
        )
    print(f"Backup: {path} ({len(points)} points)")
    return path


def setup(name=None):
    """Create a Qdrant collection (dim 392 for -v2 names / 384 otherwise)."""
    n = name or COLLECTION
    if not n:
        print("  Provide a collection name or set COLLECTION_NAME in .env")
        return
    if client.collection_exists(n):
        print(f"  Collection '{n}' already exists")
        return
    dim = 392 if n.endswith("-v2") else 384
    client.create_collection(
        collection_name=n,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )
    print(f"  Collection '{n}' created ({dim}-dim, COSINE)")


def store(text, source="manual"):
    """Store memory in Qdrant — embed text (+time features for v2) and send with payload."""
    # Secret Guard — redact secrets before anything reaches Qdrant
    from secret_guard import scrub
    from qdrant_client.models import PointStruct
    text = scrub(text, source)
    vec = _embed(text)
    now = int(time.time())
    payload = {"text": text, "source": source}
    if COLLECTION.endswith("-v2"):
        payload["ts_epoch"] = now
    client.upsert(
        collection_name=COLLECTION,
        points=[
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload=payload,
            )
        ],
    )
    print(f"Stored: {text[:100]}...")


# ─── Help / main ───────────────────────────────────────────────────────
def help():
    print("Usage:")
    print(
        "  qdrant-agent-memory-tool.py search <text> [limit]               — semantic search"
    )
    print(
        "        [--all] [--since YYYY-MM-DD] [--window 30d]   — time: decay / since / window"
    )
    print('  qdrant-agent-memory-tool.py store <text> [source]               — store memory')
    print('  qdrant-agent-memory-tool.py setup [name]                        — create collection (dim 392 for -v2)')
    print("  qdrant-agent-memory-tool.py show <id>                           — view point")
    print(
        "  qdrant-agent-memory-tool.py stats                               — stats per source"
    )
    print("  qdrant-agent-memory-tool.py list-source <source> [limit]        — entries of a source")
    print(
        "  qdrant-agent-memory-tool.py find-by-file <path>                 — points of a file (with dates)"
    )
    print('  qdrant-agent-memory-tool.py edit <id> [--text "new"]           — edit text + vector')
    print("  qdrant-agent-memory-tool.py edit-payload <id> key=val [k=v...]  — metadata only")
    print("  qdrant-agent-memory-tool.py update-vector <id>                  — recompute vector")
    print(
        "  qdrant-agent-memory-tool.py reindex-source <source>             — recompute source vectors (backup)"
    )
    print(
        "  qdrant-agent-memory-tool.py find-dupes                          — show duplicates with dates"
    )
    print(
        "  qdrant-agent-memory-tool.py dedupe                              — remove duplicates (newest kept)"
    )
    print("  qdrant-agent-memory-tool.py delete-id <id> [id...]              — delete by ID (backup)")
    print("  qdrant-agent-memory-tool.py delete-source <source>              — delete whole source")
    print(
        "  qdrant-agent-memory-tool.py delete-text <text>                  — delete by fragment (confirm)"
    )
    print(
        "  qdrant-agent-memory-tool.py delete-fragment <text> [--regex PAT] [--source SRC]"
    )
    print(
        "        [--dry-run] [--yes]                         — delete by fragment and/or regex"
    )
    print("  qdrant-agent-memory-tool.py sources                             — list sources")
    print("  qdrant-agent-memory-tool.py backup [file.json]                  — export whole collection (JSON)")
    print("")
    print("Example: qdrant-agent-memory-tool.py dedupe")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        help()
        sys.exit(0)

    cmd = argv[0]
    args = argv[1:]

    if cmd == "search":
        text = args[0]
        limit = 10
        fresh = True
        since = None
        window_days = None
        if len(args) > 1 and args[1].isdigit():
            limit = int(args[1])
        if "--all" in args:
            fresh = False
        if "--since" in args:
            since = args[args.index("--since") + 1]
        if "--window" in args:
            window_days = int(args[args.index("--window") + 1].rstrip("d"))
        search(text, limit, fresh=fresh, since=since, window_days=window_days)
    elif cmd == "store":
        text = args[0]
        source = args[1] if len(args) > 1 else "manual"
        store(text, source)
    elif cmd == "setup":
        setup(args[0] if args else None)
    elif cmd == "backup":
        path = args[0] if args else None
        backup(path)
    elif cmd == "show":
        show(args[0])
    elif cmd == "stats":
        stats()
    elif cmd == "list-source":
        limit = 50
        if args and args[-1].isdigit():
            limit = int(args[-1])
            args = args[:-1]
        list_source(args[0], limit)
    elif cmd == "find-by-file":
        find_by_file(" ".join(args))
    elif cmd == "edit":
        new_text = None
        id_arg = args[0]
        if "--text" in args:
            i = args.index("--text")
            new_text = args[i + 1]
        edit(id_arg, new_text)
    elif cmd == "edit-payload":
        edit_payload(args[0], args[1:])
    elif cmd == "update-vector":
        update_vector(args[0])
    elif cmd == "reindex-source":
        reindex_source(args[0])
    elif cmd == "find-dupes":
        find_dupes()
    elif cmd == "dedupe":
        dedupe()
    elif cmd == "delete-id":
        delete_by_ids(args)
    elif cmd == "delete-source":
        delete_by_source(args[0])
    elif cmd == "delete-text":
        delete_by_text_contains(" ".join(args))
    elif cmd == "delete-fragment":
        fragment = None
        regex = None
        source = None
        dry_run = True
        positionals = []
        for a in args:
            if a == "--regex":
                regex = args[args.index("--regex") + 1]
            elif a == "--source":
                source = args[args.index("--source") + 1]
            elif a == "--yes":
                dry_run = False
            elif a == "--dry-run":
                dry_run = True
            else:
                positionals.append(a)
        fragment = positionals[0] if positionals else None
        delete_fragment(fragment, regex, source, dry_run)
    elif cmd == "sources":
        list_sources()
    else:
        help()
