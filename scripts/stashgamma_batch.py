from __future__ import annotations
"""Run a small, persistent StashGamma batch for the local Mac scheduler.

The batch is deliberately conservative: at most 250 API requests per run,
cache hits cost zero requests, unavailable symbols are skipped, and provider
rate limits stop the run immediately. StashGamma and Twelve Data use separate
pacing because Twelve Data Basic allows only 8 API credits per minute.
Candidate-first scheduling prioritizes recent setups and reserves general
universe slots. Twelve Data usage is persisted before requests and fails closed.
"""
import argparse
import fcntl
import json
import logging
import os
import sys
import time
from typing import List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from screener import data as data_mod  # noqa: E402
from screener.freshness import latest_session  # noqa: E402
from scripts.candidate_priority import (eligible_for_refresh, last_bar_date, priority_symbols, schedule, update_candidate_history, historical_priority, select_provider_jobs, credit_remaining, reserve_credit, settle_credit)  # noqa: E402
from screener.universe import get_broad_market  # noqa: E402
from screener.market import SECTORS, INDICES  # noqa: E402
from screener.main import _cheap_filter, _rank  # noqa: E402

CACHE_DIR = os.path.join(ROOT, ".cache", "market")
LOG_DIR = os.path.join(ROOT, ".cache", "logs")
STATE_PATH = os.path.join(CACHE_DIR, "stashgamma_batch_state.json")

INSUFFICIENT_RETRY_DAYS = 30
TWELVEDATA_DAILY_BUDGET = 320  # Conservative safety margin below 350 advertised credits
# Basic allows 8 API credits/minute. The actual weight can vary by request,
# so the runner now reads Api-Credits-Request from every response.
TWELVEDATA_MIN_INTERVAL = 65.0  # Reserve eight credits; at most one request per minute
TWELVEDATA_CREDIT_RESET_WAIT = 65.0
TWELVEDATA_SAFE_MIN_LEFT = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("stashgamma_batch")


def load_cache_meta(symbol: str):
    path = data_mod._cache_path(symbol, "2y")
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        data = payload.get("data") or {}
        if len(data.get("dates", [])) < data_mod.DEFAULT_MIN_BARS:
            return None
        return payload
    except Exception:
        return None


def candidate_rows():
    core, extra = get_broad_market()
    rows = core + extra
    cheap, _stats = _cheap_filter(
        rows,
        min_price=10.0,
        min_current_vol=50_000,
        min_market_cap=2_000_000_000,
    )
    cheap.sort(key=_rank, reverse=True)
    # Diagnostic counts describe the live broad pool; snapshot membership stays frozen.
    save_state(
        universe_core_count=len(core),
        universe_extra_count=len(extra),
        universe_broad_count=len(rows),
        universe_cheap_stats=_stats,
        universe_stats_date=time.strftime("%Y-%m-%d", time.localtime()),
    )
    log.info(
        "Broad=%d；cheap filter passed=%d（market cap >= $2B, price >= $10, current volume >= 50K）",
        len(rows),
        len(cheap),
    )
    return cheap


def candidate_symbols() -> List[str]:
    return [r["symbol"] for r in candidate_rows()]


def build_twelvedata_fallback_symbols(
    cheap_rows,
    snapshot: List[str],
    unavailable: set,
    insufficient: set,
) -> List[str]:
    """Build the persistent Twelve Data fallback target set.

    The persistent universe snapshot is authoritative; current Nasdaq metadata
    must not shrink the fallback pool during cache construction. We only keep
    securities that Twelve Data classifies as equity-like:
    common stock, ADR/DR/GDR, limited partnership, or REIT. This avoids
    spending fallback credits on preferreds, funds, units, notes, warrants,
    etc.
    """
    # Do not make an unmetered /stocks request before credit commissioning.
    try:
        with open(data_mod.TWELVEDATA_STOCKS_CACHE, encoding="utf-8") as f:
            stock_types = json.load(f)
        if not isinstance(stock_types, dict):
            raise ValueError("invalid stock type cache")
    except (OSError, ValueError):
        log.warning("Twelve Data type metadata missing; fallback disabled until cache is commissioned")
        return None
    eligible = set()
    target_set = {
        str(s).upper()
        for s in snapshot
        if str(s).upper() in (unavailable | insufficient)
    }
    for symbol in target_set:
        instrument_type = stock_types.get(symbol)
        if data_mod.is_twelvedata_equity_type(instrument_type):
            eligible.add(symbol)
    result = sorted(eligible)
    log.info(
        "Twelve Data fallback candidates=%d（unavailable/insufficient ∩ cheap universe ∩ equity-like）",
        len(result),
    )
    return result


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_state(**kwargs):
    os.makedirs(CACHE_DIR, exist_ok=True)
    old = load_state()
    old.update(kwargs)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def get_insufficient_map(state: dict) -> dict:
    """Read the persistent symbol map, tolerating the old integer summary field."""
    raw = state.get("insufficient_symbols")
    if isinstance(raw, dict):
        return raw

    # Backward compatibility with the first version, which accidentally used
    # the same key for both the symbol map and the per-run integer count.
    legacy = state.get("insufficient_data")
    if isinstance(legacy, dict):
        return legacy
    return {}


def insufficient_symbols(state: dict, now: float) -> set:
    """Return symbols temporarily skipped after an insufficient-history result."""
    result = set()
    raw = get_insufficient_map(state)
    retry_after = INSUFFICIENT_RETRY_DAYS * 86400
    for symbol, info in raw.items():
        try:
            checked_at = float(info.get("checked_at", 0))
            if now - checked_at < retry_after:
                result.add(str(symbol).upper())
        except (AttributeError, TypeError, ValueError):
            continue
    return result


def get_universe_snapshot(state: dict, current_symbols: List[str], current_rows=None) -> List[str]:
    """Persist the cheap-filter universe so later runs do not shrink it.

    Nasdaq metadata such as price/current volume/market cap changes daily.
    Recomputing the candidate list every batch can therefore make previously
    eligible symbols disappear before they are downloaded. The first run
    creates a snapshot; subsequent runs use that same snapshot until the
    cache-building phase is complete.
    """
    raw = state.get("universe_symbols")
    if isinstance(raw, list) and raw:
        return [str(s).upper() for s in raw]

    snapshot = []
    seen = set()
    for symbol in current_symbols:
        upper = str(symbol).upper()
        if upper and upper not in seen:
            seen.add(upper)
            snapshot.append(upper)

    save_state(
        universe_symbols=snapshot,
        universe_rows=list(current_rows or []),
        universe_created_at=time.time(),
        universe_created_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    log.info(
        "建立 persistent universe snapshot：%d symbols；之後 cache 建立期間不會因 Nasdaq metadata 變化而縮小",
        len(snapshot),
    )
    return snapshot


def refresh_market_cache() -> None:
    """Refresh fixed market-radar instruments used by the website.

    Sector ETFs provide sector relative strength. QQQ/IWM are conservative
    tradable proxies for Nasdaq/Russell when the index symbols themselves are
    unavailable from the historical provider.
    """
    symbols = list(SECTORS.keys()) + ["SPY", "QQQ", "IWM"] + list(INDICES.keys())
    refreshed = 0
    failed = 0
    seen = set()
    for symbol in symbols:
        if symbol in seen:
            continue
        seen.add(symbol)
        try:
            data = data_mod.fetch_daily(symbol, "2y", refresh=True)
            if data:
                refreshed += 1
            else:
                failed += 1
        except data_mod.StashGammaRateLimitError as exc:
            failed += 1
            log.warning("Market radar %s rate limited: %s", symbol, exc)
            break
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning("Market radar %s refresh failed: %s", symbol, exc)
    log.info("Market radar cache refresh：success=%d failed=%d total=%d", refreshed, failed, len(seen))


def _run_locked(max_requests: int = 250, stale_days: int = 7, pause: float = 0.35):
    max_requests = min(250, max(0, max_requests))
    providers = set()
    if os.environ.get("STASHGAMMA_API_KEY"):
        providers.add("stashgamma")
    if os.environ.get("TWELVEDATA_API_KEY"):
        providers.add("twelvedata")
    if not providers:
        log.error("No provider credentials configured; no requests made")
        return 2

    os.makedirs(LOG_DIR, exist_ok=True)
    cheap_rows = candidate_rows()
    current_symbols = [r["symbol"] for r in cheap_rows]
    state = load_state()
    symbols = get_universe_snapshot(state, current_symbols, cheap_rows)

    unavailable = data_mod._load_unavailable()
    state = load_state()
    now = time.time()
    stale_seconds = stale_days * 86400
    insufficient = insufficient_symbols(state, now)

    fallback_symbols = state.get("twelvedata_fallback_symbols")
    if not isinstance(fallback_symbols, list):
        fallback_symbols = build_twelvedata_fallback_symbols(
            cheap_rows,
            symbols,
            unavailable,
            insufficient,
        )
        if fallback_symbols is not None:
            save_state(twelvedata_fallback_symbols=fallback_symbols)
    fallback_symbols = [str(s).upper() for s in (fallback_symbols or [])]
    fallback_set = set(fallback_symbols)

    # Candidate-first scheduling. Use the latest cached SPY bar as a market
    # reference, not the wall-clock date (weekends/holidays are excluded).
    previous = {}
    try:
        with open(os.path.join(ROOT, "docs", "data", "results.json"), encoding="utf-8") as f:
            previous = json.load(f)
    except (OSError, ValueError):
        pass
    if not isinstance(previous, dict):
        previous = {}
    # Keep a bounded trading-session history; only the branch implementation
    # writes this state if explicitly deployed after review.
    history = update_candidate_history(
        state.get("candidate_history"), previous, symbols,
        previous.get("generated_at") if isinstance(previous, dict) else None,
    )
    if not isinstance(previous, dict):
        previous = {}
    important = priority_symbols(previous, symbols) | historical_priority(history, symbols)
    p2 = {str(x).upper() for x in (previous.get("refresh_priority") or [])} & set(symbols)
    target_date = latest_session()
    jobs = []
    missing = []
    stale = []
    skipped_insufficient = 0
    market_symbols = set(SECTORS) | {"SPY", "QQQ", "IWM"} | set(INDICES)
    for symbol in dict.fromkeys(list(market_symbols) + symbols):
        upper = symbol.upper()
        meta = load_cache_meta(symbol)
        if upper in insufficient:
            skipped_insufficient += 1
            continue
        if meta is None and upper in unavailable and upper not in fallback_set:
            continue
        if upper in fallback_set and (meta is None or upper in unavailable):
            provider = "twelvedata"
        elif meta and meta.get("source") == "twelvedata":
            provider = "twelvedata"
        else:
            provider = "stashgamma"
        is_priority = upper in important or upper in market_symbols
        if meta is None:
            missing.append(symbol)
        else:
            if not eligible_for_refresh(meta, target_date, now, stale_seconds, is_priority or upper in p2):
                continue
            stale.append(symbol)
        last = last_bar_date(meta) if meta else None
        age = (time.time() - time.mktime(time.strptime(last, "%Y-%m-%d"))) / 86400 if last else 9999
        jobs.append({"symbol": symbol, "provider": provider,
                     "tier": 0 if upper in market_symbols else (1 if is_priority else (2 if upper in p2 or meta is None else 3)),
                     "age_days": max(0, age)})

    # Fixed per-run request ceiling. Twelve Data daily credits remain
    # separately capped; never bypass the provider's own 429 protection.
    queued = schedule(jobs, max_requests)
    # Fill spare slots if a provider's allowance excludes queued symbols.
    remainder = [j for j in schedule(jobs, len(jobs), 0) if j["symbol"] not in
                 {q["symbol"] for q in queued}]
    td_remaining = credit_remaining(
        state.get("twelvedata_daily_ledger"), now, TWELVEDATA_DAILY_BUDGET,
    ) if "twelvedata" in providers else 0
    selected_jobs = select_provider_jobs(
        [j for j in queued + remainder if j["provider"] in providers], max_requests, td_remaining,
    )
    selected = [j["symbol"] for j in selected_jobs]
    provider_by_symbol = {j["symbol"]: j["provider"] for j in selected_jobs}
    selected_provider = "candidate-first"
    fallback_missing = [s for s in missing if s.upper() in fallback_set]
    building_cache = bool(missing)

    log.info(
        "Cache build=%s；snapshot=%d；missing=%d；fallback_candidates=%d；fallback_missing=%d；stale=%d；insufficient 暫時跳過=%d；provider=%s；實際處理=%d",
        building_cache,
        len(symbols),
        len(missing),
        len(fallback_symbols),
        len(fallback_missing),
        len(stale),
        skipped_insufficient,
        selected_provider,
        len(selected),
    )

    success = 0
    unavailable_count = 0
    insufficient_count = 0
    rate_limited = False
    failed = 0
    last_twelvedata_request = None
    last_twelvedata_request_credits = None
    last_twelvedata_credits_left = None

    observed_credit_weight_unsafe = False
    diagnostics = dict(state.get("data_diagnostics") or {})
    diagnostics.update({j["symbol"]: {"status": "CREDIT_BLOCKED", "provider": "twelvedata"}
                        for j in jobs if j["provider"] == "twelvedata" and td_remaining == 0})
    attempted = 0
    updated_current = 0
    for index, symbol in enumerate(selected, 1):
        if observed_credit_weight_unsafe:
            break
        meta = load_cache_meta(symbol)
        refresh = meta is not None
        provider = provider_by_symbol[symbol]

        if provider == "twelvedata":
            # The pessimistic eight-credit reservation also controls minute pacing.
            if last_twelvedata_request is not None:
                wait = TWELVEDATA_MIN_INTERVAL - (time.monotonic() - last_twelvedata_request)
                if wait > 0:
                    time.sleep(wait)
            reserved = reserve_credit(load_state().get("twelvedata_daily_ledger"),
                                      time.time(), TWELVEDATA_DAILY_BUDGET)
            if reserved is None:
                diagnostics[symbol] = {"status": "CREDIT_BLOCKED", "provider": provider}
                continue
            save_state(twelvedata_daily_ledger=reserved)
            # Reset diagnostics even if a mocked/provider cache wrapper fails early.
            data_mod.TWELVEDATA_LAST_REQUEST_CREDITS = None
            data_mod.TWELVEDATA_LAST_CREDITS_LEFT = None
            last_twelvedata_request = time.monotonic()

        attempted += 1
        try:
            if provider == "twelvedata":
                result = (
                    data_mod.refresh_twelvedata(symbol)
                    if refresh
                    else data_mod.fetch_twelvedata(symbol, "2y")
                )
            else:
                result = data_mod.fetch_daily(symbol, "2y", refresh=refresh)

            if result is None:
                diagnostics[symbol] = {"status": "INSUFFICIENT", "provider": provider}
                insufficient_count += 1
                state = load_state()
                insufficient_map = get_insufficient_map(state)
                insufficient_map[symbol.upper()] = {
                    "checked_at": time.time(),
                    "retry_after_days": INSUFFICIENT_RETRY_DAYS,
                }
                save_state(insufficient_symbols=insufficient_map)
                log.info(
                    "[%d/%d] %s INSUFFICIENT_DATA：未取得足夠 %d bars，30 日後再檢查",
                    index,
                    len(selected),
                    symbol,
                    data_mod.DEFAULT_MIN_BARS,
                )
            else:
                if provider == "twelvedata":
                    (
                        last_twelvedata_request_credits,
                        last_twelvedata_credits_left,
                    ) = data_mod.get_twelvedata_credit_state()
                    log.info(
                        "Twelve Data usage: request_weight=%s credits_left=%s",
                        last_twelvedata_request_credits
                        if last_twelvedata_request_credits is not None
                        else "?",
                        last_twelvedata_credits_left
                        if last_twelvedata_credits_left is not None
                        else "?",
                    )
                is_current = bool(target_date and result.get("dates") and result["dates"][-1] >= target_date)
                diagnostics[symbol] = {"status": "CURRENT" if is_current else "STALE", "provider": provider,
                                       "last_bar": (result.get("dates") or [None])[-1]}
                updated_current += int(is_current)
                success += 1
                state = load_state()
                insufficient_map = get_insufficient_map(state)
                if symbol.upper() in insufficient_map:
                    insufficient_map.pop(symbol.upper(), None)
                    save_state(insufficient_symbols=insufficient_map)
                log.info(
                    "[%d/%d] %s OK source=%s%s",
                    index,
                    len(selected),
                    symbol,
                    provider,
                    " (refresh)" if refresh else "",
                )
        except data_mod.StashGammaUnavailableError as exc:
            diagnostics[symbol] = {"status": "UNAVAILABLE", "provider": provider}
            unavailable_count += 1
            data_mod._mark_unavailable(symbol)
            log.warning("[%d/%d] %s 404/unavailable：%s", index, len(selected), symbol, exc)
        except data_mod.TwelveDataUnavailableError as exc:
            diagnostics[symbol] = {"status": "UNAVAILABLE", "provider": provider}
            if provider == "twelvedata":
                (
                    last_twelvedata_request_credits,
                    last_twelvedata_credits_left,
                ) = data_mod.get_twelvedata_credit_state()
                log.info(
                    "Twelve Data usage after unavailable: request_weight=%s credits_left=%s",
                    last_twelvedata_request_credits
                    if last_twelvedata_request_credits is not None
                    else "?",
                    last_twelvedata_credits_left
                    if last_twelvedata_credits_left is not None
                    else "?",
                )
            failed += 1
            log.warning("[%d/%d] %s Twelve Data unavailable：%s", index, len(selected), symbol, exc)
        except data_mod.TwelveDataRateLimitError as exc:
            diagnostics[symbol] = {"status": "RATE_LIMITED", "provider": provider}
            rate_limited = True
            log.error(
                "[%d/%d] %s Twelve Data 429，立即停止本批：%s",
                index,
                len(selected),
                symbol,
                exc,
            )
            break
        except data_mod.StashGammaRateLimitError as exc:
            diagnostics[symbol] = {"status": "RATE_LIMITED", "provider": provider}
            rate_limited = True
            log.error("[%d/%d] %s 收到 429，立即停止本批：%s", index, len(selected), symbol, exc)
            break
        except KeyboardInterrupt:
            log.warning("收到 Ctrl-C，保留已完成 cache；下次會繼續")
            raise
        except Exception as exc:  # noqa: BLE001
            diagnostics[symbol] = {"status": "API_FAILED", "provider": provider}
            failed += 1
            log.warning("[%d/%d] %s 失敗：%s", index, len(selected), symbol, exc)
        finally:
            if provider == "twelvedata":
                weight, _left = data_mod.get_twelvedata_credit_state()
                ledger = settle_credit(reserved, time.time(), weight)
                save_state(twelvedata_daily_ledger=ledger)
                observed_credit_weight_unsafe = bool(ledger.get("blocked_reason"))
                if observed_credit_weight_unsafe:
                    log.error("Twelve Data cost unknown/excessive; persistent credit quarantine enabled")


    state = load_state()
    persistent_insufficient = get_insufficient_map(state)

    remaining_missing = 0
    for symbol in symbols:
        if symbol.upper() in unavailable:
            continue
        if symbol.upper() in persistent_insufficient:
            continue
        if load_cache_meta(symbol) is None:
            remaining_missing += 1

    # P0 market instruments already share the same bounded queue; no extra batch.

    save_state(
        candidate_history=history,
        data_diagnostics=diagnostics,
        credit_blocked=observed_credit_weight_unsafe,
        insufficient_symbols=persistent_insufficient,
        last_run_at=time.time(),
        last_run_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        requested=len(selected),
        success=success,
        updated_current=updated_current,
        attempted=attempted,
        insufficient_count=insufficient_count,
        unavailable=unavailable_count,
        failed=failed,
        rate_limited=rate_limited,
        cache_building=remaining_missing > 0,
        remaining_missing=remaining_missing,
        remaining_pending=(remaining_missing if remaining_missing > 0 else max(0, len(stale) - len(selected))),
    )

    log.info(
        "Batch 完成：requested=%d success=%d insufficient=%d unavailable=%d failed=%d rate_limited=%s；remaining_missing=%d",
        len(selected),
        success,
        insufficient_count,
        unavailable_count,
        failed,
        rate_limited,
        remaining_missing,
    )
    return 2 if rate_limited or observed_credit_weight_unsafe or failed else 0


def _run_with_lock(max_requests: int = 250, stale_days: int = 7, pause: float = 0.35):
    """Single-instance guard protects the state ledger and the batch scheduler."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    lock_path = os.path.join(CACHE_DIR, "stashgamma_batch.lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("Another batch owns the exclusive lock; no API requests made")
            return 2  # Existing runner treats 2 as retryable; do not publish on lock contention.
        try:
            return _run_locked(max_requests, stale_days, pause)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run(max_requests: int = 250, stale_days: int = 7, pause: float = 0.35):
    try:
        return _run_with_lock(max_requests, stale_days, pause)
    except (Exception, KeyboardInterrupt) as exc:
        # Existing Mac runner reserves exit 2 for retry/no-publication. In
        # particular, never publish if durable credit-state writes failed.
        log.error("Batch stopped safely (%s); no scheduled-slot completion", type(exc).__name__)
        return 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-requests", type=int, default=250)
    ap.add_argument("--stale-days", type=int, default=7)
    ap.add_argument("--pause", type=float, default=0.35)
    args = ap.parse_args()
    raise SystemExit(run(args.max_requests, stale_days=args.stale_days, pause=args.pause))
