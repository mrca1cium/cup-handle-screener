"""Pure offline candidate-first request scheduling; no network or filesystem writes."""
from __future__ import annotations

import datetime

MIN_RECHECK_SECONDS = 12 * 3600
RESERVE_GENERAL_FRACTION = 0.20


def last_bar_date(payload):
    try:
        dates = payload["data"]["dates"]
        value = str(dates[-1])
        datetime.date.fromisoformat(value)
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def eligible_for_refresh(payload, target_date, now, stale_seconds, priority=False):
    """Require stale market bars for priority, and throttle repeated unchanged bars.

    General universe retains legacy fetched_at expiry; priority candidates
    may refresh sooner if their last bar is behind the reference market bar.
    """
    if payload is None:
        return True
    fetched = float(payload.get("fetched_at") or 0)
    if priority and target_date and last_bar_date(payload) and last_bar_date(payload) < target_date:
        return now - fetched >= MIN_RECHECK_SECONDS
    return now - fetched >= stale_seconds


def priority_symbols(previous, snapshot):
    allowed = {str(s).upper() for s in snapshot}
    selected = set()
    for row in (previous.get("results") or []) + (previous.get("watchlist") or []):
        if isinstance(row, dict):
            symbol = str(row.get("symbol") or "").upper()
            if symbol in allowed:
                selected.add(symbol)
    return selected


def schedule(jobs, max_requests, reserve_general_fraction=RESERVE_GENERAL_FRACTION):
    """jobs: iterable of dicts with symbol, tier (0..3), provider, age_days.

    Deduplicate, prioritize candidates, and reserve general-universe slots
    when lower-priority work exists. Provider quotas are enforced by caller.
    """
    if max_requests <= 0:
        return []
    unique = {}
    for job in jobs:
        symbol = str(job["symbol"]).upper()
        old = unique.get(symbol)
        if old is None or (job["tier"], -job.get("age_days", 0)) < (old["tier"], -old.get("age_days", 0)):
            unique[symbol] = dict(job, symbol=symbol)
    ordered = sorted(unique.values(), key=lambda x: (x["tier"], -x.get("age_days", 0), x["symbol"]))
    high = [j for j in ordered if j["tier"] <= 2]
    general = [j for j in ordered if j["tier"] > 2]
    reserve = min(len(general), int(max_requests * reserve_general_fraction))
    first = high[:max_requests - reserve]
    remaining = max_requests - len(first)
    chosen_general = general[:remaining]
    leftover = max_requests - len(first) - len(chosen_general)
    return first + chosen_general + high[len(first):len(first) + leftover]
