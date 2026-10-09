"""Compare main-branch and Step 4 pattern selection using existing local cache.

Run on the Step 4 branch:
  .venv/bin/python scripts/compare_pattern_versions.py

Offline only: reads .cache/market and the committed main version via git show.
Does not modify cache, published website, or use market-data APIs.
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from screener import pattern as new_pattern, stage2
from scripts import build_website_from_cache as builder
from screener import data as data_mod


def load_old_pattern():
    source = subprocess.check_output(
        ["git", "show", "origin/main:screener/pattern.py"],
        cwd=ROOT, text=True,
    )
    # Relative import in pattern.py resolves to the installed screener package.
    module = types.ModuleType("screener._old_pattern_compare")
    module.__package__ = "screener"
    exec(compile(source, "origin/main:screener/pattern.py", "exec"), module.__dict__)
    return module


def main():
    old_pattern = load_old_pattern()
    state = builder.load_state()
    snapshot = state.get("universe_symbols") or []
    all_data = builder.load_cache()
    spy = all_data.get("SPY")
    if not snapshot or not spy:
        raise SystemExit("Missing local universe snapshot or SPY cache; no API calls attempted.")

    data_date = builder.cache_last_date(all_data)
    if not data_date:
        raise SystemExit("No cached data dates.")
    import datetime
    cutoff = (datetime.date.fromisoformat(data_date) - datetime.timedelta(days=7)).isoformat()
    ratings = stage2.rs_ratings(all_data, spy["close"])
    transitions = collections.Counter()
    changes = []
    vcp_summary = collections.Counter()
    eligible = 0
    stage2_count = 0
    for symbol in sorted(set(str(s).upper() for s in snapshot)):
        d = all_data.get(symbol)
        if not d or not d.get("dates") or str(d["dates"][-1]) < cutoff:
            continue
        if data_mod.average_volume(d, bars=63) < 500_000:
            continue
        eligible += 1
        rs = ratings.get(symbol)
        if rs is None or not stage2.check_stage2(d, rs)["pass"]:
            continue
        stage2_count += 1
        before = old_pattern.detect(d)
        after = new_pattern.detect(d)
        a = before["grade"] if before else "none"
        b = after["grade"] if after else "none"
        if after:
            m = after["metrics"]
            vcp_summary[("tight" if m["vcp_tight_range"] else "not_tight", "contracting" if m["vcp_progressive_contraction"] else "not_contracting")] += 1
        transitions[(a, b)] += 1
        if a != b or (before and after and before["metrics"]["cup_days"] != after["metrics"]["cup_days"]):
            changes.append({
                "symbol": symbol,
                "old": a, "new": b,
                "old_cup_days": before["metrics"]["cup_days"] if before else None,
                "new_cup_days": after["metrics"]["cup_days"] if after else None,
                "new_vcp_ranges": after["metrics"].get("vcp_segment_ranges_pct") if after else None,
                "new_vcp_tight": after["metrics"].get("vcp_tight_range") if after else None,
                "new_vcp_progressive": after["metrics"].get("vcp_progressive_contraction") if after else None,
            })

    print("Comparison: main vs step4-cup-vcp-review (offline)")
    print("Cache date:", data_date, "| Eligible:", eligible, "| Stage 2:", stage2_count)
    print("Transitions:")
    for (a, b), count in sorted(transitions.items()):
        print("  {:>5} -> {:<5}: {}".format(a, b, count))
    print("VCP diagnostics among selected new patterns:")
    for key, count in sorted(vcp_summary.items()):
        print("  {} / {}: {}".format(key[0], key[1], count))
    print("Changes:", len(changes))
    for row in changes:
        print(" ", json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
