#!/usr/bin/env python3
"""Czy deduplikacja korpusu poprawia wyszukiwanie? — offline, bez zmian w Qdrant.

Użycie:
    .venv/bin/python validate_dedupe.py

Hipoteza: ~500 nadmiarowych kopii tekstu (12% korpusu) zajmuje miejsca
w wynikach i wypycha inne dokumenty. Jeśli tak, dedupe da więcej niż reranker,
a kosztuje mniej — nie trzeba 5 s na zapytanie.

DWIE RZECZY, KTÓRE TEN SKRYPT ROZSTRZYGA:

1. Czy `dedupe` w narzędziu w ogóle działa. On porównuje `md5(text)` — SUROWY
   tekst. Duplikaty w tej kolekcji różnią się znacznikiem daty w nagłówku
   („2026-09-22 22:04: ..." vs „2026-06-23 14:45: ..."), więc surowy hash ich
   NIE złapie. To sprawdzam osobno, bo jeśli prawda, to komenda `dedupe`
   raportuje „No duplicates" na korpusie, który ma ich ~500.

2. Czy usunięcie duplikatów podnosi hit@5. Porównuję kilka progów długości:
   krótkie fragmenty („check: 644 www-data, http 200") powtarzają się
   w różnych wpisach legalnie — to nie duplikaty, tylko boilerplate. Dopiero
   dłuższe teksty są realnymi kopiami.

Kryterium trafienia jest ODPORNE na dedupe: sprawdzam, czy słowo-klucz jest
w tekście KTÓREGOKOLWIEK zwróconego dokumentu, a nie czy wrócił konkretny
doc_id. Usunięcie jednej kopii nie unieważnia więc ground truth, o ile druga
zostaje.

Nie zmienia niczego w Qdrant — czyta istniejące wektory i filtruje lokalnie.
"""

import json
import os
import re
import time
from collections import defaultdict

import numpy as np
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import importlib.util  # noqa: E402

from fastembed import TextEmbedding  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ev", os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_pl.py")
)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CASES_FILE = "eval_cases_hard_pl.json"
EMB_DIM = 384
TOP_K = 5
LAMBDA = 0.01
MAX_CHARS = 2000

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?:\s*", re.S)


def normalize(text):
    """Zdejmuje znacznik daty z nagłówka i normalizuje białe znaki."""
    t = _DATE_PREFIX.sub("", text.strip())
    return re.sub(r"\s+", " ", t).strip().lower()


def decay(age_days, lmbda=LAMBDA):
    return 1 / (1 + lmbda * age_days)


def main():
    client = QdrantClient(
        url=os.getenv("QDRANT_URL"), api_key=os.getenv("QDRANT_API_KEY"), timeout=60
    )
    collection = os.getenv("COLLECTION_NAME")

    pts, off = [], None
    while True:
        r, off = client.scroll(
            collection,
            limit=1024,
            offset=off,
            with_payload=["text", "ts_epoch"],
            with_vectors=True,
        )
        pts += r
        if off is None:
            break
    texts = [p.payload.get("text", "") for p in pts]
    tss = [p.payload.get("ts_epoch") or 0 for p in pts]
    V = np.array([p.vector[:EMB_DIM] for p in pts], dtype=np.float32)
    V /= np.maximum(np.linalg.norm(V, axis=1, keepdims=True), 1e-9)

    # ── 1. Czy `dedupe` w narzędziu w ogóle coś znajdzie ──────────────────
    import hashlib

    raw_hashes = defaultdict(list)
    for i, t in enumerate(texts):
        raw_hashes[hashlib.md5(t.encode()).hexdigest()].append(i)
    raw_dups = sum(len(v) - 1 for v in raw_hashes.values() if len(v) > 1)
    print("=== 1. Skuteczność istniejącej komendy `dedupe` ===")
    print(f"  grupy po SUROWYM md5(text) [tak działa `dedupe`]: "
          f"{sum(1 for v in raw_hashes.values() if len(v) > 1)}, nadmiarowych: {raw_dups}")
    norm_hashes = defaultdict(list)
    for i, t in enumerate(texts):
        norm_hashes[normalize(t)].append(i)
    norm_dups = sum(len(v) - 1 for v in norm_hashes.values() if len(v) > 1)
    print(f"  grupy po normalizacji daty: "
          f"{sum(1 for v in norm_hashes.values() if len(v) > 1)}, nadmiarowych: {norm_dups}")
    print()

    # ── 2. Wpływ dedupe na hit@5 ─────────────────────────────────────────
    cases_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CASES_FILE)
    with open(cases_path, encoding="utf-8") as f:
        cases = [(c["query"], c["keyword"]) for c in json.load(f)]

    model = TextEmbedding(model_name=EMBED_MODEL)
    Q = np.array([v for v in model.embed([q for q, _ in cases])], dtype=np.float32)
    Q /= np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-9)

    now = int(time.time())

    def keep_indices(minlen):
        """Które punkty zostają po dedupe. None = bez dedupe.
        Zostaje NAJNOWSZA kopia — tak samo jak deklaruje komenda `dedupe`."""
        if minlen is None:
            return list(range(len(pts)))
        groups = defaultdict(list)
        for i, t in enumerate(texts):
            n = normalize(t)
            if len(n) >= minlen:
                groups[n].append(i)
        drop = set()
        for v in groups.values():
            if len(v) > 1:
                newest = max(v, key=lambda i: tss[i])
                drop.update(i for i in v if i != newest)
        return [i for i in range(len(pts)) if i not in drop]

    print("=== 2. Wpływ dedupe na wyszukiwanie (bez rerankera) ===")
    print(f"{'wariant':>18} {'korpus':>7} {'hit@5':>7} {'MRR':>7}")
    results = {}
    for label, minlen in [
        ("bez dedupe", None),
        ("dedupe >=60", 60),
        ("dedupe >=150", 150),
        ("dedupe >=300", 300),
    ]:
        keep = keep_indices(minlen)
        D = V[keep]
        ktexts = [texts[i] for i in keep]
        kts = [tss[i] for i in keep]
        ages = np.array([(now - t) / 86400 for t in kts])
        dec = 1.0 / (1.0 + LAMBDA * ages)
        for variant in ("cosine", "cosine+decay"):
            hits, rr = 0, 0.0
            for qi, (_, kw) in enumerate(cases):
                s = Q[qi] @ D.T
                if variant == "cosine+decay":
                    s = s * dec
                order = np.argsort(-s)[:TOP_K]
                pos = next(
                    (k + 1 for k, c in enumerate(order) if kw.lower() in ktexts[c].lower()),
                    None,
                )
                if pos:
                    hits += 1
                    rr += 1.0 / pos
            n = len(cases)
            results[(label, variant)] = hits / n
            print(f"{label + ' / ' + variant:>18} {len(keep):7d} {hits:3d}/{n} = {hits / n:3.0%} {rr / n:7.3f}")
    print()
    base = results[("bez dedupe", "cosine")]
    best = max((v, k) for k, v in results.items())
    print(f"najlepszy: {best[1]} = {best[0]:.0%}  (bez dedupe cosine = {base:.0%})")


if __name__ == "__main__":
    main()
