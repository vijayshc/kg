#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

ensure_ticket() {
  local principal="$1"
  local keytab="$2"

  if [[ -z "$principal" || -z "$keytab" ]]; then
    return 0
  fi

  if ! klist -s 2>/dev/null; then
    echo "[kg-loader] acquiring Kerberos ticket for $principal"
    kinit -kt "$keytab" "$principal"
  fi
}

ensure_ticket "${TERADATA_KRB5_PRINCIPAL:-}" "${TERADATA_KRB5_KEYTAB:-}"

exec ~/anaconda3/bin/python3 "$ROOT_DIR/kg_loader.py" "$@"
