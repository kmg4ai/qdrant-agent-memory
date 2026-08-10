# Changelog

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
