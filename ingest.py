#!/usr/bin/env python3
# Ingests documentation into Qdrant — RAM-safe: 20 facts/batch + gc.collect()
import os, sys, re, gc, glob, hashlib, subprocess
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
from fastembed import TextEmbedding
from datetime_utils import content_ts, l2norm, time_features

# Secret Guard — redaction of secrets in all facts before storing
from secret_guard import scrub

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_id import detect_agent  # noqa: E402  (po sys.path, celowo)

_BATCH_SIZE = 10


client = QdrantClient(
    url=os.getenv("QDRANT_URL"), api_key=os.getenv("QDRANT_API_KEY"), timeout=60
)
COLLECTION = os.getenv("COLLECTION_NAME")

# Model embeddingu — MUSI być identyczny jak EMBED_MODEL w
# qdrant-agent-memory-tool.py. Dwa różne modele = dokumenty i zapytania
# w dwóch różnych przestrzeniach = wyszukiwanie zwraca śmieci bez żadnego błędu.
# Stąd zmienna środowiskowa, żeby dało się je przełączyć jednym ruchem.
EMBED_MODEL = os.getenv(
    "QDRANT_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

# --replace forces a full re-ingest (clear + upload everything) instead of incremental append
FORCE_REPLACE = "--replace" in sys.argv

# Replace-mode SAFETY GATE. How much of the previous content must survive for a
# `--replace` to count as safe; below this ratio the script REFUSES and deletes
# nothing.
#
# Why this exists: `--replace` cleared the source FIRST and only then uploaded
# whatever the input yielded. The input is a FILE ON DISK, so whenever that file
# was smaller than last time — trimmed by a tool, restored from an older copy,
# archived — the missing entries were deleted from Qdrant with no warning and no
# way back. Measured 2026-09-23: /root/CHANGELOG.md had lost everything before
# 2026-09-19, so a plain `changelog --replace` would have taken that source from
# 4665 points down to 648 and printed success.
#
# Append mode is immune (content-based IDs make it idempotent, and it never
# deletes), so a shrink only ever bites in replace mode — which is exactly where
# it used to be silent. Override with --force-shrink for a source that genuinely
# did shrink (e.g. entries archived away on purpose).
MIN_SURVIVAL_RATIO = float(os.getenv("QDRANT_MIN_SURVIVAL", "0.8"))
FORCE_SHRINK = "--force-shrink" in sys.argv or os.getenv("QDRANT_FORCE_SHRINK") == "1"

# ===== SOURCE PATHS (configurable via env; defaults are typical locations) =====
VPS_DOC_PATH = os.getenv("QDRANT_VPS_DOC", os.path.expanduser("~/VPS.md"))
# Absolutna, bo jako jedyna była względna: uruchomienie z innego katalogu
# kończyło się FileNotFoundError. Reszta ścieżek w tym pliku jest absolutna
# (VPS.md, /var/www, /etc/nginx, /etc/systemd) — ta jedna została przeoczona.
CHANGELOG_PATH = os.getenv("QDRANT_CHANGELOG", "")
# CELOWO BEZ DOMYŚLNYCH ŚCIEŻEK — to repozytorium jest publiczne i ogólne,
# a układ katalogów jednej maszyny nie ma w nim czego szukać. Wszystkie
# wartości ustawia się w `.env` (patrz `.env.example`). Gdy brakuje którejś,
# odpowiednia funkcja MÓWI, czego brakuje — patrz `_require_path()`.
WWW_ROOT = os.getenv("QDRANT_WWW_ROOT", "").strip()
NGINX_DIR = os.getenv("QDRANT_NGINX_DIR", "").strip()
SYSTEMD_DIR = os.getenv("QDRANT_SYSTEMD_DIR", "").strip()


def _require_path(value, var, what):
    """Czy źródło jest skonfigurowane i istnieje na dysku.

    Bez tego puste `WWW_ROOT` kończyłoby się `glob` po katalogu "" (czyli nic)
    albo `open("")` i wyjątkiem. Funkcje ingestu mają powiedzieć, czego brakuje,
    zamiast wysypać się albo po cichu nic nie zrobić.
    """
    if not value:
        print(f"  {what}: NIE skonfigurowano — ustaw {var} w .env (patrz .env.example)")
        return False
    if not os.path.exists(value):
        print(f"  {what}: {var}={value!r} nie istnieje")
        return False
    return True

# ─── Źródła reguł (rules/ + globalne AGENTS) ───────────────────────────
# CELOWO BEZ DOMYŚLNYCH ŚCIEŻEK. To repozytorium jest publiczne i ogólne —
# ścieżki konkretnej maszyny nie mają tu czego szukać. Ustaw je u siebie
# w `.env` (jest w .gitignore), a jeśli ich nie ustawisz, funkcja niżej
# POWIE, co zrobić, zamiast po cichu nic nie robić.
#
#   QDRANT_RULES_DIR=/sciezka/do/twoich/regul        (katalog z plikami *.md)
#   QDRANT_AGENTS_FILES=/sciezka/AGENTS.md:/sciezka/AGENTS(PL).md
#                       ^ wiele plików rozdziela się DWUKROPKIEM
#
# Pełny opis: README.md → „Making the rules ingest work".
RULES_DIR = os.getenv("QDRANT_RULES_DIR", "").strip()
AGENTS_FILES = [
    p.strip() for p in os.getenv("QDRANT_AGENTS_FILES", "").split(":") if p.strip()
]

_HOWTO = """
  ── Jak włączyć ingest reguł ──────────────────────────────────────────
  Ten wpis nie ma skonfigurowanych żadnych źródeł, więc nic nie zapisał.
  Dopisz do swojego `.env` (NIE do repo — `.env` jest w .gitignore):

      QDRANT_RULES_DIR=/ścieżka/do/katalogu/z/regułami
      QDRANT_AGENTS_FILES=/ścieżka/AGENTS.md:/ścieżka/AGENTS(PL).md

  `QDRANT_RULES_DIR` to katalog, z którego czytane są WSZYSTKIE pliki `*.md`.
  `QDRANT_AGENTS_FILES` to lista plików rozdzielona dwukropkiem.
  Potem uruchom ponownie:

      ingest.py infrastructure

  Szczegóły i przykład: README.md → „Making the rules ingest work".
  ──────────────────────────────────────────────────────────────────────
"""

# Pozycja listy numerowanej: „12. **No magic numbers** — ..."
_NUMBERED_ITEM = re.compile(r"^\d+\.\s")


def clear_source(source: str):
    client.delete(
        collection_name=COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=source))]
        ),
    )
    print(f"  Removed old entries for source={source}")


def point_id(source: str, text: str) -> int:
    # Content-based ID — the same text always yields the same ID, regardless of
    # its position in the file. This way a new entry at the top of CHANGELOG
    # does not shift the IDs of the remaining entries.
    return (
        int(hashlib.sha256(f"{source}|{text}".encode()).hexdigest()[:16], 16)
        & 0x7FFFFFFFFFFFFFFF
    )


def get_existing_ids(source: str) -> set:
    # All point IDs of a source present in Qdrant (fast scroll, no payload)
    ids = set()
    offset = None
    while True:
        res = client.scroll(
            collection_name=COLLECTION,
            scroll_filter=Filter(
                must=[FieldCondition(key="source", match=MatchValue(value=source))]
            ),
            limit=1000,
            with_payload=False,
            offset=offset,
        )
        points, offset = res
        for p in points:
            ids.add(p.id)
        if not points or offset is None:
            break
    return ids


def store_facts(facts: list[dict], source: str, mode="replace") -> int:
    if not facts:
        print(f"  No facts for source={source}")
        return 0

    # Secret Guard — redact secrets in all facts before storing
    for f in facts:
        f["text"] = scrub(f.get("text", ""), source)

    if mode == "replace":
        # SAFETY GATE — check BEFORE deleting anything. `clear_source` is
        # irreversible from here: the old points are gone and their text lived
        # only in the input file, which is the very thing that shrank.
        before = len(get_existing_ids(source))
        if before and len(facts) < before * MIN_SURVIVAL_RATIO:
            if not FORCE_SHRINK:
                print(
                    f"  ❌ REFUSED: source={source} would shrink from {before} "
                    f"to {len(facts)} facts ({len(facts) / before:.0%} of what is "
                    "stored)."
                )
                print(
                    "     NOTHING was deleted. If the input legitimately shrank, "
                    "re-run with --force-shrink."
                )
                return 0
            print(
                f"  ⚠️  --force-shrink: source={source} {before} → {len(facts)} facts"
            )
        clear_source(source)
        planned = [(f, point_id(source, f["text"])) for f in facts]
    else:
        # append: embed and upload ONLY new facts (content-based ID = natural dedup)
        existing = get_existing_ids(source)
        planned = []
        for f in facts:
            pid = point_id(source, f["text"])
            if pid not in existing:
                planned.append((f, pid))
        if not planned:
            print(
                f"  {len(facts)} facts already in Qdrant — nothing new for source={source}"
            )
            return 0
        print(f"  New facts: {len(planned)}/{len(facts)} for source={source}")

    model = TextEmbedding(model_name=EMBED_MODEL)
    total = len(planned)
    stored = 0
    for bs in range(0, total, _BATCH_SIZE):
        batch = planned[bs : bs + _BATCH_SIZE]
        texts = [f[0]["text"] for f in batch]
        vecs = list(model.embed(texts))
        points = []
        for i, (fact, pid) in enumerate(batch):
            vec = l2norm(vecs[i].tolist())
            # ts_epoch = content date; time-feature vector consistent with ts_epoch
            cts = content_ts(fact)
            payload = {
                "text": fact["text"],
                "source": source,
                "section": fact.get("section", ""),
                "file_path": fact.get("file_path", ""),
                "type": fact.get("type", ""),
                # KTO wywolal i JAK. Ingest NIE chodzi z timera ani crona —
                # autorem jest zawsze ktos konkretny i jest poznawalny ze
                # srodowiska. Wczesniej bylo tu na sztywno "ingest", co mowilo
                # tylko "zrobil to skrypt" i gubilo autora.
                "agent": detect_agent(),
                "via": "ingest",
            }
            if COLLECTION.endswith("-v2"):
                vec += time_features(cts)
                payload["ts_epoch"] = cts
            points.append(PointStruct(id=pid, vector=vec, payload=payload))
        client.upsert(collection_name=COLLECTION, points=points)
        stored += len(batch)
        print(f"  batch {bs // _BATCH_SIZE + 1}: {stored}/{total}")
        gc.collect()
    print(f"  Stored {stored} facts, source={source}")
    return stored


# ===== SOURCES =====


def ingest_vps():
    if not _require_path(VPS_DOC_PATH, "QDRANT_VPS_DOC", "Dokument VPS"):
        return 0
    facts = []
    sec = ""
    with open(VPS_DOC_PATH) as f:
        for line in f:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("## "):
                sec = line.lstrip("# ").strip()
                continue
            if line.startswith("### "):
                sec = line.lstrip("# ").strip()
                continue
            if line.startswith("| **"):
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 4:
                    name = parts[0].replace("**", "")
                    rest = " | ".join(parts[1:])
                    facts.append(
                        {
                            "text": f"{sec}: {name} — {rest}"[:800],
                            "section": sec,
                            "file_path": VPS_DOC_PATH,
                            "type": "service",
                        }
                    )
                continue
            if line.startswith("```") or line.startswith("    "):
                continue
            if line.startswith("- ") or line.startswith("* "):
                facts.append(
                    {
                        "text": f"{sec}: {line.lstrip('-* ')}"[:800],
                        "section": sec,
                        "file_path": VPS_DOC_PATH,
                        "type": "config",
                    }
                )
                continue
            if len(line) > 40 and not line.startswith("|"):
                facts.append(
                    {
                        "text": f"{sec}: {line}"[:800],
                        "section": sec,
                        "file_path": VPS_DOC_PATH,
                        "type": "documentation",
                    }
                )
    store_facts(facts, source="vps-docs", mode="replace")
    return len(facts)


def ingest_changelog():
    # By default incremental (append) — embeddings only for new entries.
    # Full re-ingest: ingest.py changelog --replace
    if not _require_path(CHANGELOG_PATH, "QDRANT_CHANGELOG", "Changelog"):
        return 0
    mode = "replace" if FORCE_REPLACE else "append"
    facts = []
    date = ""
    with open(CHANGELOG_PATH) as f:
        for line in f:
            line = line.rstrip()
            if not line:
                continue
            m = re.match(r"^## (\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?) — (.+)$", line)
            if m:
                date = m.group(1)
                facts.append(
                    {
                        "text": f"{date}: {m.group(2)}",
                        "section": "changelog",
                        "file_path": CHANGELOG_PATH,
                        "type": "changelog-entry",
                        "date": date,
                    }
                )
                continue
            if line.startswith("- ") and date:
                facts.append(
                    {
                        "text": f"{date}: {line.lstrip('- ')}"[:800],
                        "section": "changelog",
                        "file_path": CHANGELOG_PATH,
                        "type": "changelog-detail",
                        "date": date,
                    }
                )
    store_facts(facts, source="changelog", mode=mode)
    return len(facts)


def ingest_instructions():
    import glob

    if not _require_path(WWW_ROOT, "QDRANT_WWW_ROOT", "Katalog projektów"):
        return 0
    facts = []
    proj = {}
    # Nazwa projektu = pierwszy segment ŚCIEŻKI WZGLĘDNEJ od WWW_ROOT.
    # Wcześniej było tu `f.split("/var/www/")[1]` — czyli korzeń zaszyty na
    # sztywno, który psuł się, gdy tylko QDRANT_WWW_ROOT wskazywał gdzie indziej.
    for f in glob.glob(os.path.join(WWW_ROOT, "**", "INSTRUKCJA*"), recursive=True):
        if "node_modules" not in f and ".git" not in f:
            pn = os.path.relpath(f, WWW_ROOT).split(os.sep)[0]
            proj.setdefault(pn, []).append(f)
    for f in glob.glob(os.path.join(WWW_ROOT, "**", "README.md"), recursive=True):
        if "node_modules" not in f and ".git" not in f:
            pn = os.path.relpath(f, WWW_ROOT).split(os.sep)[0]
            if f not in proj.get(pn, []):
                proj.setdefault(pn, []).append(f)
    for project, files in proj.items():
        src = f"project-{project}"
        pf = []
        for fp in files:
            try:
                with open(fp) as fh:
                    c = fh.read()
                for p in c.split("\n\n"):
                    p = p.strip()
                    if (
                        len(p) > 30
                        and not p.startswith("#")
                        and not p.startswith("```")
                    ):
                        pf.append(
                            {
                                "text": f"[{project}] {p.replace(chr(10), ' ')}"[:800],
                                "section": project,
                                "file_path": fp,
                                "type": "project-doc",
                            }
                        )
            except Exception as e:
                print(f"  Skipped {fp}: {e}")
        if pf:
            clear_source(src)
            store_facts(pf, source=src, mode="append")
            facts.extend(pf)
            print(f"  project {project}: {len(pf)} facts")
    return len(facts)


def ingest_nginx():
    import glob

    if not _require_path(NGINX_DIR, "QDRANT_NGINX_DIR", "Konfiguracja nginx"):
        return 0
    facts = []
    for fp in glob.glob(os.path.join(NGINX_DIR, "*")):
        if "default" in fp:
            continue
        try:
            with open(fp) as f:
                c = f.read()
            sn = re.findall(r"server_name\s+([^;]+);", c)
            pp = re.findall(r"proxy_pass\s+(https?://[^;]+);", c)
            li = re.findall(r"listen\s+([^;]+);", c)
            rl = re.findall(r"rate=([^;]+);", c)
            name = sn[0].strip() if sn else os.path.basename(fp)
            for port in (l.strip() for l in li):
                for proxy in pp:
                    t = f"nginx: {name} → {proxy} (port {port})"
                    if rl:
                        t += f" | rate limit: {rl[0]}"
                    facts.append(
                        {
                            "text": t[:400],
                            "section": "nginx-routes",
                            "file_path": fp,
                            "type": "nginx-route",
                        }
                    )
        except Exception as e:
            print(f"  Skipped {fp}: {e}")
    store_facts(facts, source="nginx-routes", mode="replace")
    return len(facts)


def ingest_systemd():
    import glob

    if not _require_path(SYSTEMD_DIR, "QDRANT_SYSTEMD_DIR", "Usługi systemd"):
        return 0
    facts = []
    for fp in glob.glob(os.path.join(SYSTEMD_DIR, "*.service")):
        try:
            with open(fp) as f:
                c = f.read()
            desc = re.search(r"Description=([^\n]+)", c)
            ex = re.search(r"ExecStart=([^\n]+)", c)
            if ex:
                name = os.path.basename(fp).replace(".service", "")
                t = f"systemd service {name}: {ex.group(1).strip()}"
                if desc:
                    t += f" ({desc.group(1).strip()})"
                facts.append(
                    {
                        "text": t[:400],
                        "section": "systemd-services",
                        "file_path": fp,
                        "type": "systemd",
                    }
                )
        except Exception as e:
            print(f"  Skipped {fp}: {e}")
    store_facts(facts, source="systemd-services", mode="replace")
    return len(facts)


def _chunk_rules_file(text, path):
    """Dzieli jeden plik reguł na fakty (jedna reguła = jeden fakt).

    Pliki mają DWA kształty i oba trzeba obsłużyć:
      - `vps-devops.md` — tytuł `#` + sekcje `##`
      - `python.md`     — tytuł `#` + lista numerowana (`1. ...`, `2. ...`)
    Dzielenie tylko po `##` pomijałoby CAŁE pliki językowe (python, css, js, ts,
    html) — czyli dokładnie te reguły, po które agent sięga najczęściej.

    Tytuł pliku wchodzi do każdego faktu jako przedrostek: bez niego fakt
    „No `except: pass` — log every exception" nie mówi, że chodzi o Pythona.
    """
    lines = text.splitlines()
    title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), "")
    stem = os.path.basename(path)
    head = f"[{stem} — {title}] " if title else f"[{stem}] "

    # Który to kształt? Sekcje `##` wygrywają, gdy są; inaczej lista numerowana.
    split_on_items = not any(ln.startswith("## ") for ln in lines)

    chunks, current = [], None
    for ln in lines:
        if ln.startswith("## "):
            if current:
                chunks.append(current)
            current = [ln[3:].strip(), []]
        elif split_on_items and _NUMBERED_ITEM.match(ln):
            if current:
                chunks.append(current)
            current = [ln.strip(), []]
        elif current is not None:
            current[1].append(ln)
    if current:
        chunks.append(current)

    facts = []
    for heading, body in chunks:
        body_text = "\n".join(body).strip()
        # UWAGA: nie wolno pomijać pustego `body`. W plikach-listach treść
        # reguły JEST nagłówkiem („1. **No semicolons** — ...”), a `body` bywa
        # puste. Warunek `if not body_text: continue` wyrzucał wtedy CAŁY plik
        # — dokładnie te pliki językowe, dla których ten podział powstał.
        text = f"{head}{heading}" + (f"\n{body_text}" if body_text else "")
        facts.append(
            {
                "text": text,
                "section": heading[:80],
                "file_path": path,
                "type": "rules",
            }
        )
    return facts


def _clear_file_paths(source, paths):
    """Kasuje ze źródła TYLKO fakty pochodzące z podanych plików.

    `clear_source` czyści całe źródło — a to skasowałoby również fakty dodane
    przez `store`, które nie pochodzą z żadnego pliku. Odświeżenie reguł nie
    może niszczyć niezwiązanych wspomnień (w `infrastructure` są trzy takie).
    """
    if not paths:
        return
    # Filtr po `file_path` wymagałby indeksu keyword na tym polu, którego nie ma
    # (Qdrant odrzuca: „Index required but not found"), a założenie indeksu to
    # zmiana schematu kolekcji. Zamiast tego filtrujemy po `source` — to pole
    # JEST zindeksowane — a `file_path` sprawdzamy w Pythonie.
    wanted = set(paths)
    to_delete = []
    offset = None
    while True:
        page, offset = client.scroll(
            collection_name=COLLECTION,
            limit=256,
            offset=offset,
            scroll_filter=Filter(
                must=[FieldCondition(key="source", match=MatchValue(value=source))]
            ),
            with_payload=["file_path"],
            with_vectors=False,
        )
        for p in page:
            if p.payload.get("file_path") in wanted:
                to_delete.append(p.id)
        if offset is None:
            break
    if not to_delete:
        return
    # Bez kopii zapasowej — i celowo: kasujemy wyłącznie fakty odtwarzalne
    # z plików, które nadal leżą na dysku (i w tej samej chwili są wstawiane
    # na nowo). Kopia nie miałaby czego ratować.
    client.delete(collection_name=COLLECTION, points_selector=to_delete)


def ingest_infrastructure():
    """Reguły z `rules/*.md` + globalne AGENTS → źródło `infrastructure`.

    ODŚWIEŻANIE PER PLIK, nie per źródło: fakty z plików, które właśnie
    czytamy, są podmieniane (żeby edycja reguły nie zostawiała starej wersji),
    a fakty dodane przez `store` — które nie pochodzą z żadnego pliku —
    zostają nietknięte.

    Wcześniej była tu ATRAPA z dwoma przykładami na sztywno. Skutek: opisany
    w AGENTS.md przepis „po każdej zmianie AGENTS.md przeingestuj
    infrastructure" nie robił NIC — a z `--force-shrink` zastąpiłby trzy
    prawdziwe fakty dwoma przykładami (bramka shrinku właśnie to złapała).
    """
    paths = []
    if RULES_DIR and os.path.isdir(RULES_DIR):
        paths += sorted(glob.glob(os.path.join(RULES_DIR, "*.md")))
    paths += [p for p in AGENTS_FILES if os.path.isfile(p)]

    if not paths:
        # Nie „cicho nic" — powiedz wprost, czego brakuje i jak to ustawić.
        print(_HOWTO)
        if RULES_DIR:
            print(f"  (QDRANT_RULES_DIR={RULES_DIR!r} — taki katalog nie istnieje "
                  "albo nie ma w nim plików .md)")
        return 0

    facts = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            facts += _chunk_rules_file(fh.read(), p)

    # WSZYSTKIE reguły dostają IDENTYCZNY czas wgrania.
    #
    # Reguła jest bezczasowa, a `content_ts()` bez tego wpisu brałby datę
    # modyfikacji PLIKU (u nas czerwiec) albo przypadkową datę z treści.
    # Kolekcja `-v2` waży świeżość 28% wyniku, więc reguła z pliku nietkniętego
    # od czerwca przegrywała z regułą z pliku zapisanego dzisiaj — bez związku
    # z tym, która odpowiada na pytanie.
    #
    # Wspólny znacznik sprawia, że ten składnik sumy jest jednakowy dla każdej
    # reguły, więc przestaje je różnicować i ranking wraca do porównywania
    # samej treści. Zmierzone: 4% -> ~50% hit@5 na zestawie reguł.
    # WYMAGA wznawiania reguł po edycji (inaczej znów się „starzeją").
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for f in facts:
        f["date"] = stamp

    print(f"  Plików: {len(paths)}, faktów: {len(facts)}")
    _clear_file_paths("infrastructure", paths)
    return store_facts(facts, source="infrastructure", mode="append")
    return len(facts)


SOURCES = {
    "vps-docs": ingest_vps,
    "changelog": ingest_changelog,
    "instructions": ingest_instructions,
    "nginx": ingest_nginx,
    "systemd": ingest_systemd,
    "infrastructure": ingest_infrastructure,
}


def main():
    # Sequential mode: each source as a separate process → RAM-safe
    if len(sys.argv) > 1 and sys.argv[1] == "--sequential":
        total = 0
        for name in SOURCES:
            print(f"\n=== INGEST: {name} (separate process) ===\n")
            cmd = [sys.executable, __file__, name]
            if FORCE_REPLACE:
                cmd.append("--replace")
            if FORCE_SHRINK:
                # Must travel to the child too — the gate lives in store_facts,
                # which runs in the subprocess.
                cmd.append("--force-shrink")
            r = subprocess.run(cmd, capture_output=False)
            if r.returncode != 0:
                print(f"  ❌ {name} FAILED (exit code {r.returncode})")
            else:
                total += 1
        print(
            f"\n✅ Sequential ingest finished: {total}/{len(SOURCES)} sources ready"
        )
        return

    # Mode with --only (single source); --replace is parsed globally
    # Both FLAGS carry no source name — leaving them in would make `--force-shrink`
    # look like a source and abort with "Unknown: --force-shrink".
    args = [a for a in sys.argv[1:] if a not in ("--replace", "--force-shrink")]
    only = args[0] if args else None
    if only:
        if only not in SOURCES:
            print(f"Unknown: {only}. Available: {', '.join(SOURCES.keys())}")
            sys.exit(1)
        print(f"=== Ingest: {only} ===\n")
        n = SOURCES[only]()
        print(f"\n✅ {n} facts from {only}")
        return
    total = 0
    print("=== Full Qdrant ingest ===\n")
    for name, fn in SOURCES.items():
        print(f"[{name}]")
        n = fn()
        total += n
        print(f"      {n} facts\n")
    print(f"✅ Ingest finished: {total} facts")


if __name__ == "__main__":
    main()
