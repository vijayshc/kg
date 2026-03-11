#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

export JANUSGRAPH_URL="${JANUSGRAPH_URL:-ws://127.0.0.1:8182/gremlin}"
export JANUSGRAPH_TRAVERSAL_SOURCE="${JANUSGRAPH_TRAVERSAL_SOURCE:-g}"
export JANUSGRAPH_GRAPH_ALIAS="${JANUSGRAPH_GRAPH_ALIAS:-graph}"

cd "$ROOT_DIR"

~/anaconda3/bin/python3 scripts/generate_banking_sample_data.py
bash scripts/install_janusgraph_local.sh
bash scripts/stop_janusgraph_test.sh || true
bash scripts/start_janusgraph_test.sh

~/anaconda3/bin/python3 -m pip install -r requirements.txt
~/anaconda3/bin/python3 kg_loader.py \
  --mapping config/ontology_mapping.test.yaml \
  --report-file load_report.test.json \
  --log-level INFO

~/anaconda3/bin/python3 scripts/verify_graph_summary.py
