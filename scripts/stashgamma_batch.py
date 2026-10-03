from __future__ import annotations
"""Run a small, persistent StashGamma batch for the local Mac scheduler.

The batch is deliberately conservative: at most 250 API requests per run,
cache hits cost zero requests, unavailable symbols are skipped, and provider
rate limits stop the run immediately. StashGamma and Twelve Data use separate
pacing because Twelve Data Basic allows only 8 API credits per minute.
During the initial cache-building phase, only symbols without a valid 2y cache
are downloaded; stale-cache refreshes do not consume the build quota until the
snapshot is complete.
"""
import argparse
import json
import logging
import os
import sys
import time
from typing import List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from screener import data as data_mod  # noqa: E402
from screener.universe import get_broad_market  # noqa: E402
from screener.market import SECTORS, INDICES  # noqa: E402
from screener.main import _cheap_filter, _rank  # noqa: E402

CACHE_DIR = os.path.join(ROOT, ".cache", "market")
LOG_DIR = os.path.join(ROOT, ".cache", "logs")
STATE_PATH = os.path.join(CACHE_DIR, "stashgamma_batch_state.json")

INSUFFICIENT_RETRY_DAYS = 30
TWELVEDATA_DAILY_BUDGET = 350
# Basic allows 8 API credits/minute. The actual weight can vary by request,
# so the runner now reads Api-Credits-Request from every response.
TWELVEDATA_MIN_INTERVAL = 8.0
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
    stock_types = data_mod._load_twelvedata_stock_types()
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
        universe_core_count=len(current_rows or []),
        universe_extra_count=0,
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


def run(max_requests: int = 250, stale_days: int = 7, pause: float = 0.35):
    if not os.environ.get("STASHGAMMA_API_KEY"):
        raise SystemExit("找不到 STASHGAMMA_API_KEY")
    if not os.environ.get("TWELVEDATA_API_KEY"):
        raise SystemExit("找不到 TWELVEDATA_API_KEY")

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
        save_state(twelvedata_fallback_symbols=fallback_symbols)
    fallback_symbols = [str(s).upper() for s in fallback_symbols]
    fallback_set = set(fallback_symbols)

    missing = []
    stale = []
    skipped_insufficient = 0

    for symbol in symbols:
        upper = symbol.upper()
        meta = load_cache_meta(symbol)

        if meta is None:
            if upper in fallback_set:
                continue
            if upper in unavailable or upper in insufficient:
                continue
            missing.append(symbol)
            continue

        if upper in insufficient and upper not in fallback_set:
            skipped_insufficient += 1
            continue

        fetched_at = float(meta.get("fetched_at") or 0)
        if now - fetched_at >= stale_seconds:
            stale.append(symbol)

    # Initial StashGamma gaps are filled by Twelve Data after ordinary
    # StashGamma-missing symbols are exhausted. Once fallback targets have
    # caches, normal stale refreshes route to the provider that owns the cache.
    building_cache = bool(missing)
    # A fallback symbol that was recently marked insufficient must also
    # respect the 30-day retry window. Without this filter it would be selected
    # again on every batch and waste one Twelve Data credit each time.
    fallback_missing = [
        s
        for s in fallback_symbols
        if load_cache_meta(s) is None and s not in insufficient
    ]

    # Count fallback symbols that are currently suppressed by the retry window
    # so the log reflects the real number of temporarily skipped symbols.
    skipped_insufficient += sum(
        1
        for s in fallback_symbols
        if s in insufficient and load_cache_meta(s) is None
    )
    if building_cache:
        selected = missing[:max_requests]
        selected_provider = "stashgamma"
    elif fallback_missing:
        selected = fallback_missing[:min(max_requests, TWELVEDATA_DAILY_BUDGET)]
        selected_provider = "twelvedata"
    else:
        selected = stale[:max_requests]
        selected_provider = "mixed"

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

    for index, symbol in enumerate(selected, 1):
        meta = load_cache_meta(symbol)
        refresh = meta is not None
        provider = "stashgamma"

        if selected_provider == "twelvedata" or (
            selected_provider == "mixed"
            and meta
            and meta.get("source") == "twelvedata"
        ):
            provider = "twelvedata"

        # Twelve Data Basic allows 8 API credits/minute. Do not assume
        # one request equals one credit: Api-Credits-Request tells us the
        # actual weight after each call.
        #
        # If the previous response says fewer than 2 credits remain, wait for
        # a full minute window before making another request. This is
        # intentionally conservative because there is no reset timestamp in
        # the response headers.
        if provider == "twelvedata":
            if (
                last_twelvedata_credits_left is not None
                and last_twelvedata_credits_left < TWELVEDATA_SAFE_MIN_LEFT
            ):
                log.info(
                    "Twelve Data credits left=%d；等待 %.0fs 讓 minute quota reset",
                    last_twelvedata_credits_left,
                    TWELVEDATA_CREDIT_RESET_WAIT,
                )
                time.sleep(TWELVEDATA_CREDIT_RESET_WAIT)
                last_twelvedata_credits_left = None
                last_twelvedata_request_credits = None
                last_twelvedata_request = None

            if last_twelvedata_request is not None:
                elapsed = time.monotonic() - last_twelvedata_request
                request_weight = last_twelvedata_request_credits or 1
                # Pace according to the observed request weight. For example,
                # weight=2 => at most 4 requests/minute; weight=1 => 8/minute.
                min_interval = max(
                    TWELVEDATA_MIN_INTERVAL,
                    60.0 / max(1, 8 // request_weight),
                )
                wait = min_interval - elapsed
                if wait > 0:
                    time.sleep(wait)

            last_twelvedata_request = time.monotonic()

        try:
            if selected_provider == "twelvedata":
                result = (
                    data_mod.refresh_twelvedata(symbol)
                    if refresh
                    else data_mod.fetch_twelvedata(symbol, "2y")
                )
            elif selected_provider == "mixed" and meta and meta.get("source") == "twelvedata":
                result = data_mod.refresh_twelvedata(symbol) if refresh else data_mod.fetch_twelvedata(symbol, "2y")
            else:
                result = data_mod.fetch_daily(symbol, "2y", refresh=refresh)

            if result is None:
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
            unavailable_count += 1
            data_mod._mark_unavailable(symbol)
            log.warning("[%d/%d] %s 404/unavailable：%s", index, len(selected), symbol, exc)
        except data_mod.TwelveDataUnavailableError as exc:
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
            rate_limited = True
            log.error("[%d/%d] %s 收到 429，立即停止本批：%s", index, len(selected), symbol, exc)
            break
        except KeyboardInterrupt:
            log.warning("收到 Ctrl-C，保留已完成 cache；下次會繼續")
            raise
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning("[%d/%d] %s 失敗：%s", index, len(selected), symbol, exc)

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

    # Keep the website market radar current from local cache. These fixed
    # instruments are separate from the 250-stock batch and keep the total
    # comfortably below StashGamma's hourly ceiling.
    if not rate_limited:
        refresh_market_cache()

    save_state(
        insufficient_symbols=persistent_insufficient,
        last_run_at=time.time(),
        last_run_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        requested=len(selected),
        success=success,
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
    return 0 if not rate_limited else 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-requests", type=int, default=250)
    ap.add_argument("--stale-days", type=int, default=7)
    ap.add_argument("--pause", type=float, default=0.35)
    args = ap.parse_args()
    raise SystemExit(run(args.max_requests, stale_days=args.stale_days, pause=args.pause))
