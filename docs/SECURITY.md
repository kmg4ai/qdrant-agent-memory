# 🔒 Security

## Secrets handling

This project stores **semantic memory for AI agents** — which means it is very
easy for an agent to accidentally persist API keys, passwords, or tokens.

Two layers of protection:

### 1. `secret_guard.py` — scrub before store

Every write path (the `store` operation in `qdrant-agent-memory-tool.py`, plus `ingest.py`)
runs text through `secret_guard.scrub()` **before** it reaches Qdrant. Known
secret shapes are replaced with `[REDACTED]`:

- OpenAI / Anthropic / OpenRouter / DeepSeek / Groq / Qdrant / Cerebras API keys
- `sk-...` style keys, Bearer tokens
- `*_TOKEN=...` env assignments
- `password`/`passwd`/`secret`/`token` followed by a value
- Argon2 (`$argon2...`) and bcrypt (`$2y...`) hashes
- An extendable **blacklist** of values known to have leaked historically
  (edit `_BLACKLIST` in `secret_guard.py` locally — do not commit real secrets)

### 2. Git hygiene

- `.env` is **gitignored**; only `.env.example` (placeholders) is committed.
- Never commit `QDRANT_API_KEY`, `QDRANT_URL`, or `COLLECTION_NAME` values.
- Before any commit, grep the diff:

```bash
git diff | grep -iE "admin-password|admin-token|sk-|api[_-]?key|[A-Za-z0-9_-]{20,}"
```

## Threat model

| Threat | Mitigation |
|---|---|
| Secret leaks into Qdrant via agent writes | `secret_guard` scrubbing on every write path |
| Secret leaks into git | `.gitignore` + commit-time grep |
| Collection compromise | Qdrant Cloud API key in `.env` only; never in code |
| Agents writing garbage | Content-hashed point IDs make re-ingest idempotent |

## Backups

Destructive operations (`dedupe`, `delete-id`, `delete-source`,
`reindex-source`) write a JSON snapshot to `backups/` before modifying the
collection. Keep that directory if you care about accidental data loss.

## Reporting a vulnerability

Open an issue on this repository — do not include real secrets in the report.
