#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
JANUSGRAPH_VERSION="1.1.0"
JANUSGRAPH_ZIP="janusgraph-${JANUSGRAPH_VERSION}.zip"
JANUSGRAPH_URL="https://github.com/JanusGraph/janusgraph/releases/download/v${JANUSGRAPH_VERSION}/${JANUSGRAPH_ZIP}"
TOOLS_DIR="$ROOT_DIR/tools"

mkdir -p "$TOOLS_DIR"

if [[ ! -f "$TOOLS_DIR/$JANUSGRAPH_ZIP" ]]; then
  echo "[install] downloading JanusGraph ${JANUSGRAPH_VERSION}"
  curl -fL -C - "$JANUSGRAPH_URL" -o "$TOOLS_DIR/$JANUSGRAPH_ZIP"
fi

if [[ ! -d "$TOOLS_DIR/janusgraph-${JANUSGRAPH_VERSION}" ]]; then
  echo "[install] unpacking JanusGraph ${JANUSGRAPH_VERSION}"
  unzip -q "$TOOLS_DIR/$JANUSGRAPH_ZIP" -d "$TOOLS_DIR"
fi

echo "[install] JanusGraph home: $TOOLS_DIR/janusgraph-${JANUSGRAPH_VERSION}"
