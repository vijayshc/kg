#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
JANUSGRAPH_HOME="$ROOT_DIR/tools/janusgraph-1.1.0"
SERVER_SCRIPT="$JANUSGRAPH_HOME/bin/janusgraph-server.sh"

if [[ ! -x "$SERVER_SCRIPT" ]]; then
  echo "[stop] JanusGraph is not installed; nothing to stop."
  exit 0
fi

echo "[stop] stopping JanusGraph Gremlin Server"
"$SERVER_SCRIPT" stop || true
