#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
JANUSGRAPH_HOME="$ROOT_DIR/tools/janusgraph-1.1.0"
SERVER_SCRIPT="$JANUSGRAPH_HOME/bin/janusgraph-server.sh"

is_ready() {
  ~/anaconda3/bin/python3 - <<'PY'
import socket
s = socket.socket()
s.settimeout(1)
try:
    s.connect(("127.0.0.1", 8182))
    print("ready")
finally:
    s.close()
PY
}

if [[ ! -x "$SERVER_SCRIPT" ]]; then
  echo "[start] JanusGraph is not installed. Run scripts/install_janusgraph_local.sh first." >&2
  exit 1
fi

if is_ready >/dev/null 2>&1; then
  echo "[start] JanusGraph is already accepting connections."
  exit 0
fi

if pgrep -f "org.apache.tinkerpop.gremlin.server.GremlinServer" >/dev/null 2>&1; then
  echo "[start] Gremlin Server process exists; waiting for readiness."
else
  echo "[start] starting JanusGraph Gremlin Server (in-memory backend)"
  "$SERVER_SCRIPT" start || true
fi

echo "[start] waiting for ws://127.0.0.1:8182/gremlin"
for _ in {1..60}; do
  if is_ready; then
    echo "[start] JanusGraph is accepting connections"
    exit 0
  fi
  sleep 2
done

echo "[start] JanusGraph did not become ready in time" >&2
exit 1
