#!/usr/bin/env python3
"""MCP server: all 18 qdrant-agent-memory operations as MCP tools (optional layer).

This is OPTIONAL — the default integration is a single tool `qdrant-agent-memory-tool.py`
(18 operations) through the plugin/SKILL (no MCP). This server exposes the same
functionality over MCP for users who prefer/need the MCP standard.

Tools (prefix `qdrant-agent-memory_`):
  qdrant-agent-memory_search           — semantic search
  qdrant-agent-memory_store            — store memory
  qdrant-agent-memory_backup           — export whole collection (JSON)
  qdrant-agent-memory_show             — view point by ID
  qdrant-agent-memory_stats            — stats per source
  qdrant-agent-memory_list_source      — entries of a source
  qdrant-agent-memory_find_by_file     — points of a file (with dates)
  qdrant-agent-memory_edit             — edit text + recompute vector
  qdrant-agent-memory_edit_payload     — edit metadata only
  qdrant-agent-memory_update_vector    — recompute vector from existing text
  qdrant-agent-memory_reindex_source   — recompute vectors of a whole source (backup)
  qdrant-agent-memory_find_dupes       — show duplicates
  qdrant-agent-memory_dedupe           — remove duplicates (newest kept)
  qdrant-agent-memory_delete_id        — delete point by ID (backup)
  qdrant-agent-memory_delete_source    — delete whole source
  qdrant-agent-memory_delete_text      — delete by text fragment
  qdrant-agent-memory_delete_fragment  — delete by fragment and/or regex
  qdrant-agent-memory_sources          — list sources

Run (stdio):
  # via uv (recommended, portable):
  uv run --project <install-dir> --quiet mcp_server.py
  # or via the same python as the install (venv):
  <python-with-qdrant_client> mcp_server.py

Runner used to invoke the tool: env var QDRANT_RUNNER (e.g. "uv run --project /path --quiet python"),
fallback: sys.executable (the python running this server).
"""

import os
import subprocess
import sys
from pathlib import Path

from fastmcp import FastMCP

DIR = Path(__file__).resolve().parent
TOOL = DIR / "qdrant-agent-memory-tool.py"

# Runner: QDRANT_RUNNER (e.g. "uv run --project ... --quiet python") or the same python as the server.
RUNNER = os.getenv("QDRANT_RUNNER", sys.executable).strip().split()

mcp = FastMCP("qdrant-agent-memory")


def _run(*args, timeout=300):
    """Run the tool via the runner (subprocess), return stdout."""
    cmd = RUNNER + [str(TOOL)] + list(args)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"Error: timeout exceeded ({timeout}s)"
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    if res.returncode != 0:
        return f"Error (rc={res.returncode}): {err or out}"
    return out


# ─── 18 MCP tools (prefix qdrant-agent-memory_) ───────────────────────────────

@mcp.tool()
def qdrant_agent_memory_search(
    query: str, limit: int = 10, since: str | None = None, window_days: int | None = None
) -> str:
    """Semantic search in Qdrant memory. Optional --since YYYY-MM-DD or window_days (e.g. 30)."""
    args = ["search", query, str(limit)]
    if since:
        args += ["--since", since]
    if window_days:
        args += ["--window", f"{window_days}d"]
    return _run(*args)


@mcp.tool()
def qdrant_agent_memory_store(text: str, source: str = "manual") -> str:
    """Store memory in Qdrant (Secret Guard automatically redacts secrets)."""
    return _run("store", text, source)


@mcp.tool()
def qdrant_agent_memory_backup(path: str | None = None) -> str:
    """Export the whole collection to a JSON file. path optional (default backups/)."""
    return _run("backup", path) if path else _run("backup")


@mcp.tool()
def qdrant_agent_memory_show(id: str) -> str:
    """View a point by ID (payload + vector size)."""
    return _run("show", id)


@mcp.tool()
def qdrant_agent_memory_stats() -> str:
    """Collection stats per source (counts, ts_epoch, no ts)."""
    return _run("stats")


@mcp.tool()
def qdrant_agent_memory_list_source(source: str, limit: int = 50) -> str:
    """Entries of a source, with dates. limit defaults to 50."""
    return _run("list-source", source, str(limit))


@mcp.tool()
def qdrant_agent_memory_find_by_file(path: str) -> str:
    """Points tied to a specific file (file_path), with dates."""
    return _run("find-by-file", path)


@mcp.tool()
def qdrant_agent_memory_edit(id: str, text: str | None = None) -> str:
    """Edit point text + recompute vector. text optional (without → just previews)."""
    if text:
        return _run("edit", id, "--text", text)
    return _run("edit", id)


@mcp.tool()
def qdrant_agent_memory_edit_payload(id: str, kv: list[str]) -> str:
    """Edit only metadata (payload) of a point. kv = list of "key=value"."""
    return _run("edit-payload", id, *kv)


@mcp.tool()
def qdrant_agent_memory_update_vector(id: str) -> str:
    """Recompute a point vector from its existing text (e.g. after a model change)."""
    return _run("update-vector", id)


@mcp.tool()
def qdrant_agent_memory_reindex_source(source: str, confirm: bool = False) -> str:
    """Recompute vectors of a whole source (backup taken). Without confirm=True → preview."""
    if not confirm:
        return _run("reindex-source", source) + "\n\n[confirm=true to execute]"
    return _run("reindex-source", source)


@mcp.tool()
def qdrant_agent_memory_find_dupes() -> str:
    """Show duplicate groups (with dates) — no deletion."""
    return _run("find-dupes")


@mcp.tool()
def qdrant_agent_memory_dedupe(confirm: bool = False) -> str:
    """Remove duplicates (newest kept, backup taken). Without confirm=True → preview."""
    if not confirm:
        return _run("find-dupes") + "\n\n[confirm=true to remove]"
    return _run("dedupe")


@mcp.tool()
def qdrant_agent_memory_delete_id(id: str, confirm: bool = False) -> str:
    """Delete a point by ID (backup taken). Without confirm=True → point preview."""
    if not confirm:
        return _run("show", id) + "\n\n[confirm=true to delete]"
    return _run("delete-id", id)


@mcp.tool()
def qdrant_agent_memory_delete_source(source: str, confirm: bool = False) -> str:
    """Delete a whole source. Without confirm=True → confirmation only (irreversible)."""
    if not confirm:
        return f"Confirm deletion of source '{source}' with confirm=True. This will delete ALL points."
    return _run("delete-source", source)


@mcp.tool()
def qdrant_agent_memory_delete_text(text: str, confirm: bool = False) -> str:
    """Delete points containing a text fragment. Without confirm=True → preview of matches."""
    if not confirm:
        return _run("delete-text", text) + "\n\n[confirm=true to delete]"
    return _run("delete-text", text)


@mcp.tool()
def qdrant_agent_memory_delete_fragment(
    text: str | None = None,
    regex: str | None = None,
    source: str | None = None,
    confirm: bool = False,
) -> str:
    """Delete points matching a fragment and/or regex (--source optional). Without confirm → dry-run."""
    args = ["delete-fragment"]
    if text:
        args += [text]
    if regex:
        args += ["--regex", regex]
    if source:
        args += ["--source", source]
    args += ["--yes"] if confirm else ["--dry-run"]
    return _run(*args)


@mcp.tool()
def qdrant_agent_memory_sources() -> str:
    """List all sources in the collection."""
    return _run("sources")


if __name__ == "__main__":
    mcp.run(transport="stdio")
