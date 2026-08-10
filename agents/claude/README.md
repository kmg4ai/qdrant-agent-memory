# Claude Code — qdrant-agent-memory plugin

One plugin, all 18 operations. Installs via the Claude Code plugin system
(skills-based — **no MCP server, no network daemon**).

## Install (recommended — plugin marketplace)

```bash
# 1. Add the marketplace (once)
claude plugin marketplace add https://github.com/kmg4ai/qdrant-agent-memory.git

# 2. Install the plugin
claude plugin install qdrant-agent-memory@qdrant-agent-memory
```

## Install (manual — copy the skill)

```bash
mkdir -p ~/.claude/skills/qdrant-agent-memory
cp skills/qdrant-agent-memory/SKILL.md ~/.claude/skills/qdrant-agent-memory/
```

## Configure

The skill reads the Qdrant credentials from `$QDIR/.env`. Set the install
location (defaults to `$HOME/qdrant-agent-memory`):

```bash
# in your shell profile
export QDRANT_MEMORY_DIR="$HOME/qdrant-agent-memory"
```

Or set it per-repo in `.claude/settings.json`:
```json
{
  "env": {
    "QDRANT_MEMORY_DIR": "$HOME/qdrant-agent-memory"
  }
}
```

Then install the Python part (once):
```bash
bash scripts/install.sh   # creates venv, installs deps, copies .env.example → .env
# → edit .env: QDRANT_URL, QDRANT_API_KEY, COLLECTION_NAME
```

## What you get

The `qdrant` skill exposes the full command set — search, store, stats,
sources, list-source, find-by-file, edit, edit-payload, update-vector,
reindex-source, find-dupes, dedupe, delete-id, delete-source, delete-text,
delete-fragment.

The model reads the skill on demand and runs the commands through the venv
python — pay-per-use, no token subscription.
