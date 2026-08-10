import { tool } from "@opencode-ai/plugin"

// ── Configuration: adjust to your qdrant-agent-memory install ────────────────
// Default: $HOME/qdrant-agent-memory with venv (python3). Override via env:
//   QDRANT_MEMORY_DIR      — qdrant-agent-memory install directory
//   QDRANT_VENV_PYTHON     — full path to the venv python
//   QDRANT_RUNNER          — full command that runs the tool (e.g.
//                            "uv run --project /path --quiet python3")
//                            Instead of venv — uses global uv/pnpm.
// ─────────────────────────────────────────────────────────────────────────
const home = process.env.HOME ?? "/root"
const DIR = process.env.QDRANT_MEMORY_DIR ?? `${home}/qdrant-agent-memory`
const VENV_PYTHON =
  process.env.QDRANT_VENV_PYTHON ?? `${DIR}/venv/bin/python3`
// RUNNER is an array of words — Bun Shell spreads arrays into separate args.
// Default: venv python. Overridden by QDRANT_RUNNER (e.g. uv run ...).
const RUNNER = (process.env.QDRANT_RUNNER ?? VENV_PYTHON).trim().split(/\s+/)
const TOOL = `${DIR}/qdrant-agent-memory-tool.py`

// Tool invocation: RUNNER (array) + TOOL + args (array).
// Bun Shell escapes each element individually — no injection.
function runQdrant(args: string[]): Promise<string> {
  return Bun.$`${RUNNER} ${TOOL} ${args}`.text()
}

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

export const qdrantAgentMemoryShow = tool({
  description:
    "Show full details of a single Qdrant point by ID: payload, metadata, dates.",
  args: {
    id: tool.schema
      .union([tool.schema.number(), tool.schema.string()])
      .describe("Point ID to inspect (integer or string ID)"),
  },
  async execute(args) {
    return runQdrant(["show", JSON.stringify(String(args.id))])
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

export const qdrantAgentMemoryListSources = tool({
  description: "List all distinct source identifiers stored in the Qdrant collection.",
  args: {},
  async execute() {
    return runQdrant(["sources"])
  },
})

export const qdrantAgentMemoryListSource = tool({
  description:
    "List entries from a specific source in the Qdrant collection, with their dates.",
  args: {
    source: tool.schema.string().describe("Source identifier (e.g., changelog, session)"),
    limit: tool.schema
      .number()
      .optional()
      .default(20)
      .describe("Maximum number of entries to show"),
  },
  async execute(args) {
    const limit = args.limit ?? 20
    return runQdrant(["list-source", args.source, String(limit)])
  },
})

export const qdrantAgentMemoryFindByFile = tool({
  description:
    "Find Qdrant points tied to a specific file path (payload file_path), grouped by date.",
  args: {
    path: tool.schema.string().describe("File path to find points for (e.g. /etc/nginx/nginx.conf)"),
  },
  async execute(args) {
    return runQdrant(["find-by-file", args.path])
  },
})

export const qdrantAgentMemoryEdit = tool({
  description:
    "Edit the text of a Qdrant point and recompute its vector. " +
    "Optionally provide new text; without it, just re-embeds existing text.",
  args: {
    id: tool.schema
      .union([tool.schema.number(), tool.schema.string()])
      .describe("Point ID to edit (integer or string ID)"),
    text: tool.schema
      .string()
      .optional()
      .describe("New text to replace the point content"),
  },
  async execute(args) {
    const cmd = ["edit", JSON.stringify(String(args.id))]
    if (args.text) cmd.push("--text", JSON.stringify(args.text))
    return runQdrant(cmd)
  },
})

export const qdrantAgentMemoryEditPayload = tool({
  description:
    "Edit only the metadata (payload key=value pairs) of a Qdrant point, without re-embedding text.",
  args: {
    id: tool.schema
      .union([tool.schema.number(), tool.schema.string()])
      .describe("Point ID to edit (integer or string ID)"),
    pairs: tool.schema
      .array(tool.schema.string())
      .describe('Metadata to set as key=value strings, e.g. ["source=vps-docs", "priority=high"]'),
  },
  async execute(args) {
    const cmd = ["edit-payload", JSON.stringify(String(args.id))]
    for (const p of args.pairs ?? []) cmd.push(`"${p}"`)
    return runQdrant(cmd)
  },
})

export const qdrantAgentMemoryUpdateVector = tool({
  description:
    "Recompute the embedding vector of an existing Qdrant point from its stored text. " +
    "Use after fixing text, or to match a new embedding model.",
  args: {
    id: tool.schema
      .union([tool.schema.number(), tool.schema.string()])
      .describe("Point ID to re-embed (integer or string ID)"),
  },
  async execute(args) {
    return runQdrant(["update-vector", JSON.stringify(String(args.id))])
  },
})

export const qdrantAgentMemoryReindexSource = tool({
  description:
    "Recompute embedding vectors of ALL points from a source. " +
    "Backup is taken automatically first.",
  args: {
    source: tool.schema.string().describe("Source identifier to reindex (e.g. changelog, vps-docs)"),
  },
  async execute(args) {
    return runQdrant(["reindex-source", args.source])
  },
})

export const qdrantAgentMemoryFindDupes = tool({
  description:
    "Show duplicate points in the Qdrant collection (with dates), without deleting. " +
    "Use before qdrantAgentMemoryDedupe to preview what would be removed.",
  args: {},
  async execute() {
    return runQdrant(["find-dupes"])
  },
})

export const qdrantAgentMemoryDedupe = tool({
  description:
    "Find and remove duplicate points in the Qdrant collection. " +
    "Keeps the newest version (by created_at/date/content). " +
    "Requires confirm=true to execute; backup is taken automatically before removal.",
  args: {
    confirm: tool.schema
      .boolean()
      .optional()
      .default(false)
      .describe("Must be true to actually remove duplicates (safe default false)"),
  },
  async execute(args) {
    if (args.confirm !== true) {
      const preview = await runQdrant(["find-dupes"])
      return "Duplicate preview (remove only after confirming with confirm=true):\n" + preview.trim()
    }
    return runQdrant(["dedupe"])
  },
})

export const qdrantAgentMemoryDeleteId = tool({
  description:
    "Delete a specific point from the Qdrant collection by its ID. " +
    "Requires confirm=true to actually delete; backup is ALWAYS taken first.",
  args: {
    id: tool.schema
      .union([tool.schema.number(), tool.schema.string()])
      .describe("Point ID to delete (integer or string ID)"),
    confirm: tool.schema
      .boolean()
      .optional()
      .default(false)
      .describe("Must be true to actually delete (safe default false)"),
  },
  async execute(args) {
    if (args.confirm !== true) {
      const preview = await runQdrant(["show", JSON.stringify(String(args.id))])
      return "Point preview (delete with confirm=true):\n" + preview.trim()
    }
    return runQdrant(["delete-id", JSON.stringify(String(args.id))])
  },
})

export const qdrantAgentMemoryDeleteSource = tool({
  description:
    "Delete ALL entries for a source from the Qdrant collection. " +
    "Requires confirm=true to actually delete; otherwise returns what would be deleted.",
  args: {
    source: tool.schema.string().describe("Source identifier to delete"),
    confirm: tool.schema
      .boolean()
      .optional()
      .default(false)
      .describe("Must be true to actually delete (safe default false)"),
  },
  async execute(args) {
    if (args.confirm !== true) {
      return `Confirm deletion of source '${args.source}' with confirm=true. ` +
        `This will delete ALL points of this source and is irreversible.`
    }
    return runQdrant(["delete-source", args.source])
  },
})

export const qdrantAgentMemoryDeleteText = tool({
  description:
    "Delete points whose text contains a given fragment (case-insensitive). " +
    "Requires confirm=true to actually delete; otherwise returns the matches that would be removed.",
  args: {
    text: tool.schema.string().describe("Text fragment to match against point content"),
    confirm: tool.schema
      .boolean()
      .optional()
      .default(false)
      .describe("Must be true to actually delete (safe default false)"),
  },
  async execute(args) {
    if (args.confirm !== true) {
      return `Confirm deletion of points containing '${args.text}' with confirm=true. ` +
        `First see the matches: run delete-text with the text argument (answer N to the prompt).`
    }
    // delete-text reads confirmation from stdin — inject 't'
    const result = await Bun.$`printf 't\n' | ${RUNNER} ${TOOL} delete-text ${JSON.stringify(args.text)}`.text()
    return result.trim()
  },
})

export const qdrantAgentMemoryDeleteFragment = tool({
  description:
    "Delete points matching a text fragment and/or regex pattern. " +
    "Shows matches (fragment vs regex separately) before deleting. " +
    "Requires confirm=true to actually delete; backup is ALWAYS taken first.",
  args: {
    text: tool.schema
      .string()
      .optional()
      .describe("Text fragment (case-insensitive substring) to match against point text"),
    regex: tool.schema
      .string()
      .optional()
      .describe("Regex pattern to match against point text (re.search)"),
    source: tool.schema
      .string()
      .optional()
      .describe("Only consider points from this source (e.g. changelog, vps-docs)"),
    confirm: tool.schema
      .boolean()
      .optional()
      .default(false)
      .describe("Must be true to actually delete (safe default false)"),
  },
  async execute(args) {
    const confirm = args.confirm === true
    const yesFlag = confirm ? "--yes" : "--dry-run"
    const cmd = ["delete-fragment"]
    if (args.text) cmd.push(args.text)
    if (args.regex) cmd.push("--regex", args.regex)
    if (args.source) cmd.push("--source", args.source)
    cmd.push(yesFlag)
    const result = await runQdrant(cmd)
    const label = confirm
      ? "Deletion result (backup taken)"
      : "Match preview (delete with confirm=true)"
    return `${label}:\n` + result.trim()
  },
})

export const qdrantAgentMemoryBackup = tool({
  description:
    "Export the entire Qdrant collection to a JSON backup file. " +
    "Use as a safety copy before manual cleanup or dedupe.",
  args: {
    path: tool.schema
      .string()
      .optional()
      .describe("Output file path (default: backups/<timestamp>-backup.json)"),
  },
  async execute(args) {
    const cmd = ["backup"]
    if (args.path) cmd.push(args.path)
    return runQdrant(cmd)
  },
})
