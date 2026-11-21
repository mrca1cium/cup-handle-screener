#!/usr/bin/env python3
"""Read-only acceptance against an existing checkout/cache; never fetch market data.

Run with the target checkout's Python 3.9+ environment. The candidate source is
this script's checkout; --root is the unchanged baseline checkout/cache.
All output writes are intercepted in memory and provider HTTP is disabled.
"""
from __future__ import annotations

import argparse
import builtins
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import types


def verify(root, sources):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    import requests

    def no_network(*args, **kwargs):
        raise AssertionError("Network disabled for cache acceptance")

    requests.sessions.Session.request = no_network
    for name, key in (("screener.freshness", "freshness"), ("scripts.candidate_priority", "priority")):
        module = types.ModuleType(name)
        module.__file__ = str(root / (name.replace(".", "/") + ".py"))
        sys.modules[name] = module
        exec(compile(sources[key], module.__file__, "exec"), module.__dict__)

    def load_module(source, name):
        module = types.ModuleType(name)
        module.__file__ = str(root / "scripts/build_website_from_cache.py")
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        return module

    baseline = load_module((root / "scripts/build_website_from_cache.py").read_text(), "baseline")
    candidate = load_module(sources["builder"], "candidate")
    all_data, state, previous = baseline.load_cache(), baseline.load_state(), baseline.load_previous_output()
    unavailable = baseline.data_mod._load_unavailable()
    baseline.data_mod._load_unavailable = lambda: unavailable
    # Freeze display metadata from local state/output; never invoke Nasdaq fallback.
    metadata = {row["symbol"]: dict(row) for row in state.get("universe_rows", [])
                if isinstance(row, dict) and row.get("symbol")}
    for row in (previous.get("results") or []) + (previous.get("watchlist") or []):
        metadata.setdefault(row["symbol"], dict(row))
    original_open, original_makedirs = builtins.open, os.makedirs
    output = {}

    class Sink(io.StringIO):
        def __init__(self, key):
            super().__init__()
            self.key = key

        def close(self):
            if not self.closed:
                output[self.key] = json.loads(self.getvalue())
            super().close()

    def memory_open(path, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            if Path(path).parent == Path("/__apex_memory_output__"):
                return Sink(Path(path).name)
            raise AssertionError("Unexpected disk write")
        return original_open(path, mode, *args, **kwargs)

    results = []
    try:
        builtins.open, os.makedirs = memory_open, lambda *a, **k: None
        for module in (baseline, candidate):
            module.OUT_DIR = "/__apex_memory_output__"
            module.build_metadata = lambda _state: metadata
            module.load_cache = lambda: all_data
            module.load_state = lambda: state
            module.load_previous_output = lambda: previous
            with contextlib.redirect_stdout(io.StringIO()):
                module.main()
            results.append(dict(output))
            output.clear()
    finally:
        builtins.open, os.makedirs = original_open, original_makedirs
    old, new = results
    a, b = old["results.json"], new["results.json"]

    def strip(rows):
        return [{k: v for k, v in row.items() if k != "data_date"} for row in rows]

    report = {
        "cache_files": len(all_data), "snapshot": len(state["universe_symbols"]),
        "matches": len(b["results"]), "watch": len(b["watchlist"]),
        "match_rows_identical": a["results"] == strip(b["results"]),
        "watch_rows_identical": a["watchlist"] == strip(b["watchlist"]),
        "charts_identical": old["charts.json"] == new["charts.json"],
        "legacy_top_fields_identical": all(a[k] == b[k] for k in a if k not in ("results", "watchlist")),
        "p2_count": len(b["refresh_priority"]), "p2": b["refresh_priority"],
        "expected_session": b["expected_session"],
        "status_counts": {status: sum(x["status"] == status for x in b["data_status"].values())
                          for status in ("CURRENT", "STALE", "MISSING", "UNAVAILABLE", "INSUFFICIENT", "API_FAILED")},
        "disk_writes": 0, "provider_calls": 0,
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    sources = {key: (candidate / path).read_text() for key, path in (
        ("builder", "scripts/build_website_from_cache.py"),
        ("priority", "scripts/candidate_priority.py"), ("freshness", "screener/freshness.py"))}
    report = verify(args.root, sources)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(report[k] for k in ("match_rows_identical", "watch_rows_identical", "charts_identical", "legacy_top_fields_identical")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
