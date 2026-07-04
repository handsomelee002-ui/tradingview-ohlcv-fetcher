"""
Market Movers — TradingView market-movers lists (gainers/losers/volume) -> OHLCV

Pulls a ranked "top N" list (gainers, losers, or most-active-by-volume) for a
market from TradingView's public screener/scanner API, then fetches OHLCV for
those tickers using the same fetch logic as ohlcv_fetcher.py. Saves into its
own sub-folder under data/topx/ so it never mixes with your main
tickers.txt-driven data/ohlcv/ files.

--market takes the exact slug from a TradingView market-movers URL:
    tradingview.com/markets/stocks-malaysia/market-movers-gainers/  -> malaysia
    tradingview.com/markets/stocks-germany/market-movers-volume/    -> germany
    tradingview.com/markets/stocks-usa/market-movers-gainers/       -> usa
Most slugs pass straight to TradingView's scanner backend; a handful of known
exceptions (e.g. "usa" -> backend region "america") are translated internally.

Run:
    python market_movers.py --market malaysia --list volume  --top 30
    python market_movers.py --market malaysia --list losers  --top 100
    python market_movers.py --market taiwan   --list gainers --top 50
    python market_movers.py --market usa      --list volume  --top 30

Output (latest run only, overwritten each time):
    data/topx/<market>_<list>_top<n>/<ticker>.csv       one CSV per stock
    data/topx/<market>_<list>_top<n>/_ranking.csv       the ranked list itself

For individual, non-commercial use.
"""

import re
import time
import logging
from pathlib import Path

import requests

from ohlcv_fetcher import tv_client, fetch_tv, _to_flat, DEFAULT_BARS, RATE_LIMIT_SECONDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOPX_DIR = Path("data/topx")

# Known TradingView URL-slug -> scanner backend region exceptions.
# Everything not listed here is passed straight through unchanged.
MARKET_ALIAS = {
    "usa": "america",
}

# Extra exchange filtering per backend region, to match what TradingView's own
# market-movers page shows (e.g. "america" otherwise includes OTC/pink-sheet
# penny stocks that dominate gainers/losers with meaningless % swings).
EXCHANGE_FILTER = {
    "america": ["NASDAQ", "NYSE", "AMEX"],
}

LIST_SORT = {
    "gainers": ("change", "desc"),
    "losers": ("change", "asc"),
    "volume": ("volume", "desc"),
}

SCAN_COLUMNS = ["name", "close", "volume", "change", "description"]


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def fetch_ranking(market: str, list_type: str, top_n: int) -> list[dict]:
    """Query TradingView's scanner API. Returns a list of dicts, ranked."""
    region = MARKET_ALIAS.get(market, market)
    sort_by, sort_order = LIST_SORT[list_type]

    filters = [{"left": "is_primary", "operation": "equal", "right": True}]
    exchanges = EXCHANGE_FILTER.get(region)
    if exchanges:
        filters.append({"left": "exchange", "operation": "in_range", "right": exchanges})

    payload = {
        "filter": filters,
        "options": {"lang": "en"},
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "columns": SCAN_COLUMNS,
        "sort": {"sortBy": sort_by, "sortOrder": sort_order},
        "range": [0, top_n],
    }

    url = f"https://scanner.tradingview.com/{region}/scan"
    try:
        resp = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach TradingView scanner ({url}): {e}")
    if resp.status_code == 404:
        raise ValueError(
            f"Unknown market {market!r} (backend region {region!r} not found). "
            f"Check the slug against the TradingView URL, e.g. "
            f"tradingview.com/markets/stocks-{market}/market-movers-{list_type}/"
        )
    resp.raise_for_status()

    rows = []
    for item in resp.json().get("data", []):
        ticker = item["s"]
        name, close, volume, change, description = item["d"]
        rows.append({
            "ticker": ticker,
            "name": name,
            "close": close,
            "volume": volume,
            "change": change,
            "description": description,
        })
    if not rows:
        raise ValueError(f"No results for market={market!r} list={list_type!r} (empty scanner response)")
    log.info("Scanner: %d tickers for market=%s list=%s", len(rows), market, list_type)
    return rows


def save_ranking(rows: list[dict], out_dir: Path) -> Path:
    import pandas as pd
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "_ranking.csv"
    df = pd.DataFrame(rows)
    df.insert(0, "rank", range(1, len(df) + 1))
    df.to_csv(path, index=False)
    log.info("Saved ranking -> %s", path)
    return path


# ---------------------------------------------------------------------------
# OHLCV save (same shape as ohlcv_fetcher.save, but into a topx sub-folder)
# ---------------------------------------------------------------------------

def _safe_name(ticker: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", ticker.strip()).strip("_")


def save_topx(ticker: str, df, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_safe_name(ticker)}.csv"
    _to_flat(df, ticker).to_csv(path, index=False)
    log.info("Saved %d rows -> %s", len(df), path)
    return path


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(market: str, list_type: str, top_n: int, bars: int) -> None:
    rows = fetch_ranking(market, list_type, top_n)
    out_dir = TOPX_DIR / f"{market.lower()}_{list_type}_top{top_n}"
    save_ranking(rows, out_dir)

    tv = tv_client()
    ok, failed = 0, []
    for row in rows:
        ticker = row["ticker"]
        try:
            df = fetch_tv(tv, ticker, bars)
            if df is not None:
                save_topx(ticker, df, out_dir)
                ok += 1
            else:
                failed.append(ticker)
        except Exception as e:                      # one bad stock must not abort the batch
            log.error("%s: skipped (%s)", ticker, e)
            failed.append(ticker)
        time.sleep(RATE_LIMIT_SECONDS)

    print(f"\n[done] fetched {ok}/{len(rows)} tickers -> {out_dir}")
    if failed:
        print(f"[failed] {len(failed)}: {', '.join(failed)}")
        print("  (rerun to retry; transient TradingView drops are common on anonymous sessions)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="TradingView market-movers -> OHLCV (per-market top-N lists)")
    p.add_argument("--market", required=True,
                   help="TradingView URL slug, e.g. malaysia, usa, germany, taiwan "
                        "(from tradingview.com/markets/stocks-<market>/...)")
    p.add_argument("--list", required=True, choices=sorted(LIST_SORT),
                   help="Which ranked list to pull")
    p.add_argument("--top", type=int, required=True, help="How many tickers to take from the ranking")
    p.add_argument("--bars", type=int, default=DEFAULT_BARS,
                   help=f"Bars per stock (default {DEFAULT_BARS}, max 5000)")
    args = p.parse_args()

    try:
        run(args.market.strip().lower(), args.list, args.top, args.bars)
    except (ValueError, RuntimeError, KeyError, requests.RequestException) as e:
        print(f"[error] {e}")
        raise SystemExit(1)
