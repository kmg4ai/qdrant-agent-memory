#!/usr/bin/env python3
"""CLI tool for Qdrant: search, show, edit, dedupe, list, stats, backup"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_id import detect_agent  # noqa: E402  (po sys.path, celowo)
from datetime_utils import l2norm  # noqa: E402  (po sys.path, celowo)
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

# Model embeddingu — JEDNO miejsce prawdy. Kolekcja jest w ~86% polska, a model
# wyłącznie angielski (all-MiniLM-L6-v2) dawał przy polskich zapytaniach wyniki
# śmieciowe. Wielojęzyczny zamiennik ma ten sam wymiar (384), więc NIE wymaga
# zmiany schematu kolekcji — tylko przeliczenia wektorów (reindex-all).
# UWAGA: ingest.py MUSI używać tej samej wartości. Dwa różne modele = zapytania
# i dokumenty w dwóch różnych przestrzeniach = wyszukiwanie przestaje działać.
EMBED_MODEL = os.getenv(
    "QDRANT_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

# ─── Reranker (drugi etap wyszukiwania) ────────────────────────────────
# Cross-encoder czyta zapytanie i dokument RAZEM, więc szereguje precyzyjniej
# niż sam cosinus. Zmierzone na 104 trudnych zapytaniach: produkcyjna ścieżka
# 24% -> 50% hit@5, MRR 0.163 -> 0.383, McNemar p < 0.001 (29 wygranych,
# 2 przegrane). Sufit recall@50 = 64%, więc bierze 78% tego, co osiągalne.
#
# Model jest ANGIELSKI (jina-reranker-v1-turbo-en) i to jest kompromis, nie
# optimum: wielojęzyczny (jina-reranker-v2-base-multilingual) ma RSS ~2,4 GB
# i na tym serwerze nie wstaje — earlyoom wysyła mu SIGTERM. Angielski działa
# na polskim korpusie lepiej niż bi-enkoder, co jest zmierzone, nie założone.
RERANK_ENABLED = os.getenv("QDRANT_RERANK", "1") != "0"
RERANK_MODEL = os.getenv("QDRANT_RERANK_MODEL", "jinaai/jina-reranker-v1-turbo-en")
# 50 kandydatów: recall@50 = 64% (przy 30 kandydatach ~60%, czyli mniej).
RERANK_CANDIDATES = int(os.getenv("QDRANT_RERANK_CANDIDATES", "50"))
# Reranker przyjmuje maks. ~512 tokenów, więc dłuższego tekstu nie przeczyta —
# a jego potokenizowanie i zaalokowanie kosztuje. Wpisy changelogu mają do 4798
# znaków, stąd obcięcie.
RERANK_MAX_CHARS = 2000
# batch 8, nie domyślne 64: przy 64 i długich tekstach proces zjadał ~1,4 GB
# więcej i wypadał poza próg earlyoom.
RERANK_BATCH = 8
# Poniżej tylu MB wolnego RAM nie wczytujemy rerankera — earlyoom zabija
# największy proces na maszynie, a to może być cudza praca.
RERANK_MIN_FREE_MB = 1200

_reranker = None
_reranker_failed = False

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

    model = TextEmbedding(model_name=EMBED_MODEL)
    vec = l2norm(list(model.embed([text]))[0].tolist())
    if with_time and COLLECTION.endswith("-v2"):
        vec += _time_features(time.time())
    return vec


def _embed_batch(texts, model):
    """Embed wielu tekstów JEDNĄ instancją modelu (każdy znormalizowany).

    `_embed` tworzy TextEmbedding przy KAŻDYM wywołaniu — dla reindeksu 8236
    punktów znaczyłoby to 8236 ładowań ONNX (godziny zamiast minut). Tutaj model
    powstaje raz i jest przekazywany.
    """
    return [l2norm(v.tolist()) for v in model.embed(texts)]


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

    # Kto zapisal — pole `agent` wypelnia detect_agent() przy kazdym store().
    # Wpisy sprzed 2026-09-11 nie maja go wcale; te z ingest.py oznaczylismy
    # wstecznie jako `ingest` (to fakt — dodal je skrypt, nie agent), a wpisy
    # z `source=session` zostaja jako `?`, bo autora nie da sie ustalic
    # i zgadywanie w metadanych jest gorsze niz brak wartosci.
    by_agent = defaultdict(int)
    for p in points:
        by_agent[p.payload.get("agent") or "?"] += 1
    print()
    print(f"{'agent':22s} {'count':>5s}")
    for a in sorted(by_agent, key=lambda k: (-by_agent[k], k)):
        print(f"{a:22s} {by_agent[a]:5d}")

    # `via` odpowiada na inne pytanie niz `agent`: nie KTO, a JAK. Rozdzielone,
    # bo wpis wciagniety przez ingest.py ma autora (kto odpalil skrypt)
    # i mechanizm (skrypt) — a zlepienie ich w jedno pole gubi autora.
    by_via = defaultdict(int)
    for p in points:
        by_via[p.payload.get("via") or "?"] += 1
    print()
    print(f"{'via':22s} {'count':>5s}")
    for v in sorted(by_via, key=lambda k: (-by_via[k], k)):
        print(f"{v:22s} {by_via[v]:5d}")


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


def reindex_source(source=None, batch_size=64):
    """Przelicza wektory źródła (albo CAŁEJ kolekcji, gdy source=None).

    CZAS TREŚCI JEST ZACHOWANY. Poprzednia wersja robiła `ts_epoch = now` oraz
    cechy czasu z `now`, co przy reindeksie kasowało całą historię: 7094
    datowanych wpisów changelogu zlewało się w jedną chwilę, a filtry
    `--since`/`--window` i zanik czasu w rankingu przestawały działać —
    nieodwracalnie, bo stara data żyła tylko w wektorze.

    Cechy czasu są funkcją DETERMINISTYCZNĄ od `ts_epoch`, więc liczymy je
    z zachowanego `ts_epoch` — wynik jest identyczny z zachowaniem surowych
    wymiarów, ale nie wymaga pobierania wektorów.

    Embedding idzie partiami przez JEDNĄ instancję modelu — `_embed` tworzy
    TextEmbedding przy każdym wywołaniu, więc pętla po 8236 tekstach oznaczałaby
    8236 ładowań ONNX (godziny zamiast minut).
    """
    points = [
        p for p in _scroll_all() if source is None or p.payload.get("source") == source
    ]
    if not points:
        print(f"  No points for source='{source}'")
        return
    print(f"Recomputing vectors: {len(points)} points, source={source or 'ALL'}")
    print(f"  model: {EMBED_MODEL}")
    _backup(points, "reindex")

    from fastembed import TextEmbedding
    from qdrant_client.models import PointStruct

    model = TextEmbedding(model_name=EMBED_MODEL)
    upserts = []
    stored = 0
    missing_ts = 0
    for bs in range(0, len(points), batch_size):
        batch = points[bs : bs + batch_size]
        texts = [p.payload.get("text", "") for p in batch]
        for p, emb in zip(batch, _embed_batch(texts, model)):
            payload = dict(p.payload)
            vec = emb
            if COLLECTION.endswith("-v2"):
                ts = payload.get("ts_epoch")
                if ts is None:
                    # Nie powinno się zdarzyć w -v2 (ingest zawsze ustawia), ale
                    # gdy się zdarzy, lepiej głośno policzyć niż cicho zgubić.
                    missing_ts += 1
                    ts = int(time.time())
                    payload["ts_epoch"] = int(ts)
                vec = vec + _time_features(int(ts))
            upserts.append(PointStruct(id=p.id, vector=vec, payload=payload))
        client.upsert(collection_name=COLLECTION, points=upserts)
        stored += len(upserts)
        upserts = []
        print(f"  {stored}/{len(points)}")
    print(f"  Recomputed and overwritten {stored} points")
    if missing_ts:
        print(f"  ⚠️  {missing_ts} punktów bez ts_epoch — ustawione na teraz")


# ─── Duplicates ────────────────────────────────────────────────────────
# Znacznik daty w nagłówku wpisu: „2026-09-22 22:04: treść".
_DUP_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?:\s*", re.S)
# Minimalna długość treści, by uznać ją za duplikat w trybie znormalizowanym.
# Próg jest istotny: krótkie fragmenty („check: 644 www-data, http 200")
# powtarzają się w RÓŻNYCH wpisach legalnie — to boilerplate, nie duplikaty.
DUP_MIN_CHARS = 60


def _dup_key(text, normalize=False):
    """Klucz grupowania duplikatów.

    Tryb surowy (dotychczasowy) bierze CAŁY tekst — i dlatego znajdował ZERO
    grup. Kopie w tej kolekcji różnią się znacznikiem daty w nagłówku
    („2026-06-23 14:45:" vs „2026-09-22 22:04:"), więc surowy tekst nigdy się
    nie zgadza. `find-dupes` raportowało „No duplicates" na korpusie, który ma
    2482 takie grupy.

    Tryb znormalizowany zdejmuje znacznik daty i normalizuje białe znaki.
    Zwraca None dla tekstów krótszych niż DUP_MIN_CHARS.
    """
    if normalize:
        t = re.sub(r"\s+", " ", _DUP_DATE_PREFIX.sub("", text.strip())).strip().lower()
        if len(t) < DUP_MIN_CHARS:
            return None
    else:
        t = text
    return hashlib.md5(t.encode()).hexdigest()


def _find_dup_groups(normalize=False):
    by_hash = defaultdict(list)
    for p in _scroll_all():
        k = _dup_key(p.payload.get("text", ""), normalize)
        if k is not None:
            by_hash[k].append(p)
    return [g for g in by_hash.values() if len(g) > 1]


def find_dupes():
    """Raportuje OBIE liczby — surową i po normalizacji daty.

    Pokazywanie tylko surowej było kłamstwem: 0 grup przy 2482 realnych.
    """
    raw = _find_dup_groups(normalize=False)
    norm = _find_dup_groups(normalize=True)
    n_raw = sum(len(g) - 1 for g in raw)
    n_norm = sum(len(g) - 1 for g in norm)
    print("  Tryb surowy (md5 całego tekstu) — na tym działa `dedupe` bez flagi:")
    print(f"    grup: {len(raw)}, nadmiarowych punktów: {n_raw}")
    print(f"  Tryb znormalizowany (bez znacznika daty, >= {DUP_MIN_CHARS} znaków):")
    print(f"    grup: {len(norm)}, nadmiarowych punktów: {n_norm}")
    if not norm:
        print("  Brak duplikatów w obu trybach")
        return
    print(f"\n  Grupy (tryb znormalizowany, pokazano do 15 z {len(norm)}):")
    for g in norm[:15]:
        print(f"  Group ({len(g)} points):")
        for p in g:
            d = _point_date(p) or "no-date"
            print(
                f"    [{d}] ID={p.id} src={p.payload.get('source', '?')} {p.payload.get('text', '')[:60]}"
            )
    if n_norm:
        print(
            f"\n  UWAGA: usunięcie ich przez `dedupe --normalize` skasuje {n_norm} "
            f"punktów.\n  Zmierzone: zysk +4 pp hit@5 przy p=0.219 (SZUM) — "
            "czyli nieudowodniony."
        )


def dedupe(normalize=False):
    """Usuwa duplikaty — domyślnie tryb SUROWY (zachowanie jak dotąd).

    Tryb znormalizowany (flaga --normalize) usuwa realne kopie różniące się
    znacznikiem daty — ale to kasuje ~2218 punktów (27% korpusu) przy zysku
    +4 pp hit@5 zmierzonym jako NIEstotny (p=0.219). Dlatego NIE jest
    domyślny: domyślne zachowanie nie może być nieodwracalne bez dowodu.
    """
    groups = _find_dup_groups(normalize=normalize)
    if not groups:
        print(f"  No duplicates (tryb {'znormalizowany' if normalize else 'surowy'})")
        return
    total_dup = sum(len(g) for g in groups)
    print(f"Found {len(groups)} groups / {total_dup} duplicate points")
    if normalize:
        print(
            f"  ⚠️  tryb znormalizowany: usuniętych zostanie {total_dup} punktów "
            "(kopie różniące się tylko datą w nagłówku)"
        )

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


# ─── Reranker helpers ──────────────────────────────────────────────────
def _mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return 10**6  # nie wiemy — nie blokuj z powodu braku odczytu
    return 10**6


def _get_reranker():
    """Reranker JEDEN RAZ NA PROCES.

    Ładowanie modelu trwa ~5–9 s — przy ładowaniu per zapytanie wyszukiwanie
    przez CLI robiłoby się kilkunastosekundowe zamiast kilkusekundowego.
    Dla serwera MCP (proces długożyjący) to jednorazowy koszt startu.
    """
    global _reranker, _reranker_failed
    if _reranker is not None or _reranker_failed:
        return _reranker
    avail = _mem_available_mb()
    if avail < RERANK_MIN_FREE_MB:
        _reranker_failed = True
        print(
            f"  ⚠️  reranker pominięty: {avail} MB wolnego RAM "
            f"< {RERANK_MIN_FREE_MB} MB (earlyoom zabija największy proces)"
        )
        return None
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        _reranker = TextCrossEncoder(model_name=RERANK_MODEL)
    except Exception as e:  # brak modelu / brak pamięci / zły cache
        _reranker_failed = True
        print(f"  ⚠️  reranker niedostępny ({type(e).__name__}: {e}) — ranking po cosinusie")
        return None
    return _reranker


def _minmax(xs):
    lo, hi = min(xs), max(xs)
    if hi - lo < 1e-9:
        return [0.5] * len(xs)
    return [(x - lo) / (hi - lo) for x in xs]


# ─── Deleting (existing) ───────────────────────────────────────────────
def search(text, limit=10, fresh=True, since=None, window_days=None, lmbda=0.01,
           rerank=None, source=None):
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
    # Filtry łączymy w JEDNĄ listę `must`. Wcześniej był tylko czas; filtr po
    # źródle jest potrzebny, bo gdy kolekcja jest zdominowana przez jedno źródło
    # (u nas: changelog to ~85% punktów), mniejszość — np. reguły — bywa
    # wypychana poza top-N i praktycznie nieosiągalna zwykłym zapytaniem.
    must = []
    if gte is not None:
        must.append(FieldCondition(key="ts_epoch", range=QdRange(gte=gte)))
    if source:
        # `source` JEST zindeksowane w tej kolekcji, więc MatchValue działa.
        # (Dla `file_path` indeksu nie ma i Qdrant taki filtr odrzuca.)
        must.append(FieldCondition(key="source", match=MatchValue(value=source)))
    time_filter = Filter(must=must) if must else None

    use_rerank = RERANK_ENABLED if rerank is None else rerank
    # Przy reranku pobieramy 50 kandydatów (recall@50 = 64%), bez niego
    # zostaje dawne potrójne przewężenie.
    n_fetch = max(limit * 3, RERANK_CANDIDATES) if use_rerank else limit * 3

    results = client.query_points(
        collection_name=COLLECTION,
        query=vec,
        limit=n_fetch,
        with_payload=True,
        query_filter=time_filter,
    ).points

    # Zanik czasu trzymamy jako OSOBNY czynnik, a nie wmnożony w score.
    # Powód: reranker PODMIENIA bazowy score, więc gdyby decay był już
    # w środku, nie dałoby się go zastosować po reranku — a wtedy zanik czasu
    # po cichu przestałby działać. Tak mnożymy go na końcu, po normalizacji.
    cand = []
    for r in results:
        ts = _point_ts(r)
        d = 1.0
        if fresh and ts is not None:
            d = _decay((now_ts - ts) / 86400, lmbda)
        cand.append((r, d))

    scored = None
    if use_rerank and len(cand) > 1:
        model = _get_reranker()
        if model is not None:
            try:
                texts_c = [r.payload.get("text", "")[:RERANK_MAX_CHARS] for r, _ in cand]
                raw = list(model.rerank(text, texts_c, batch_size=RERANK_BATCH))
                # Normalizacja min-max PRZED mnożeniem przez zanik czasu.
                # Wyniki cross-encodera to logity o dużej skali (często ujemne),
                # więc bez normalizacji pomnożenie przez ~0.77 nic nie zmienia
                # i zanik czasu przestaje cokolwiek znaczyć.
                norm = _minmax(raw)
                # Trzeci element to PODPIS dla wydruku. Po normalizacji min-max
                # najwyższy wynik to ZAWSZE ~1.0 × decay, więc sama ta liczba
                # nie mówi nic o trafności — zapytanie o średniki w Pythonie
                # pokazywało 0.994 dla reguły o `git push`. Dlatego drukujemy
                # obok surowy cosinus, który da się interpretować.
                scored = [
                    (
                        norm[i] * cand[i][1],
                        cand[i][0],
                        f"rerank={norm[i]:.2f} cos={cand[i][0].score:.3f}",
                    )
                    for i in range(len(cand))
                ]
            except Exception as e:
                print(f"  ⚠️  rerank nieudany ({type(e).__name__}: {e}) — ranking po cosinusie")

    if scored is None:
        scored = [(r.score * d, r, f"cos={r.score:.3f}") for r, d in cand]

    scored.sort(key=lambda x: x[0], reverse=True)
    for score, r, detail in scored[:limit]:
        ts = _point_ts(r)
        age = (now_ts - ts) / 86400 if ts else None
        age_s = f"{age:.0f}d" if age is not None else "-"
        print(
            f"  rank={score:.3f} [{detail}] ({age_s}) ID={r.id}"
            f"  src={r.payload.get('source', '?')}  by={r.payload.get('agent', '?')}"
        )
        print(f"      text: {r.payload.get('text', '')[:120]}")
    # Zwracamy DWUELEMENTOWE krotki jak dotąd — `rank` to pozycja w rankingu,
    # nie podobieństwo. Zmiana arności psułaby istniejących wywołujących.
    return [(s, r) for s, r, _ in scored[:limit]]


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
                f"    ID={p.id}  src={p.payload.get('source', '?')}"
                f"  by={p.payload.get('agent', '?')}  {p.payload.get('text', '')[:80]}"
            )
    if by_regex:
        print("  ── by regex ──")
        for p in by_regex:
            print(
                f"    ID={p.id}  src={p.payload.get('source', '?')}"
                f"  by={p.payload.get('agent', '?')}  {p.payload.get('text', '')[:80]}"
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
    payload = {"text": text, "source": source,
               "agent": detect_agent(), "via": "store"}
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
        "  qdrant-agent-memory-tool.py search <text> [limit] [--all] [--since D] [--window Nd] [--no-rerank] [--source SRC]"
    )
    print(
        "        — semantic search; domyślnie z rerankerem (+26 pp hit@5, ~5 s). --no-rerank = sam cosinus"
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
        "  qdrant-agent-memory-tool.py reindex-all                         — recompute ALL vectors (backup; po zmianie modelu)"
    )
    print(
        "  qdrant-agent-memory-tool.py find-dupes                          — duplicates: tryb surowy ORAZ po normalizacji daty"
    )
    print(
        "  qdrant-agent-memory-tool.py dedupe [--normalize]                — usuń duplikaty (najnowsza zostaje); --normalize = też kopie różniące się datą (kasuje ~27% korpusu)"
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
        # --no-rerank: ranking samym cosinusem. Reranker daje +26 pp trafień,
        # ale kosztuje ~5 s i jest angielski — bywa potrzebny wariant szybki.
        rerank = False if "--no-rerank" in args else None
        # --source <nazwa>: zawęź do jednego źródła. Bez tego mniejszościowe
        # źródła (np. reguły) toną w zdominowanym korpusie.
        source = args[args.index("--source") + 1] if "--source" in args else None
        search(
            text, limit, fresh=fresh, since=since, window_days=window_days,
            rerank=rerank, source=source,
        )
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
    elif cmd == "reindex-all":
        reindex_source(None)
    elif cmd == "find-dupes":
        find_dupes()
    elif cmd == "dedupe":
        # --normalize: usuń także kopie różniące się tylko datą w nagłówku.
        # NIE domyślnie — kasuje ~27% korpusu przy zysku zmierzonym jako szum.
        dedupe(normalize="--normalize" in args)
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
