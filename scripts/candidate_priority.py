"""Pure offline candidate-first request scheduling; no network or filesystem writes."""
from __future__ import annotations

import datetime

MIN_RECHECK_SECONDS = 12 * 3600
HISTORY_TRADING_DAYS = 20
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


def update_candidate_history(history, previous, snapshot, as_of_date, max_sessions=HISTORY_TRADING_DAYS):
    """Maintain a bounded, date-keyed set of former MATCH/WATCH symbols."""
    allowed = {str(s).upper() for s in snapshot}
    result = {str(k): [s for s in v if s in allowed] for k, v in (history or {}).items()
              if isinstance(v, list)}
    if as_of_date:
        current = priority_symbols(previous, snapshot)
        result[as_of_date] = sorted(set(result.get(as_of_date, [])) | current)
    return dict(sorted(result.items(), reverse=True)[:max_sessions])


def historical_priority(history, snapshot):
    allowed = {str(s).upper() for s in snapshot}
    return {str(s).upper() for symbols in (history or {}).values()
            if isinstance(symbols, list) for s in symbols if str(s).upper() in allowed}


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


def select_provider_jobs(jobs, max_requests, twelvedata_remaining):
    """Backfill skipped provider jobs without exceeding either request limit."""
    result = []
    td = 0
    for job in jobs:
        if len(result) >= max_requests:
            break
        if job["provider"] == "twelvedata":
            if td >= max(0, twelvedata_remaining):
                continue
            td += 1
        result.append(job)
    return result

# Pessimistic credit accounting: reserve before sending an API call.
# A reserved credit is never refunded on network failures or unknown responses.
TWELVEDATA_RESERVE_CREDITS = 2


def utc_credit_day(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).date().isoformat()


def credit_remaining(ledger, now, daily_budget, reserve=TWELVEDATA_RESERVE_CREDITS):
    """Remaining safe call count, never carrying yesterday's usage forward."""
    day = utc_credit_day(now)
    used = int((ledger or {}).get("credits", 0)) if (ledger or {}).get("date") == day else 0
    return max(0, (max(0, daily_budget - used)) // reserve)


def reserve_credit(ledger, now, daily_budget, reserve=TWELVEDATA_RESERVE_CREDITS):
    """Return updated ledger or None if no safe allowance remains."""
    if credit_remaining(ledger, now, daily_budget, reserve) <= 0:
        return None
    day = utc_credit_day(now)
    used = int((ledger or {}).get("credits", 0)) if (ledger or {}).get("date") == day else 0
    return {"date": day, "credits": used + reserve}
