# 🧠 qdrant-agent-memory

**Semantic memory for AI coding agents** — a persistent vector memory built on [Qdrant](https://qdrant.tech) that works across **opencode**, **Claude Code**, and **Hermes** (or any agent that can run a shell command).

Your agents forget between sessions. This tool gives them a shared, searchable memory: store decisions, facts, configs, and lessons once — retrieve them semantically in any future session, from any agent.

> No MCP server required. Just Python scripts + a Qdrant collection.

## ✨ Features

- **Semantic search** — find past knowledge by *meaning*, not by grep
- **Time-aware vectors** — optional `-v2` collection ranks fresher memories higher and filters by date
- **Full CRUD on memory** — view, edit, re-embed, dedupe, delete (with automatic backups)
- **Secret guard** — patterns that scrub API keys, passwords, and tokens *before* anything is written to Qdrant
- **Multi-agent** — one memory, three agents (opencode / Claude Code / Hermes) + drop-in for any shell-capable agent
- **RAM-safe bulk ingest** — batch embedding + `gc.collect()` for low-RAM VPSes

## 🚀 Quick start

```bash
# 1. Clone + install
git clone https://github.com/kmg4ai/qdrant-agent-memory.git
cd qdrant-agent-memory
python -m venv venv
venv/bin/pip install -r requirements.txt

# 2. Configure (Qdrant Cloud or local docker)
cp .env.example .env
#  → fill in QDRANT_URL, QDRANT_API_KEY, COLLECTION_NAME
#    (use a name ending in -v2 for time-aware vectors, e.g. my-memory-v2)

# 2b. Create the collection (first run only)
venv/bin/python qdrant-agent-memory-tool.py setup

# 3. Store a memory
venv/bin/python qdrant-agent-memory-tool.py store "The web dashboard uses Argon2id auth, configured in the server's nginx auth file" "infrastructure"

# 4. Search your memory
venv/bin/python qdrant-agent-memory-tool.py search "how is nginx auth configured"
```

## 📚 CLI reference

| Command | Description |
|---|---|
| `search "<query>" [limit]` | Semantic search (top-5 by default) |
| `search "<query>" --all` | Search without time-decay ranking |
| `search "<query>" --since 2026-07-01` | Only memories from that date onward |
| `search "<query>" --window 30d` | Only memories from the last 30 days |
| `store "<text>" "<source>"` | Save a memory |
| `setup [name]` | Create the Qdrant collection (dim 392 for `-v2`, else 384) |
| `show <id>` | Full detail of one memory point |
| `stats` | Point counts per source |
| `sources` | List all source identifiers |
| `list-source <source> [limit]` | Entries of one source |
| `find-by-file "<path>"` | Points tied to a file path |
| `edit <id> --text "new text"` | Change text + recompute vector |
| `edit-payload <id> key=val ...` | Update only metadata |
| `update-vector <id>` | Re-embed existing text |
| `reindex-source <source>` | Re-embed all points of a source (backup first) |
| `find-dupes` | Show duplicates (with dates) |
| `dedupe` | Remove duplicates, keep newest (backup first) |
| `delete-id <id>` | Delete one point (backup first) |
| `delete-source <source>` | Delete a whole source |
| `delete-text "<fragment>"` | Delete points containing text (confirms) |
| `delete-fragment "<text>" [--regex ...] [--source ...] [--yes]` | Delete by fragment and/or regex |

Destructive commands take automatic **backups** (stored in `backups/`) before they delete anything.

## 🤖 Agent integrations

Each agent gets the **same 18 operations** through its native mechanism — no MCP involved:

| Agent | Mechanism | Location |
|---|---|---|
| **opencode** | Native tools (`qdrantAgentMemorySearch`…`qdrantAgentMemoryDeleteFragment`) | `agents/opencode/qdrant-agent-memory.ts` |
| **Claude Code** | Plugin `qdrant-agent-memory` (skill, 18 operations) | `skills/qdrant-agent-memory/SKILL.md` + `.claude-plugin/` |
| **Hermes** | Skill `qdrant-agent-memory` | `agents/hermes/qdrant-agent-memory/SKILL.md` |

### opencode

Copy `agents/opencode/qdrant-agent-memory.ts` to `~/.config/opencode/tools/` and set the
install location at the top (`VENV_PYTHON`, `DIR`, or via `QDRANT_VENV_PYTHON`
/ `QDRANT_MEMORY_DIR` env vars). Restart opencode — the tools register automatically.

### Claude Code

```bash
# Option A — plugin (recommended)
claude plugin marketplace add https://github.com/kmg4ai/qdrant-agent-memory.git
claude plugin install qdrant-agent-memory@qdrant-agent-memory

# Option B — manual skill copy
mkdir -p ~/.claude/skills/qdrant-agent-memory
cp skills/qdrant-agent-memory/SKILL.md ~/.claude/skills/qdrant-agent-memory/
```

See [`agents/claude/README.md`](agents/claude/README.md) for full instructions.

### Hermes

```bash
mkdir -p ~/.hermes/skills/software-development/qdrant-agent-memory
cp agents/hermes/qdrant-agent-memory/SKILL.md ~/.hermes/skills/software-development/qdrant-agent-memory/
```

All integrations read the install location from `QDRANT_MEMORY_DIR`
(default `$HOME/qdrant-agent-memory`) and credentials from `.env` — the same memory
is shared across every agent.

### Optional: MCP server (for the adventurous)

The default integrations are **no-MCP** by design — one tool, 18 operations, works
everywhere with just Python. If you prefer (or need) the standard **MCP** protocol,
an optional MCP server exposes the **same 18 operations** with a `qdrant-agent-memory_`
prefix.

```bash
# 1. Install deps (uv recommended; fastmcp included)
uv sync --project /path/to/qdrant-agent-memory

# 2. Run the MCP server (stdio)
uv run --project /path/to/qdrant-agent-memory --quiet mcp_server.py
```

Register it as a stdio MCP server (e.g. Claude Code, `~/.claude.json`):

```json
{
  "mcpServers": {
    "qdrant-agent-memory": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/path/to/qdrant-agent-memory", "--quiet", "mcp_server.py"]
    }
  }
}
```

Available tools (prefix `qdrant-agent-memory_`): `search`, `store`, `backup`,
`show`, `stats`, `list_source`, `find_by_file`, `edit`, `edit_payload`,
`update_vector`, `reindex_source`, `find_dupes`, `dedupe`, `delete_id`,
`delete_source`, `delete_text`, `delete_fragment`, `sources`.

Destructive tools (`dedupe`, `delete_*`, `reindex_source`) require `confirm=true`
— otherwise they return a preview / dry-run. Backups are always taken before deletion.

> This is an **optional** layer. The skill/plugin integrations above give you the
> full 18 operations with no MCP server — pick whichever fits your setup.

## 📥 Bulk ingest from your machine

`ingest.py` indexes your own knowledge files (VPS doc, changelog, nginx/systemd configs, project READMEs):

```bash
venv/bin/python ingest.py                       # all sources
venv/bin/python ingest.py vps-docs              # one source
venv/bin/python ingest.py changelog --replace   # full re-ingest
venv/bin/python ingest.py --sequential          # each source in its own process (low-RAM friendly)
```

Paths are configurable via env (`QDRANT_VPS_DOC`, `QDRANT_CHANGELOG`, `QDRANT_WWW_ROOT`, `QDRANT_NGINX_DIR`, `QDRANT_SYSTEMD_DIR`).

## 🔒 Security

- `.env` is gitignored — only `.env.example` (placeholders) is committed
- **Secret Guard** (`secret_guard.py`) scrubs keys/passwords/tokens from any text before it reaches Qdrant
- See [`docs/SECURITY.md`](docs/SECURITY.md) for details

## 🤖 Built with

This project was built with a team of AI coding agents:

| Agent | Used for |
|-------|----------|
| [OpenCode](https://opencode.ai) | Main development, orchestration |
| [DeepSeek V4 Flash](https://deepseek.com) | Primary coding model |
| [Claude Code](https://www.anthropic.com/claude-code) | Secondary coding |
| [Hermes](https://github.com/NousResearch/hermes-agent) | Bug detection & fixes (find and fix bugs) |
| [DeepClaude](https://github.com/aattaran/deepclaude) | Claude Code with DeepSeek backend |
| [Superpowers](https://github.com/obra/superpowers) | Skill framework |
| [Qdrant](https://qdrant.tech) | Vector database |

**Plugins used:** opencode-vision, agentic-security.

## 📄 License

MIT — see [LICENSE](LICENSE).
