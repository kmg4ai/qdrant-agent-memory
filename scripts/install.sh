#!/usr/bin/env bash
# qdrant-agent-memory — installer: venv + deps + .env + sanity check
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> qdrant-agent-memory install"
echo "==> 1/4 Creating venv..."
python3 -m venv venv

echo "==> 2/4 Installing dependencies..."
venv/bin/pip install --upgrade pip >/dev/null
venv/bin/pip install -r requirements.txt

echo "==> 3/4 Configuring .env..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    Created .env from .env.example — FILL IN: QDRANT_URL, QDRANT_API_KEY, COLLECTION_NAME"
else
  echo "    .env already exists — leaving it"
fi

echo "==> 4/4 Sanity check..."
venv/bin/python -m py_compile qdrant-agent-memory-tool.py secret_guard.py datetime_utils.py ingest.py

echo ""
echo "✅ Installed. Next steps:"
echo "  1. edit .env (QDRANT_URL, QDRANT_API_KEY, COLLECTION_NAME)"
echo "  2. store memory:    venv/bin/python qdrant-agent-memory-tool.py store \"something important\" \"source\""
echo "  3. search:          venv/bin/python qdrant-agent-memory-tool.py search \"what you are looking for\""
echo ""
echo "  Agent integrations: agents/opencode, agents/claude, agents/hermes"
