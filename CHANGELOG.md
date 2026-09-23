# Changelog

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
