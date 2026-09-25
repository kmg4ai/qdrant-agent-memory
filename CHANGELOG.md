# Changelog

## 2026-09-25 13:29 — Rules ingest actually works + tutorial for a fresh clone

**The defect.** `ingest_infrastructure()` was a **stub holding two hardcoded
example facts**. It read no rules at all. Consequences: the documented workflow
("after every AGENTS.md change, re-ingest infrastructure") did **nothing**, and
running it with `--force-shrink` would have replaced three real stored facts
with two examples — the shrink gate refused, which is how it was found.

**The fix.** It now reads real sources and chunks them **one rule = one fact**:

| file shape | split on |
|---|---|
| `# Title` + `## Section` | each `##` section |
| `# Title` + numbered list | each numbered item |

Both shapes are needed. Splitting only on `##` skipped **whole language files**
(`python.md`, `css.md`, `js.md`, `ts.md`, `html.md`) — exactly the rules an agent
reaches for most. A first cut of this fix did skip them and reported `0 faktów`
for each; the file title is now prefixed onto every fact so a bare
`No except: pass — logging` still says it is about Python.

**Refresh is PER FILE, not per source.** Facts from the files being read are
replaced (an edited rule must not leave its old version behind); facts added via
`store`, which come from no file, are left alone. `clear_source` would have
deleted those too — three such facts live in `infrastructure`.

> Filtering by `file_path` needs a keyword index that the collection does not
> have, and adding one is a schema change. So the code filters on `source` (which
> *is* indexed) and checks `file_path` in Python.

**Two versions, by construction.** The public repo carries the generic code; the
machine-specific paths live only in `.env` (`.gitignore`d). With no sources
configured the command prints step-by-step instructions and stores **zero** facts
— a fresh clone cannot silently do nothing, and cannot delete anything.

- `.env.example` documents every variable, including the new model and reranker
  ones.
- `README.md` gains **"🧠 Ingesting your coding rules"** — what to set, what the
  output should look like, how files are split, and how to refresh.

Measured on a 27-file rules tree: **262 facts, 0 files yielding nothing**, and
the three `store`-added facts in the source left intact.

## 2026-09-23 18:20 — Multilingual embedding model + fixed destructive reindex

**Why.** The collection is ~86% Polish (7094 of 8236 points are `changelog`), but
the embedding model was `all-MiniLM-L6-v2` — English-only. Measured baseline on a
10-query Polish paraphrase set: **hit@5 = 10%** (semantic only) and **0%** through
the production path. A Polish query for "kompresja pamięci przed sięgnięciem do
dysku" scored 0.674 and returned documents about scraping; the same query in
English scored 0.475 and returned the correct document at rank 2. Higher score
with worse results is the signature of a model with no Polish representation: the
texts collapse toward a common direction.

**The swap.** `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` —
also 384 dims, so **no schema change, no new collection**; Qdrant's config is
untouched. Model name now lives in one place per file (`EMBED_MODEL`, overridable
via `QDRANT_EMBED_MODEL`) instead of being hardcoded in three.

**Measured result — improvement confirmed, but not sufficient:**

| model | hit@5 (semantic) | MRR | hit@5 (production) | MRR |
|---|---|---|---|---|
| all-MiniLM-L6-v2 | 10% | 0.050 | 0% | 0.000 |
| multilingual-MiniLM-L12 | 20% | 0.053 | 20% | 0.058 |

The production path went from *broken* (0%) to *matching the embedding's own
ceiling*. That ceiling is the finding: the full rank curve is
top-1 **0%**, top-3 10%, top-5 20%, top-10 20%, **top-25 60%**, top-50 70%.
The correct documents are routinely present but ranked 15–43 — score spread
between rank 1 and rank 23 is only ~0.04. The model "roughly knows" and cannot
rank precisely. Corpus has **zero exact duplicate texts**, so this is not
duplicate crowding. The next lever is a higher-capacity model (768/1024 dims),
which *would* require a new collection.

**Two bugs found and fixed:**

1. `reindex_source` set `payload["ts_epoch"] = now` and recomputed time features
   from `now` on every point. A reindex would have collapsed 7094 dated changelog
   entries into one instant, permanently breaking `--since`/`--window` and time
   decay — the old dates lived only in the vectors. Now derived from the
   preserved `ts_epoch`; verified bit-for-bit identical on 15 points before
   running against all 8236.
2. `reindex_source` called `_embed` per point, and `_embed` constructs
   `TextEmbedding` on every call — 8236 ONNX loads for a full reindex. Now batched
   through one model instance.

**`l2norm` added (`datetime_utils.py`).** Qdrant normalizes the whole 392-vector
on upsert for cosine — verified: every stored vector has `|v| = 1.000000`. Raw
model output is *not* unit-norm and differs per model and per text
(measured: all-MiniLM-L6-v2 → 1.000, multilingual-MiniLM-L12 → 2.83–4.17), so
without normalization the time features would carry a different weight per
document, and swapping the model would change two things at once. Normalizing
first keeps the time/semantic balance identical to before, so the swap changes
only language quality.

**`reindex-all` command added.** `reindex_source(None)` — recompute the whole
collection, needed after any model change. Idempotent: recomputes from `text` and
preserves `ts_epoch`, so re-running after a mid-way failure repairs the mixed
state.

**`fix_created_at.py`** now uses the same `EMBED_MODEL` — it re-embeds points, so
a stale hardcoded model would have silently written vectors from a different
space into the collection.

**`eval_pl.py` added** — 10 Polish paraphrase queries with expected keywords,
reporting hit@5/MRR for both the semantic-only and production paths. Self-checks
that each expected keyword exists in the corpus before measuring, so a bad eval
set fails loudly instead of silently reporting 0%.

**Embedding cache moved out of `/tmp`.** `/tmp` is tmpfs (RAM): 328 MB of model
weights sat in memory and vanished on reboot, forcing a re-download. Relocated to
`/root/.cache/fastembed` via `FASTEMBED_CACHE_PATH` in `.env` (fastembed has no
CLI flag for it but reads this variable). `/tmp` RAM usage 1.2 GB → 871 MB.
Verified `define_cache_dir()` honours it and the model loads from cache in 3.95 s.

## 2026-09-23 14:34 — Replace-mode safety gate: refuse to delete on a shrinking source

**The defect.** `--replace` — and the four sources that hardcode `mode="replace"`
— called `clear_source()` FIRST and only then uploaded whatever the input yielded.
The input is a file on disk, so whenever that file was smaller than last time
(trimmed, restored from an older copy, archived) the missing entries were deleted
from Qdrant with no warning and no way back. Measured 2026-09-23:
`/root/CHANGELOG.md` had lost everything before 2026-09-19, so a plain
`changelog --replace` would have taken that source from **4665 points down to
648** and printed a success line.

Append mode was never at risk (content-based IDs make it idempotent, and it never
deletes) — which is exactly why the hole sat unnoticed in replace mode.

**The fix** — `store_facts()`, `ingest.py`:
- before clearing, count what is stored for that source and compare it with what
  is about to be written;
- if the new count is below `MIN_SURVIVAL_RATIO` (default **0.8**, env
  `QDRANT_MIN_SURVIVAL`), **REFUSE**: nothing is deleted, the reason is printed
  with both counts and the percentage, and the run stores 0 facts;
- `--force-shrink` (or `QDRANT_FORCE_SHRINK=1`) is the escape hatch for a source
  that genuinely shrank; it prints a warning with the before/after counts;
- `--force-shrink` is stripped from argv like `--replace`, and forwarded to the
  child process in `--sequential` mode (the gate runs in the subprocess).

**Verified** 2026-09-23 against the live collection, without touching it:
`QDRANT_CHANGELOG=<tiny file> ingest.py changelog --replace` printed
`REFUSED: source=changelog would shrink from 7058 to 2 facts (0% ...)` and the
source still held **7058** points afterwards. The normal path was verified too —
`ingest.py infrastructure` replaced its 2 facts as before (no shrink, so the gate
passes). `--force-shrink` parsing was verified by
`ingest.py --force-shrink <bogus>`, which named the bogus source and not the flag.

Not covered: the `--force-shrink` path is deliberately untested end-to-end. The
only way to exercise it against the live collection is to actually delete a
source, which is the exact outcome the gate exists to prevent.

## 2026-08-10 06:20 — English-only codebase + squash to single commit
- Translated the whole codebase to English (zero Polish chars in tracked files): qdrant-agent-memory-tool.py, ingest.py, mcp_server.py, secret_guard.py, datetime_utils.py, opencode TS plugin, both SKILL.md files, CHANGELOG.md, SECURITY.md, install.sh, .env.example, .gitignore, requirements.txt
- `secret_guard.py`: redaction placeholder changed from the old localized marker to `[REDACTED]`
- Squashed history to a single commit (`Initial release`, root commit via `git checkout --orphan`); removed `.mailmap` (sole old-author mapping — obsolete after squash)
- Force-pushed: `91e6349` → `6b8ffca` (forced update); verified 1 commit on origin, tree clean, zero old-author references / zero Polish chars

## 2026-08-10 01:52 — QDRANT_RUNNER: global uv instead of venv + fix for opencode loading
- `agents/opencode/qdrant-agent-memory.ts`: added `QDRANT_RUNNER` (env override; default venv python). All calls rewritten from strings to argument arrays (Bun Shell spreads arrays — the old `${cmd}` strings would NEVER have worked, because Bun escapes the whole string as a single argument)
- `agents/opencode/qdrant-agent-memory.ts`: `runQdrant()` helper — RUNNER (array) + TOOL + args (array)
- `skills/qdrant-agent-memory/SKILL.md`: `PY` picks `QDRANT_RUNNER` (e.g. `uv run --project ... --quiet`) or falls back to `$QDIR/venv/bin/python`; note about invoking `$PY` without quotes (multi-word)
- `.gitignore`: entry `agents/opencode/node_modules` — local symlink of the server into opencode node_modules (VPS path, DO NOT commit)

## 2026-08-09 22:15 — Full rename to the qdrant-agent-memory prefix
- All file and integration names under one name `qdrant-agent-memory` (easy for humans to find):
  - `qdrant-tool.py` → `qdrant-agent-memory-tool.py`
  - `skills/qdrant/` → `skills/qdrant-agent-memory/` (skill `qdrant` → `qdrant-agent-memory`)
  - `agents/hermes/qdrant-rag/` → `agents/hermes/qdrant-agent-memory/` (skill `qdrant-rag` → `qdrant-agent-memory`)
  - `agents/opencode/qdrant.ts` → `agents/opencode/qdrant-agent-memory.ts`
  - opencode tools: `qdrantSearch`…`qdrantBackup` → `qdrantAgentMemorySearch`…`qdrantAgentMemoryBackup`
- `.claude-plugin/plugin.json`: `skills: ["qdrant"]` → `["qdrant-agent-memory"]`
- `pyproject.toml`: `name = "qdrant-memory"` → `"qdrant-agent-memory"`

## 2026-08-09 21:35 — qdrant-agent-memory: 18 operations in a single tool + rename
- All operations in ONE tool `qdrant-agent-memory-tool.py` — 18 subcommands (search, store, show, stats, sources, list-source, find-by-file, edit, edit-payload, update-vector, reindex-source, find-dupes, dedupe, delete-id, delete-source, delete-text, delete-fragment, backup)
- Removed separate scripts `qdrant_store.py` / `qdrant_search.py` (everything in `qdrant-agent-memory-tool.py`)
- Added `backup` operation — export the whole collection to JSON (safe copy before cleanup)
- Renamed to `qdrant-agent-memory` (plugin + opencode/Claude Code/Hermes integrations)
- Portable paths (configurable via env: QDRANT_MEMORY_DIR, QDRANT_VENV_PYTHON, QDRANT_URL, QDRANT_API_KEY, COLLECTION_NAME)
- `secret_guard.py` — secret scan and redaction before store (placeholder leak list)

## 2026-08-10 02:07 — fix: delete-fragment --yes
- `qdrant-agent-memory-tool.py`: `delete-fragment --yes` deletes WITHOUT an extra stdin confirmation (previously `_confirm()` asked despite the flag → EOF in non-interactive mode, e.g. opencode/Claude). Backup is still taken before deletion.
- Test: store + delete-fragment --yes → "Deleted 1 points (backup done)"; verified 0 matches; commit ff47d80 pushed to GH
