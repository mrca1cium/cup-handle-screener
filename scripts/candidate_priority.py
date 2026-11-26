"""Pure offline candidate-first request scheduling; no network or filesystem writes."""
from __future__ import annotations

import datetime
import math
import copy
from screener.freshness import is_session, NY, close_hour, PROVIDER_GRACE

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
    try:
        fetched = float(payload.get("fetched_at") or 0)
        if not math.isfinite(fetched) or fetched > now:
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    last = last_bar_date(payload)
    if target_date and last and last >= target_date:
        return False
    if priority and target_date and last and last < target_date:
        day = datetime.date.fromisoformat(target_date)
        ready = datetime.datetime.combine(day, datetime.time(close_hour(day)), NY) + PROVIDER_GRACE
        # A fetch before this session closed cannot satisfy the new session.
        # Do not let a 12h scheduler interval miss by a few seconds forever.
        if now >= ready.timestamp() and fetched < ready.timestamp():
            return True
        return now - fetched >= MIN_RECHECK_SECONDS
    return now - fetched >= (MIN_RECHECK_SECONDS if priority else stale_seconds)


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
    if max_sessions <= 0:
        return {}
    try:
        session = datetime.date.fromisoformat(str(as_of_date)[:10])
    except (TypeError, ValueError):
        return {}
    cutoff = session
    count = 1 if is_session(session) else 0
    while count < max_sessions:
        cutoff -= datetime.timedelta(days=1)
        if is_session(cutoff):
            count += 1
    result = {}
    for key, values in (history if isinstance(history, dict) else {}).items():
        try:
            day = datetime.date.fromisoformat(str(key))
        except ValueError:
            continue
        if isinstance(values, list) and cutoff <= day <= session and is_session(day):
            result[day.isoformat()] = sorted({str(s).upper() for s in values if str(s).upper() in allowed})
    if is_session(session):
        result[session.isoformat()] = sorted(set(result.get(session.isoformat(), [])) | priority_symbols(previous, snapshot))
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
    primary = [j for j in ordered if j["tier"] < 2]
    potential = [j for j in ordered if j["tier"] == 2]
    general = [j for j in ordered if j["tier"] > 2]
    reserve = min(len(general), int(max_requests * reserve_general_fraction))
    potential_reserve = min(len(potential), max(1, max_requests // 10)) if potential and max_requests > 1 else 0
    potential_reserve = min(potential_reserve, max_requests - reserve)
    chosen = primary[:max_requests - reserve - potential_reserve]
    chosen += potential[:potential_reserve] + general[:reserve]
    used = {j["symbol"] for j in chosen}
    chosen += [j for j in ordered if j["symbol"] not in used][:max_requests - len(chosen)]
    return chosen


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
TWELVEDATA_RESERVE_CREDITS = 8


def _events(ledger, now):
    """Reject legacy/ambiguous state rather than assuming unused API capacity."""
    if (not isinstance(ledger, dict) or ledger.get("version") != 2
            or ledger.get("exclusive_key") is not True or ledger.get("blocked_reason")
            or ledger.get("pending", False) is not False):
        return None
    try:
        initialized = ledger["initialized_at"]
        if type(initialized) not in (int, float) or not math.isfinite(initialized) or initialized > now:
            return None
        events = ledger["events"]
        if not isinstance(events, list):
            return None
        active, total = [], 0
        for event in events:
            at, credits = event["at"], event["credits"]
            if (type(at) not in (int, float) or not math.isfinite(at) or at > now
                    or at < initialized or type(credits) is not int or credits <= 0):
                return None
            total += credits
            if now - at < 86400:
                active.append(dict(event))
        if type(ledger.get("credits")) is not int or ledger["credits"] != total:
            return None
        return active
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def credit_remaining(ledger, now, daily_budget, reserve=TWELVEDATA_RESERVE_CREDITS):
    if type(reserve) is not int or reserve <= 0:
        raise ValueError("reserve must be a positive integer")
    events = _events(ledger, now)
    if events is None:
        return 0
    return max(0, (daily_budget - sum(e["credits"] for e in events)) // reserve)


def reserve_credit(ledger, now, daily_budget, reserve=TWELVEDATA_RESERVE_CREDITS):
    if credit_remaining(ledger, now, daily_budget, reserve) <= 0:
        return None
    result = copy.deepcopy(ledger)
    result["events"] = _events(ledger, now) + [{"at": now, "credits": reserve}]
    result["pending"] = True
    result["credits"] = sum(e["credits"] for e in result["events"])
    return result


def initialize_credit_ledger(now, daily_budget, exclusive_key=False):
    """Offline commissioning: reserve a full budget for a 24h quiet period.

    Never seed an empty ledger against an account whose prior use is unknown.
    The operator must first isolate this key from every other consumer.
    """
    if exclusive_key is not True:
        raise ValueError("exclusive key confirmation required")
    return {"version": 2, "initialized_at": now, "exclusive_key": True,
            "events": [{"at": now, "credits": daily_budget}], "credits": daily_budget}


def settle_credit(ledger, now, observed_weight):
    """Keep reservations, date them at completion, and latch unknown/large costs."""
    result = copy.deepcopy(ledger)
    event = result["events"][-1]
    event["at"] = max(event["at"], now)
    result["pending"] = False
    valid = type(observed_weight) is int and 0 < observed_weight <= TWELVEDATA_RESERVE_CREDITS
    if not valid:
        result["blocked_reason"] = "unknown_or_excessive_request_weight"
        if type(observed_weight) is int and observed_weight > event["credits"]:
            event["credits"] = observed_weight
    result["credits"] = sum(e["credits"] for e in result["events"])
    return result
