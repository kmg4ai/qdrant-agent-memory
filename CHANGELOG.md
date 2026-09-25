# Changelog

## 2026-09-25 18:31 — Rules retrieve 4% → 50%: give them one shared timestamp

**The fix, and why it is the right one.** Rules are **timeless**, but their
`ts_epoch` came from the rules file's *modification time* (June 2026) or from a
stray date in the text. A `-v2` collection weights freshness at **28% of every
similarity score**, so a rule in a file nobody had touched since June lost to a
rule in a file saved that morning — with no relation to which rule answered the
question.

All rules now get **the same timestamp** (the moment of ingest). That makes the
freshness term a **constant** across rules, so it stops discriminating between
them and ranking reverts to comparing text alone.

**Measured through the real `search()`, same 28 queries:**

| | before | after |
|---|---|---|
| cosine only | 4% | **50%** |
| + reranker | 4% | 43% |

**50% is exactly what the offline 384-dimension measurement predicted** — which
is the confirmation, not a coincidence: with the freshness term constant, the
ranking is pure content similarity.

**What this does NOT do:** it leaves the 28% weight alone. That weight does real
work for the changelog, where a date is a fact about the world. The fix only
stops *file-edit recency* being mistaken for *rule relevance*.

**Caveat, stated in the code:** rules must be re-ingested after edits, or they
drift back into looking stale. That is already the documented workflow.

### `content_ts()` no longer takes a date from anywhere in the text

It took `max()` of every `20xx-xx-xx` in the body, so a date *mentioned* in the
content beat the entry's real header date: a fact starting `2026-08-27 23:12:`
was dated `2026-08-28`, and a cron example in a package README dated a fact
`2010-01-12`. It now reads a date **only from the start of the text**.

Scope measured before changing anything: **183 of 8639 points (2.1%)**, every one
of them a correction of this bug. Note the fix is in the code but **not yet in
the stored vectors** for those 183 — applying it needs a re-ingest of their
sources, and `changelog --replace` is correctly refused by the shrink gate (the
file holds 3106 entries against 7094 stored points, so it would delete ~4000).

### On the disagreement that produced this

I first proposed a **separate 384-dim collection without time features**. That
would have worked but costs a second collection plus routing in the tool, the
skill and MCP. The owner pushed back — *"I want to find newer information, so I
need that tag"* — and was right that the timestamp is not the problem. Chasing
the objection produced a ~3-line fix with the same measured outcome. The heavier
proposal was premature.

## 2026-09-25 14:33 — A `-v2` collection cannot un-rank freshness; measured on rules

**The finding.** Rules retrieval on the rules source measured **4% hit@5 (1/28)**.
Isolating the cause, offline against the same 265 facts with the same model:

| vector compared | hit@5 |
|---|---|
| embedding only — 384 dims | **50%** |
| embedding **+ the 8 time features** — 392 dims, as stored | **21%** |
| + the explicit decay multiplier — default `search` | **4%** |

So the loss is **29 pp from the time features baked into the vector** and **17 pp
more from the decay multiplier** — 50% down to 4%.

**Why it matters.** `--all` only skips the *multiplier*. The time signal also
lives **inside the vector**, so `search --all` cannot recover more than 21%.
For content where freshness is not a signal — coding rules, instructions,
reference material — the `-v2` design taxes every query, and the tax is not
optional.

**A claim of mine this corrects.** I earlier reported that "the English reranker
ranks Polish rules poorly" and called it a hypothesis. It was **wrong**: the
paired measurement shows the reranker is **neutral** on rules (1 win / 1 loss,
p = 1.0000, and 8/28 vs 6/28 without decay, p = 0.7539). The culprit was the
time signal, not the reranker. Had I not measured it, a working component would
have been blamed and possibly removed.

**Also in this change:** `find-dupes`' two leftover example facts deleted
(backup `backups/20260925_134741-delete-id.json`); the printed score fixed — it
is now `rank=0.703 [rerank=0.70 cos=0.444]` instead of a bare normalised number
that always read ≈1.0 for the top hit and meant nothing.

**Proposed, not done:** timeless content belongs in a collection **without** time
features (the tool already supports 384-dim collections — `setup` creates either).
That is the only way to recover the full 50%.

## 2026-09-25 13:41 — Code is 1:1 with the public repo; all machine paths in `.env`

**`ingest.py` no longer names a single machine path.** The four remaining
defaults are gone:

```diff
-CHANGELOG_PATH = os.getenv("QDRANT_CHANGELOG", "/root/CHANGELOG.md")
-WWW_ROOT       = os.getenv("QDRANT_WWW_ROOT",  "/var/www")
-NGINX_DIR      = os.getenv("QDRANT_NGINX_DIR", "/etc/nginx/sites-enabled")
-SYSTEMD_DIR    = os.getenv("QDRANT_SYSTEMD_DIR","/etc/systemd/system")
+CHANGELOG_PATH = os.getenv("QDRANT_CHANGELOG", "")
+WWW_ROOT       = os.getenv("QDRANT_WWW_ROOT", "").strip()
+NGINX_DIR      = os.getenv("QDRANT_NGINX_DIR", "").strip()
+SYSTEMD_DIR    = os.getenv("QDRANT_SYSTEMD_DIR", "").strip()
```

Also fixed: `ingest_instructions()` derived the project name with
`f.split("/var/www/")[1]` — the root hardcoded a second time, which broke the
moment `QDRANT_WWW_ROOT` pointed anywhere else. It now uses
`os.path.relpath(f, WWW_ROOT).split(os.sep)[0]`.

**Guards, so an empty path cannot fail silently or crash.** `_require_path()`
makes each source say which variable is missing and store **zero** facts.
Verified both directions: with `.env` configured every source resolves; with all
variables blanked — a fresh clone — all six print their hint and store nothing.

**`search --source <name>` added.** Measured problem: in a corpus where one
source dominates (here ~85% changelog), a minority source is pushed outside the
top-N and is effectively unreachable. Filtering by `source` (which *is* indexed)
fixes reachability. Note `file_path` has no index, so a filter on it is rejected
by Qdrant — `source` is the one that works.

### Two problems found while testing — not fixed here

1. **The printed `score` stopped meaning anything.** Since the reranker
   integration normalises candidate scores with min-max, the top hit always
   prints ≈1.0 × decay. A query about Python semicolons printed `0.994` for a
   rule about `git push` — that is rank 1, not a 99% match. The number now
   misleads; printing the cosine score alongside would restore interpretation.
2. **The English reranker ranks Polish rules poorly.** Same query, restricted to
   the rules source: the top hits were PRIORYTET 14 (git push), PRIORYTET 9
   (plan mode) and PRIORYTET 12 (reporting) — nothing about Python. The reranker
   measured well on the changelog corpus; it appears to hurt on this subset.
   Hypothesis, not a conclusion — needs the same paired measurement that the
   changelog corpus got.

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
