from __future__ import annotations
"""Run a small, persistent StashGamma batch for the local Mac scheduler.

The batch is deliberately conservative: at most 250 API requests per run,
cache hits cost zero requests, unavailable symbols are skipped, and a 429
stops the run immediately. During the initial cache-building phase, only
symbols without a valid 2y cache are downloaded; stale-cache refreshes do not
consume the build quota until the snapshot is complete.
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
from screener.main import _cheap_filter, _rank  # noqa: E402

CACHE_DIR = os.path.join(ROOT, ".cache", "market")
LOG_DIR = os.path.join(ROOT, ".cache", "logs")
STATE_PATH = os.path.join(CACHE_DIR, "stashgamma_batch_state.json")

INSUFFICIENT_RETRY_DAYS = 30

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


def candidate_symbols() -> List[str]:
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
    return [r["symbol"] for r in cheap]


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


def get_universe_snapshot(state: dict, current_symbols: List[str]) -> List[str]:
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
        universe_created_at=time.time(),
        universe_created_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    log.info(
        "建立 persistent universe snapshot：%d symbols；之後 cache 建立期間不會因 Nasdaq metadata 變化而縮小",
        len(snapshot),
    )
    return snapshot


def run(max_requests: int = 250, stale_days: int = 7, pause: float = 0.35):
    if not os.environ.get("STASHGAMMA_API_KEY"):
        raise SystemExit("找不到 STASHGAMMA_API_KEY")

    os.makedirs(LOG_DIR, exist_ok=True)
    current_symbols = candidate_symbols()
    state = load_state()
    symbols = get_universe_snapshot(state, current_symbols)

    unavailable = data_mod._load_unavailable()
    state = load_state()
    now = time.time()
    stale_seconds = stale_days * 86400
    insufficient = insufficient_symbols(state, now)

    missing = []
    stale = []
    skipped_insufficient = 0

    for symbol in symbols:
        upper = symbol.upper()
        if upper in unavailable:
            continue
        if upper in insufficient:
            skipped_insufficient += 1
            continue

        meta = load_cache_meta(symbol)
        if meta is None:
            missing.append(symbol)
            continue

        fetched_at = float(meta.get("fetched_at") or 0)
        if now - fetched_at >= stale_seconds:
            stale.append(symbol)

    # IMPORTANT: while the snapshot still has uncached symbols, use the entire
    # request budget only for missing symbols. This prevents stale refreshes
    # from starving the initial cache build.
    building_cache = bool(missing)
    selected_pool = missing if building_cache else stale
    selected = selected_pool[:max_requests]

    log.info(
        "Cache build=%s；snapshot=%d；missing=%d；stale=%d；insufficient 暫時跳過=%d；本批最多=%d；實際處理=%d",
        building_cache,
        len(symbols),
        len(missing),
        len(stale),
        skipped_insufficient,
        max_requests,
        len(selected),
    )

    success = 0
    unavailable_count = 0
    insufficient_count = 0
    rate_limited = False
    failed = 0

    for index, symbol in enumerate(selected, 1):
        meta = load_cache_meta(symbol)
        refresh = meta is not None
        try:
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
                success += 1
                state = load_state()
                insufficient_map = get_insufficient_map(state)
                if symbol.upper() in insufficient_map:
                    insufficient_map.pop(symbol.upper(), None)
                    save_state(insufficient_symbols=insufficient_map)
                log.info(
                    "[%d/%d] %s OK%s",
                    index,
                    len(selected),
                    symbol,
                    " (refresh)" if refresh else "",
                )
        except data_mod.StashGammaUnavailableError as exc:
            unavailable_count += 1
            data_mod._mark_unavailable(symbol)
            log.warning("[%d/%d] %s 404/unavailable：%s", index, len(selected), symbol, exc)
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
        if index < len(selected):
            time.sleep(pause)

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
    raise SystemExit(run(args.max_requests, args.stale_days, args.pause))
