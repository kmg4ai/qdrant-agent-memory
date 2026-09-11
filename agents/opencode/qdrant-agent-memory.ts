import { tool } from "@opencode-ai/plugin"
import { existsSync } from "node:fs"

// ── Configuration: adjust to your qdrant-agent-memory install ────────────────
// Default: $HOME/qdrant-agent-memory. Override via env:
//   QDRANT_MEMORY_DIR      — qdrant-agent-memory install directory
//   QDRANT_VENV_PYTHON     — full path to the venv python
//   QDRANT_RUNNER          — full command that runs the tool (e.g.
//                            "uv run --project /path --quiet python3")
// ─────────────────────────────────────────────────────────────────────────
const home = process.env.HOME ?? "/root"
const DIR = process.env.QDRANT_MEMORY_DIR ?? `${home}/qdrant-agent-memory`
const VENV_PYTHON =
  process.env.QDRANT_VENV_PYTHON ?? `${DIR}/venv/bin/python3`
// RUNNER is an array of words — Bun Shell spreads arrays into separate args.
// Prefer venv python IF it exists; otherwise the install uses uv — fall back
// to `uv run --project DIR`. Override entirely via QDRANT_RUNNER.
const defaultRunner = existsSync(VENV_PYTHON)
  ? VENV_PYTHON
  : `uv run --quiet --project ${DIR} python3`
const RUNNER = (process.env.QDRANT_RUNNER ?? defaultRunner).trim().split(/\s+/)
const TOOL = `${DIR}/qdrant-agent-memory-tool.py`

// Tool invocation: RUNNER (array) + TOOL + args (array).
// Bun Shell escapes each element individually — no injection.
function runQdrant(args: string[]): Promise<string> {
  return Bun.$`${RUNNER} ${TOOL} ${args}`.text()
}

// ── Core tools ─────────────────────────────────────────────────────────────
// Te 4 narzędzia są wystarczające na co dzień (RAG search + store + stats).
// Operacje admin (show, edit, dedupe, delete, backup itd.) są dostępne
// przez skill "qdrant-agent-memory" — wywołuje ten sam skrypt przez bash.

export const qdrantAgentMemorySearch = tool({
  description:
    "Search past session memory in Qdrant vector database. " +
    "Returns top-5 semantically similar memories from previous sessions. " +
    "Use this at the start of a new session to get relevant context.",
  args: {
    query: tool.schema
      .string()
      .describe("What to search for in past conversations and session history"),
  },
  async execute(args) {
    return runQdrant(["search", args.query, "5"])
  },
})

export const qdrantAgentMemoryStore = tool({
  description:
    "Store important information in Qdrant vector database for future retrieval. " +
    "Use this when you learn configuration details, project decisions, or solutions to problems.",
  args: {
    text: tool.schema.string().describe("The information to remember for future sessions"),
    source: tool.schema
      .string()
      .optional()
      .describe("Source identifier (e.g., session ID, project name)"),
  },
  async execute(args) {
    const source = args.source ?? "opencode"
    return runQdrant(["store", args.text, source])
  },
})

export const qdrantAgentMemorySearchTemporal = tool({
  description:
    "Search Qdrant memory with time-decay and optional time filter. " +
    "Fresh results rank higher. Use --since for date cutoff or window for recent days.",
  args: {
    query: tool.schema.string().describe("What to search for in past sessions"),
    since: tool.schema
      .string()
      .optional()
      .describe("Only results created on/after this date (YYYY-MM-DD)"),
    window: tool.schema
      .number()
      .optional()
      .describe("Only results from the last N days (e.g. 30)"),
    fresh: tool.schema
      .boolean()
      .optional()
      .default(true)
      .describe("Apply time-decay so fresh results rank higher (default true)"),
  },
  async execute(args) {
    const cmd = ["search", args.query, "10"]
    if (args.fresh === false) cmd.push("--all")
    if (args.since) cmd.push("--since", args.since)
    if (args.window) cmd.push("--window", `${args.window}d`)
    return runQdrant(cmd)
  },
})

export const qdrantAgentMemoryStats = tool({
  description:
    "Show Qdrant collection statistics: total points and count per source, " +
    "plus how many have created_at/date. Useful for auditing what is stored.",
  args: {},
  async execute() {
    return runQdrant(["stats"])
  },
})
