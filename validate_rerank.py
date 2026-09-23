#!/usr/bin/env python3
"""Walidacja rerankera cross-encoder — offline, BEZ dotykania kolekcji i search().

Użycie:
    .venv/bin/python validate_rerank.py

Po co: zmierzony problem to PRECYZJA RANKINGU, nie brak trafień — właściwe
dokumenty są w kolekcji, ale na pozycjach 15–43 (hit@5 = 20%, recall@50 = 70%).
Reranker drugiego etapu (cross-encoder czyta zapytanie i dokument RAZEM) powinien
wciągnąć je do top-5. Ten skrypt sprawdza, czy tak jest, ZANIM cokolwiek wejdzie
do qdrant-agent-memory-tool.py.

Cztery warianty, ten sam zestaw zapytań:
  cosine        — jak dziś (top-5 po cosinusie)
  cosine+decay  — jak dziś produkcyjnie (cosinus × zanik czasu)
  rerank        — sam reranker na 50 kandydatach
  rerank+decay  — reranker znormalizowany min-max do [0,1], POTEM zanik czasu

Ten ostatni wariant istnieje, bo naiwny reranker CICHO KASUJE zanik czasu:
wyniki cross-encodera to logity o dużej skali (często ujemne), więc pomnożenie
ich przez decay ~0.77 nic nie zmienia, a przy cosinusie ~0.4–0.7 zmieniało.
Normalizacja przed mnożeniem przywraca zanikowi jego wagę.

Nie zmienia niczego w Qdrant — czyta tylko istniejące wektory.
"""

import faulthandler
import os
import signal
import sys
import time

import numpy as np
from dotenv import load_dotenv

# Przy SIGTERM zrzuć stos WSZYSTKICH wątków. Bez tego proces ginie po cichu
# (brak tracebacku, brak kodu wyjścia w potoku) i nie wiadomo, w którym miejscu
# — a właśnie tak dwa razy zginęła ta walidacja.
faulthandler.register(signal.SIGTERM, all_threads=True)

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import importlib.util  # noqa: E402

from fastembed import TextEmbedding  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ev", os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_pl.py")
)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

EMB_DIM = 384
TOP_K = 5
CANDIDATES = 50  # tyle podajemy rerankerowi; recall@50 = 70%
MAX_CHARS = 2000  # ~512 tokenów; dłuższego tekstu reranker i tak nie przeczyta
RERANK_BATCH = 8  # nie 64: maszyna ma 7,8 GB RAM i swap zajęty w ~90%
LAMBDA = 0.01
# Model rerankera można podać argumentem. Domyślny wielojęzyczny ma RSS ~2,4 GB
# i na tym serwerze NIE WSTAJE — earlyoom zabija go SIGTERM-em, gdy dostępny RAM
# spadnie do 10% (zmierzone 2026-09-23, log earlyoom). Dlatego da się podmienić
# na mały i sprawdzić, czy ścieżka rerankera jest tu w ogóle wykonalna.
RERANK_MODEL = "jinaai/jina-reranker-v2-base-multilingual"
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def decay(age_days, lmbda=LAMBDA):
    return 1 / (1 + lmbda * age_days)


def minmax(xs):
    lo, hi = min(xs), max(xs)
    if hi - lo < 1e-9:
        return [0.5] * len(xs)
    return [(x - lo) / (hi - lo) for x in xs]


def mem_available_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def main():
    rerank_model = sys.argv[1] if len(sys.argv) > 1 else RERANK_MODEL

    # Bramka pamięciowa. Na tym serwerze działa earlyoom z progami mem 10% /
    # swap 10% — gdy je przekroczymy, wysyła SIGTERM do procesu z najwyższym
    # oom_score (czyli naszego). Zabija wtedy proces, który może nie być nasz,
    # więc lepiej odmówić z góry niż liczyć na szczęście. Wczytanie modelu
    # rerankera to ~2,4 GB dla wersji wielojęzycznej i ~0,4 GB dla małych.
    # Próg liczony tak, jak działa earlyoom: po wczytaniu modelu musi ZOSTAĆ
    # zapas powyżej jego progu (mem 10% z ~4,8 GB ≈ 480 MB) plus margines.
    # RSS wielojęzycznego zmierzony: 2395 MiB (cały proces). Sam model ~1,9 GB.
    # 1200 MB byłoby progiem, który przepuszcza model, po którym i tak
    # przychodzi SIGTERM — czyli bramką, która niczego nie chroni.
    avail = mem_available_mb()
    is_big = "multilingual" in rerank_model or "bge-reranker" in rerank_model
    need = 3600 if is_big else 1200
    if avail < need:
        print(f"❌ ODMOWA: dostępny RAM {avail} MB < wymagane ~{need} MB.")
        print("   earlyoom ma progi mem 10% / swap 10% i wysyła SIGTERM do")
        print("   największego procesu — uruchomienie tego teraz mogłoby")
        print("   ubić cudzą pracę. Zwolnij pamięć albo użyj mniejszego modelu.")
        sys.exit(3)
    print(f"RAM dostępny: {avail} MB (wymagane ~{need} MB) — OK\n")

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

    D = np.array([p.vector[:EMB_DIM] for p in pts], dtype=np.float32)
    D /= np.maximum(np.linalg.norm(D, axis=1, keepdims=True), 1e-9)

    # Ten sam model co wdrożony w ingest.py / narzędziu — walidujemy reranker
    # na dokładnie tej przestrzeni, która jest w kolekcji.
    model = TextEmbedding(model_name=EMBED_MODEL)
    queries = [q for q, _ in ev.CASES]
    Q = np.array([v for v in model.embed(queries)], dtype=np.float32)
    Q /= np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-9)

    print(f"model embeddingu: {EMBED_MODEL}")
    print(f"reranker:         {rerank_model}")
    print(f"korpus:           {len(pts)} punktów, kandydatów na zapytanie: {CANDIDATES}")
    print("ładowanie rerankera (pierwsze użycie może pobierać)...")

    from fastembed.rerank.cross_encoder import TextCrossEncoder

    t0 = time.time()
    reranker = TextCrossEncoder(model_name=rerank_model)
    print(f"  reranker gotowy w {time.time() - t0:.1f}s\n")

    now = time.time()
    variants = ["cosine", "cosine+decay", "rerank", "rerank+decay"]
    hits = {v: 0 for v in variants}
    rr = {v: 0.0 for v in variants}
    rerank_times = []

    for qi, (query, kw) in enumerate(ev.CASES):
        order = np.argsort(-(Q[qi] @ D.T))[:CANDIDATES]
        # cand_full — PEŁNY tekst, wyłącznie do sprawdzenia słowa-klucza.
        # cand_texts — obcięty, tylko dla rerankera. Gdyby sprawdzać na
        # obciętym, trafienie leżące poza obcięciem policzyłoby się jako MISS
        # i sztucznie zaniżyło wynik rerankera.
        cand_full = [texts[j] for j in order]
        cand_texts = [t[:MAX_CHARS] for t in cand_full]
        ages = [(now - tss[j]) / 86400 for j in order]
        cos = (Q[qi] @ D.T)[order]

        # Obcięcie do MAX_CHARS. Reranker i tak przyjmuje maks. 512 tokenów
        # (~2000 znaków dla polskiego), więc ogon tekstu jest odrzucany — ale
        # najpierw musi zostać POTOKENIZOWANY i zaalokowany. Wpisy changelogu
        # mają do 4798 znaków, więc bez obcięcia każde zapytanie alokuje
        # kilkukrotnie więcej, niż zużyje. batch_size=8 z tego samego powodu:
        # to maszyna z 7,8 GB RAM i swapem zajętym w ~90% (stan sprzed tej sesji).
        t0 = time.time()
        raw = list(reranker.rerank(query, cand_texts, batch_size=RERANK_BATCH))
        rerank_times.append(time.time() - t0)

        rr_norm = minmax(raw)

        rankings = {
            "cosine": np.argsort(-cos),
            "cosine+decay": np.argsort(-(cos * np.array([decay(a) for a in ages]))),
            "rerank": np.argsort(-np.array(raw)),
            "rerank+decay": np.argsort(-(np.array(rr_norm) * np.array([decay(a) for a in ages]))),
        }

        line = []
        for v in variants:
            top = rankings[v][:TOP_K]
            pos = next(
                (k + 1 for k, c in enumerate(top) if kw.lower() in cand_full[c].lower()),
                None,
            )
            if pos:
                hits[v] += 1
                rr[v] += 1.0 / pos
            line.append(f"{v}={pos or 'MISS'}")
        print(f"  [{kw:20s}] " + "  ".join(line))

    n = len(ev.CASES)
    print(f"\n{'wariant':16s} {'hit@5':>7s}  {'MRR':>6s}")
    for v in variants:
        print(f"{v:16s} {hits[v]:2d}/{n} = {hits[v] / n:3.0%}  {rr[v] / n:6.3f}")
    print(f"\nczas reranku na zapytanie: średnio {np.mean(rerank_times):.2f}s "
          f"(min {min(rerank_times):.2f}s, max {max(rerank_times):.2f}s), "
          f"{CANDIDATES} kandydatów")


if __name__ == "__main__":
    main()
