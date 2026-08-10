---
name: qdrant-agent-memory
description: "Work with the Qdrant vector database (semantic memory) — search, store, stats, cleanup. Works WITHOUT MCP: a single tool qdrant-agent-memory-tool.py with 18 operations (including store) via venv python."
version: 1.0.0
author: qdrant-agent-memory
license: MIT
metadata:
  hermes:
    tags: [qdrant, rag, memory, vector-search]
    related_skills: []
---

# Qdrant RAG — semantic memory via a single tool (no MCP)

Qdrant stores semantic memory (documentation, changelog, findings, sessions).
**Do not use MCP** — the single tool `qdrant-agent-memory-tool.py` has ALL 18 operations (including `store`).
Invoke it via venv python from the qdrant-agent-memory install.

## Always

```bash
QDIR="${QDRANT_MEMORY_DIR:-$HOME/qdrant-agent-memory}"   # install directory (override via QDRANT_MEMORY_DIR)
PY="$QDIR/venv/bin/python"
TOOL="$QDIR/qdrant-agent-memory-tool.py"
```

**Note:** system `python3` does NOT have `qdrant_client`/`fastembed` — always use
`$QDIR/venv/bin/python`. Credentials (`QDRANT_URL`, `QDRANT_API_KEY`,
`COLLECTION_NAME`) load from `$QDIR/.env` — do not provide them.

## Search

```bash
"$PY" "$TOOL" search "<query>" [limit]
# for example:
"$PY" "$TOOL" search "how nginx auth is configured" 5
"$PY" "$TOOL" search "deploy" --all              # no time decay
"$PY" "$TOOL" search "deploy" --since 2026-07-01 # only after a date
"$PY" "$TOOL" search "deploy" --window 30d       # only last 30 days
```

Result: `score=` ranking + `source` + content. Always use `search` for similarity search.

## Store new knowledge

```bash
"$PY" "$TOOL" store "<content>" "<source>"
```

- `<source>` = category (e.g. `vps-docs`, `changelog`, `infrastructure`, `session`, `decision`).
- Store findings that should be available in future sessions (decisions, configs, facts).

## Stats / overview

```bash
"$PY" "$TOOL" stats                  # how many points per source
"$PY" "$TOOL" sources                # list of sources
"$PY" "$TOOL" list-source vps-docs   # entries of a source
"$PY" "$TOOL" show <id>              # view a point
```

## Cleanup (carefully — scripts take backups, but verify before deleting)

```bash
"$PY" "$TOOL" backup [file.json]     # export the whole collection (JSON) — do before cleanup
"$PY" "$TOOL" find-dupes             # show duplicates (with dates)
"$PY" "$TOOL" dedupe                 # remove duplicates — newest stays
"$PY" "$TOOL" delete-id <id>         # delete point by ID (backup)
"$PY" "$TOOL" delete-source <src>    # delete whole source
"$PY" "$TOOL" delete-fragment <text> --dry-run   # dry-run before deleting
"$PY" "$TOOL" delete-fragment <text> --yes       # delete by fragment (backup)
```

## Editing

```bash
"$PY" "$TOOL" edit <id> --text "new content"     # text + recompute vector
"$PY" "$TOOL" edit-payload <id> key=val [k=v...] # metadata only (no vector)
"$PY" "$TOOL" update-vector <id>                  # recompute vector from existing text
"$PY" "$TOOL" reindex-source <source>            # recompute vectors of a whole source (backup)
```

## Advanced

```bash
"$PY" "$TOOL" find-by-file "<path>"              # points of a specific file (with dates)
"$PY" "$TOOL" delete-text "<fragment>"           # delete by fragment in content (confirm)
```

## Rules

- **Do not use MCP** — only `$TOOL` (18 operations: search, store, show, stats, sources,
  list-source, find-by-file, edit, edit-payload, update-vector, reindex-source, find-dupes,
  dedupe, delete-id, delete-source, delete-text, delete-fragment, backup).
- Do not store secrets in Qdrant (api keys, passwords, tokens) — remove/replace before storing
  (secret_guard.py does this automatically, but do not rely only on it).
- Before `dedupe`/`delete-*` show what will be deleted (`find-dupes`, `--dry-run`).
