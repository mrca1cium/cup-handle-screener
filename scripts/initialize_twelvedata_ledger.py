#!/usr/bin/env python3
"""Offline ledger commissioning. No API calls; inspection is the default.

Initialization requires an isolated key and imposes a full 24-hour cooldown.
Do not run against production state without separate rollout authorization.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.candidate_priority import initialize_credit_ledger, credit_remaining

BUDGET = 320


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--exclusive-key", action="store_true",
                        help="Operator confirms no other process, machine or service uses this key")
    args = parser.parse_args(argv)
    if args.initialize and not args.exclusive_key:
        parser.error("Initialization requires --exclusive-key; inspect all consumers first")
    try:
        if not args.initialize:
            state = json.loads(args.state.read_text(encoding="utf-8"))
            print(json.dumps({"available_requests": credit_remaining(
                state.get("twelvedata_daily_ledger"), time.time(), BUDGET), "read_only": True}))
            return 0
        # Use the exact same exclusive lock as the future batch runner.
        with (args.state.parent / "stashgamma_batch.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = json.loads(args.state.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("State must be an object")
            now = time.time()
            state["twelvedata_daily_ledger"] = initialize_credit_ledger(now, BUDGET, True)
            path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.state.parent,
                                                 prefix=".credit-state-", delete=False) as f:
                    path = f.name
                    json.dump(state, f, ensure_ascii=False, indent=2, allow_nan=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(path, args.state)
            finally:
                if path and os.path.exists(path):
                    os.unlink(path)
            print(json.dumps({"initialized": True, "available_requests": 0,
                              "not_before_utc_epoch": now + 86400,
                              "requires_exclusive_key_for_all_future_use": True}))
            return 0
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"Ledger commissioning stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
