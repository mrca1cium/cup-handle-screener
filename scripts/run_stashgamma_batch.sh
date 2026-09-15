#!/bin/bash
set -euo pipefail

ROOT="/Users/calcium/cup-handle-screener"
ENV_FILE="$HOME/.config/cup-handle-screener/stashgamma.env"
LOG_DIR="$ROOT/.cache/logs"
mkdir -p "$LOG_DIR"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"
cd "$ROOT"

exec /usr/bin/python3 "$ROOT/scripts/stashgamma_batch.py" --max-requests 250 --stale-days 7 --pause 0.35
