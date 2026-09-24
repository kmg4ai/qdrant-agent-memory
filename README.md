# 🧠 qdrant-agent-memory

**Semantic memory for AI coding agents** — a persistent vector memory built on [Qdrant](https://qdrant.tech) that works across **opencode**, **Claude Code**, and **Hermes** (or any agent that can run a shell command).

Your agents forget between sessions. This tool gives them a shared, searchable memory: store decisions, facts, configs, and lessons once — retrieve them semantically in any future session, from any agent.

> No MCP server required. Just Python scripts + a Qdrant collection.

## ✨ Features

- **Semantic search** — find past knowledge by *meaning*, not by grep
- **Multilingual embeddings** — the default model is `paraphrase-multilingual-MiniLM-L12-v2`; a memory written in Polish is found by a Polish paraphrase (an English-only model returned *higher* similarity scores with *worse* results on a Polish corpus)
- **Cross-encoder reranking** — a second query-time stage re-scores the top 50 candidates; measured **25% → 42%** hit@5 on hard queries (p = 0.0003). Falls back to plain cosine if the model can't load
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
| `search "<query>" [limit]` | Semantic search (top-5 by default), reranked |
| `search "<query>" --all` | Search without time-decay ranking |
| `search "<query>" --since 2026-07-01` | Only memories from that date onward |
| `search "<query>" --window 30d` | Only memories from the last 30 days |
| `search "<query>" --no-rerank` | Skip the reranker (faster, less precise) |
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
| `reindex-all` | Re-embed the **whole collection** — run after changing the embedding model |
| `find-dupes` | Duplicates: raw-md5 count **and** date-normalised count |
| `dedupe` | Remove duplicates, keep newest (backup first) |
| `dedupe --normalize` | Also collapse copies differing only by their date header — **deletes ~27% of a typical corpus; not proven to improve retrieval** |
| `delete-id <id>` | Delete one point (backup first) |
| `delete-source <source>` | Delete a whole source |
| `delete-text "<fragment>"` | Delete points containing text (confirms) |
| `delete-fragment "<text>" [--regex ...] [--source ...] [--yes]` | Delete by fragment and/or regex |

Destructive commands take automatic **backups** (stored in `backups/`) before they delete anything.

## 🔄 Switching models

There are **two independent models**, and they switch differently — because they
sit at different stages of the pipeline:

| | Embedding model | Reranker |
|---|---|---|
| Runs at | **index time** — every point stores its vector | **query time** — reads raw text |
| Switch cost | **full reindex** (vectors are model-specific) | **nothing** — stores no state |
| Same dimension required? | **YES**, else a new collection | no |
| Env var | `QDRANT_EMBED_MODEL` | `QDRANT_RERANK_MODEL` |
| Turn off | — | `QDRANT_RERANK=0` |

**Both are switched with the same tool — no second script is needed.**

### Embedding model (A → B)

Vectors computed by model A mean nothing to model B: the two live in different
spaces, and mixing them makes search return garbage **with no error raised**.
So a switch is two steps — rename, then re-embed:

```bash
# 1. Change the model — one variable, read by both ingest and the tool
echo 'QDRANT_EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2' >> .env

# 2. Re-embed the whole collection (backs up first; idempotent, safe to re-run)
venv/bin/python qdrant-agent-memory-tool.py reindex-all
```

`reindex-all` recomputes every vector from the stored text and **preserves each
point's `ts_epoch`** — the 8 time features are re-derived from the *content date*,
never from "now". (An earlier version reset them to now; that would have collapsed
a dated history into a single instant and permanently broken `--since`/`--window`.)

**The hard constraint is the dimension.** A `-v2` collection is 392-dimensional
(384 embedding + 8 time features). A model with a different output size cannot be
reindexed into it — that needs a new collection and a fresh ingest:

| model | dim | drop-in for 392? |
|---|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | 384 | yes — but **English only** |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 384 | yes — **multilingual (default)** |
| `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | 768 | **no** — new collection |
| `intfloat/multilingual-e5-large` | 1024 | **no** — new collection |

### Reranker (query-time second stage)

A cross-encoder reads query and document **together**, so it ranks more precisely
than cosine alone. It stores nothing, so switching it is one variable:

```bash
QDRANT_RERANK_MODEL=jinaai/jina-reranker-v2-base-multilingual  # bigger, stronger
QDRANT_RERANK=0                                               # disable reranking
QDRANT_RERANK_CANDIDATES=50                                   # recall/rerank tradeoff
```

Per query it fetches `RERANK_CANDIDATES` by cosine, reranks them, normalises the
scores to [0,1] and **then** applies the time decay. If the model cannot be
loaded (weights missing, or too little free RAM) search **falls back to cosine
with a warning** instead of failing.

> **Memory note.** The multilingual reranker needs ~2.4 GB RSS. On a small VPS
> running an OOM daemon you may get SIGTERM'd mid-load. The small English
> `jinaai/jina-reranker-v1-turbo-en` (~0.22 GB RSS) still measured a large gain on
> a **Polish** corpus — try it before reaching for the big one.

### Measured, not assumed

The numbers below come from 104 hand-written **hard** queries: Polish paraphrases
with no lexical overlap with their target document, each verified mechanically
(query/document content-word overlap ≤ 0.34, keyword rare in the corpus).

| pipeline | hit@5 |
|---|---|
| cosine + time decay | 25% |
| **+ reranker** | **42%** (McNemar 21 wins / 3 losses, p = 0.0003) |

`validate_rerank.py` reproduces this measurement; `eval_pl.py` holds a small
built-in set. **After any model change, re-run them before believing it helped** —
a change that looks better on ten queries can reverse on a hundred.

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
