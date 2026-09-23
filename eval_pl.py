#!/usr/bin/env python3
"""Eval retrievalu dla polskich zapytań semantycznych — hit@5 i MRR.

Użycie:
    .venv/bin/python eval_pl.py <model_name>

Po co: kolekcja jest w ~86% polska (changelog), a oryginalny model
(all-MiniLM-L6-v2) jest WYŁĄCZNIE angielski. Ten skrypt mierzy liczbami, czy
zmiana modelu na wielojęzyczny realnie poprawia wyszukiwanie.

DWA POMIARY — celowo, bo mierzą różne rzeczy:

  1. "qdrant"  — tak jak w produkcji: wektor 392 (384 + 8 cech czasu), ranking
                 po stronie Qdranta. End-to-end, ale cechy czasu współdecydują.
  2. "semantyczny" — offline, cosine tylko po pierwszych 384 wymiarach. Izoluje
                 SAM embedding od projektu cech czasu, więc pokazuje czysty
                 efekt zmiany modelu.

Gdyby cechy czasu dominowały ranking (a mają niemałą normę), pomiar 1 mógłby
zamaskować realną poprawę albo regresję. Dlatego pomiar 2 istnieje.

Zapytania są CELOWO parafrazami Z POLSKIMI DIAKRYTYKAMI: nie zawierają
dystynktywnych tokenów dokumentu (np. "fail2ban", "zram"). Na pokryciu
leksykalnym oba modele wypadają dobrze, więc taki test niczego by nie
rozróżnił — różnica wychodzi na semantyce.

Uruchamiać z katalogu tego pliku (importuje datetime_utils).
"""

import os
import sys
import time

import numpy as np
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from datetime_utils import l2norm, time_features  # noqa: E402
from fastembed import TextEmbedding  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

# (zapytanie, oczekiwane słowo-klucz, które MUSI pojawić się w trafnym dokumencie)
CASES = [
    (
        "automatyczne blokowanie adresów po nieudanych próbach logowania, lista wyjątków, ręczne banowanie",
        "ip-ctl",
    ),
    (
        "ograniczanie częstości żądań z jednego adresu na poziomie serwera proxy",
        "limit_req",
    ),
    (
        "instalacja pakietu w katalogu należącym do większego projektu — pakiet ląduje nie tam gdzie trzeba",
        "pnpm-workspace.yaml",
    ),
    (
        "pierwsze logowanie hasłem do pojedynczej strony; plik haseł musi należeć do użytkownika serwera",
        "auth_basic",
    ),
    (
        "treść notatek w zaszyfrowanej bazie, w jawnym pliku tylko metadane",
        "SQLCipher",
    ),
    (
        "celowo wolna funkcja utrudniająca odgadnięcie hasła",
        "rgon2",
    ),
    (
        "kraj klienta ustalany bez wysyłania adresu IP na zewnątrz",
        "geoip-lite",
    ),
    (
        "sprawdzenie układu strony przez wyliczone pozycje elementów, bez zrzutu ekranu",
        "patchright",
    ),
    (
        "jednorazowy kod jako drugi składnik logowania, mógł po cichu przepuścić na port SSH",
        "PAMServiceName",
    ),
    (
        "kompresja pamięci przed sięgnięciem do dysku",
        "zram",
    ),
]

TOP_K = 5
EMB_DIM = 384  # wymiar embeddingu; resztę (do 392) stanowią cechy czasu


def _scroll_all(client, collection):
    pts, off = [], None
    while True:
        r, off = client.scroll(
            collection,
            limit=1024,
            offset=off,
            with_payload=["text"],
            with_vectors=True,
        )
        pts += r
        if off is None:
            return pts


def _rank_of_keyword(texts, kw, doc_indices):
    """Pozycja w rankingu (1-based), na której pojawia się słowo-klucz (albo None).

    UWAGA: `doc_indices` to INDEKSY DOKUMENTÓW w kolejności rankingu, nie same
    pozycje. Zwracanie `idx + 1` zamiast `pos` dawało numer dokumentu (np. 679)
    zamiast pozycji — hit@5 pozostawał poprawny, ale MRR był bez sensu i wychodził
    0.000 przy niezerowej liczbie trafień.
    """
    for pos, idx in enumerate(doc_indices, 1):
        if kw.lower() in texts[idx].lower():
            return pos
    return None


def main():
    if len(sys.argv) < 2:
        sys.exit("użycie: eval_pl.py <model_name>")
    model_name = sys.argv[1]

    client = QdrantClient(
        url=os.getenv("QDRANT_URL"), api_key=os.getenv("QDRANT_API_KEY"), timeout=60
    )
    collection = os.getenv("COLLECTION_NAME")
    pts = _scroll_all(client, collection)
    texts = [p.payload.get("text", "") for p in pts]

    # ── Sanity check: ground truth musi istnieć w bazie ────────────────────
    # Bez tego zły eval set cicho raportuje 0% i wygląda jak wina modelu.
    broken = [kw for _, kw in CASES if not any(kw.lower() in t.lower() for t in texts)]
    if broken:
        print(f"❌ ZEPSUTY EVAL SET — brak w bazie: {broken}")
        print("   Popraw słowa-klucze przed pomiarem.")
        sys.exit(2)

    # ── Embed zapytań i dokumentów ────────────────────────────────────────
    model = TextEmbedding(model_name=model_name)
    queries = [q for q, _ in CASES]
    qvecs = np.array([v for v in model.embed(queries)], dtype=np.float32)

    doc_vecs = np.array([p.vector[:EMB_DIM] for p in pts], dtype=np.float32)
    doc_norms = np.linalg.norm(doc_vecs, axis=1, keepdims=True)
    doc_unit = doc_vecs / np.maximum(doc_norms, 1e-9)
    q_unit = qvecs / np.maximum(np.linalg.norm(qvecs, axis=1, keepdims=True), 1e-9)
    sem_scores = q_unit @ doc_unit.T  # (n_queries, n_docs)

    # Cechy czasu — tak jak w produkcji (zapytanie = "teraz")
    tf = time_features(int(time.time()))

    results = {}
    for label in ("semantyczny", "qdrant"):
        hits, rr_sum, rows = 0, 0.0, []
        for qi, (query, kw) in enumerate(CASES):
            if label == "semantyczny":
                ranks = np.argsort(-sem_scores[qi])[:TOP_K]
            else:
                # l2norm — DOKŁADNIE jak produkcyjne _embed. Bez tego zapytanie
                # wchodzi z surową normą modelu (np. 4.17), a dokumenty
                # z normą 1 — i cechy czasu mają inną wagę w zapytaniu niż
                # w dokumentach, więc pomiar nie odzwierciedla produkcji.
                res = client.query_points(
                    collection_name=collection,
                    query=l2norm(qvecs[qi].tolist()) + tf,
                    limit=TOP_K,
                    with_payload=["text"],
                ).points
                id_to_idx = {p.id: i for i, p in enumerate(pts)}
                ranks = [id_to_idx[p.id] for p in res]
            rank = _rank_of_keyword(texts, kw, ranks)
            if rank:
                hits += 1
                rr_sum += 1.0 / rank
            rows.append((rank, kw, query))
        results[label] = (hits, rr_sum, rows)

    n = len(CASES)
    print(f"model: {model_name}")
    print(f"korpus: {len(pts)} punktów")
    for label in ("semantyczny", "qdrant"):
        hits, rr_sum, _ = results[label]
        print(f"  hit@{TOP_K} [{label:11}] = {hits}/{n} = {hits / n:4.0%}   MRR = {rr_sum / n:.3f}")
    print()
    for i, (query, kw) in enumerate(CASES):
        s = results["semantyczny"][2][i][0]
        q = results["qdrant"][2][i][0]
        print(f"  sem={str(s or 'MISS'):>4}  qdrant={str(q or 'MISS'):>4}  [{kw}]  {query[:62]}")


if __name__ == "__main__":
    main()
