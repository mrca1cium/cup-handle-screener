#!/bin/bash
set -euo pipefail

ROOT="/Users/calcium/cup-handle-screener"
STASHGAMMA_ENV_FILE="$HOME/.config/cup-handle-screener/stashgamma.env"
TWELVEDATA_ENV_FILE="$HOME/.config/cup-handle-screener/twelvedata.env"
LOG_DIR="$ROOT/.cache/logs"
STATE_FILE="$ROOT/.cache/market/stashgamma_schedule_state.json"
PYTHON="$ROOT/.venv/bin/python"

mkdir -p "$LOG_DIR" "$(dirname "$STATE_FILE")"

if [ ! -f "$STASHGAMMA_ENV_FILE" ]; then
  echo "Missing $STASHGAMMA_ENV_FILE" >&2
  exit 1
fi

if [ ! -f "$TWELVEDATA_ENV_FILE" ]; then
  echo "Missing $TWELVEDATA_ENV_FILE" >&2
  exit 1
fi

if [ ! -x "$PYTHON" ]; then
  echo "Missing executable Python: $PYTHON" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$STASHGAMMA_ENV_FILE"
# shellcheck disable=SC1090
source "$TWELVEDATA_ENV_FILE"
cd "$ROOT"

# launchd uses RunAtLoad as a startup/catch-up trigger. Work out the most
# recent scheduled slot (01:00 or 13:00) and run only if that slot has not
# already been handled. This makes startup catch-up persistent across a full
# shutdown, while avoiding a duplicate run at normal scheduled times.
SCHEDULE_SLOT="$("$PYTHON" - <<'PY'
import datetime
now = datetime.datetime.now()
today = now.date()
if now.hour >= 13:
    slot = datetime.datetime.combine(today, datetime.time(13, 0))
elif now.hour >= 1:
    slot = datetime.datetime.combine(today, datetime.time(1, 0))
else:
    slot = datetime.datetime.combine(today - datetime.timedelta(days=1), datetime.time(13, 0))
print(slot.strftime("%Y-%m-%dT%H:%M"))
PY
)"

LAST_SLOT="$("$PYTHON" - "$STATE_FILE" <<'PY'
import json
import sys
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(data.get("last_scheduled_slot", ""))
except Exception:
    print("")
PY
)"

if [ "$LAST_SLOT" = "$SCHEDULE_SLOT" ]; then
  exit 0
fi

set +e
"$PYTHON" "$ROOT/scripts/stashgamma_batch.py" --max-requests 250 --stale-days 7 --pause 0.35
STATUS=$?
set -e

# Only mark the scheduled slot handled when the batch itself did not hit a
# provider rate limit. A rate-limited batch remains eligible for a later
# startup/catch-up invocation rather than being silently lost.
if [ "$STATUS" -ne 2 ]; then
  "$PYTHON" - "$STATE_FILE" "$SCHEDULE_SLOT" <<'PY'
import json
import os
import sys
path, slot = sys.argv[1], sys.argv[2]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
except Exception:
    data = {}
data["last_scheduled_slot"] = slot
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
PY
fi

# Rebuild the public website data from the local cache after every successful
# batch. This keeps docs/data/results.json and charts.json on the same cache
# used by the new screener instead of the retired Yahoo workflow.
if [ "$STATUS" -ne 2 ]; then
  set +e
  "$PYTHON" "$ROOT/scripts/build_website_from_cache.py"
  BUILD_STATUS=$?
  set -e

  if [ "$BUILD_STATUS" -ne 0 ]; then
    echo "Website build failed with status $BUILD_STATUS" >&2
  else
    /usr/bin/git add docs/data/results.json docs/data/charts.json
    if ! /usr/bin/git diff --cached --quiet; then
      /usr/bin/git config user.name "mrca1cium"
      /usr/bin/git config user.email "mr.calcium@gmail.com"
      /usr/bin/git commit -m "scan: update website from local cache"
      /usr/bin/git push
    fi
  fi
fi

exit "$STATUS"
