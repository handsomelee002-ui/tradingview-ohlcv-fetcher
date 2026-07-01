"""
OHLCV Fetcher — TradingView (any exchange, incl. Bursa Malaysia / MYX)

Pulls daily OHLCV for any TradingView-listed stock (KLSE, US, etc.) via the
(unofficial) `tvDatafeed` library. For MYX, TradingView's feed is licensed
directly from Bursa Malaysia — official-grade data, reachable automatically with
no manual tokens. Validated to 99.75–100% against Bursa's own site for KLSE
stocks (see bursa_validate.py).

Reads tickers from a file (default tickers.txt, one per line — .txt or .csv),
loops every stock, and saves one clean CSV per stock:

    data/ohlcv/<ticker>.csv   columns: date,ticker,H,L,O,C,V

Tickers use TradingView's own EXCHANGE:SYMBOL format (one format, no guessing):
    MYX:1155      Bursa Malaysia
    NASDAQ:AAPL   US stock
    NYSE:IBM      US stock

Run:
    python ohlcv_fetcher.py                 # full fetch, tickers.txt
    python ohlcv_fetcher.py --tickers MYX:1155
    python ohlcv_fetcher.py --file sample_tickers.csv
    python ohlcv_fetcher.py --update        # top up only recent days
    python ohlcv_fetcher.py --bars 5000     # bars per stock (max 5000 ~= 20yr)

Install:
    pip install pandas
    pip install --upgrade git+https://github.com/rongardF/tvdatafeed.git

Optional TradingView login (more stable, fewer throttles) — env vars:
    set TV_USERNAME=youruser
    set TV_PASSWORD=yourpass

For individual, non-commercial use.
"""

import os
import re
import time
import logging
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OHLCV_DIR = Path("data/ohlcv")
TICKERS_FILE = Path("tickers.txt")
DEFAULT_BARS = 5000            # tvDatafeed hard cap; ~20 years of daily bars
UPDATE_BARS = 30              # small pull for incremental top-up
RATE_LIMIT_SECONDS = 1.0     # pause between stocks (be polite / avoid throttle)
FETCH_RETRIES = 3            # retry transient TradingView drops
RETRY_BACKOFF_SECONDS = 2.0  # wait between retries
DATA_COLUMNS = ["date", "ticker", "H", "L", "O", "C", "V"]


# ---------------------------------------------------------------------------
# Ticker list
# ---------------------------------------------------------------------------

def load_tickers(path: Path = TICKERS_FILE) -> list[str]:
    """One ticker per line. '#' comments and anything after a comma are ignored."""
    if not path.exists():
        raise FileNotFoundError(
            f"Ticker file not found: {path}\n"
            f"Create it (one ticker per line, e.g. MYX:1155) or pass --tickers."
        )
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        t = line.split(",")[0].strip()
        if t:
            out.append(t)
    if not out:
        raise ValueError(f"No tickers found in {path}")
    log.info("Loaded %d tickers from %s", len(out), path)
    return out


def parse_ticker(ticker: str) -> tuple[str, str]:
    """
    Split a TradingView ticker into (exchange, symbol). ONE format only:
        MYX:1155      -> ('MYX', '1155')      Bursa Malaysia
        NASDAQ:AAPL   -> ('NASDAQ', 'AAPL')   US
        NYSE:IBM      -> ('NYSE', 'IBM')
    No suffix guessing — the ticker is used exactly as TradingView expects it.
    """
    t = ticker.strip()
    if ":" not in t:
        raise ValueError(
            f"Ticker must be TradingView format EXCHANGE:SYMBOL "
            f"(e.g. MYX:1155, NASDAQ:AAPL). Got: {ticker!r}"
        )
    ex, sym = t.split(":", 1)
    return ex.strip().upper(), sym.strip().upper()


def _data_path(ticker: str) -> Path:
    """Filesystem-safe CSV name, e.g. MYX:1155 -> MYX_1155.csv, NASDAQ:AAPL -> NASDAQ_AAPL.csv."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", ticker.strip()).strip("_")
    return OHLCV_DIR / f"{safe}.csv"


# ---------------------------------------------------------------------------
# TradingView client
# ---------------------------------------------------------------------------

def tv_client():
    """Build a TvDatafeed client, logging in if TV_USERNAME/TV_PASSWORD are set."""
    try:
        from tvDatafeed import TvDatafeed
    except ImportError:
        raise ImportError(
            "tvDatafeed not installed. Run:\n"
            "  pip install --upgrade git+https://github.com/rongardF/tvdatafeed.git"
        )
    user, pw = os.environ.get("TV_USERNAME"), os.environ.get("TV_PASSWORD")
    if user and pw:
        log.info("TradingView: logging in as %s", user)
        return TvDatafeed(username=user, password=pw)
    log.info("TradingView: anonymous session (set TV_USERNAME/TV_PASSWORD for stability)")
    return TvDatafeed()


def fetch_tv(tv, ticker: str, bars: int) -> pd.DataFrame | None:
    """
    Fetch daily OHLCV from TradingView for any exchange. Returns df indexed by
    date with columns open/high/low/close/volume, or None.
    """
    from tvDatafeed import Interval
    exchange, symbol = parse_ticker(ticker)
    df = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            df = tv.get_hist(symbol=symbol, exchange=exchange,
                             interval=Interval.in_daily, n_bars=bars)
        except Exception as e:
            log.error("%s: fetch error (attempt %d/%d): %s", ticker, attempt, FETCH_RETRIES, e)
            df = None
        if df is not None and not df.empty:
            break
        if attempt < FETCH_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS)
    if df is None or df.empty:
        log.warning("%s: no data after %d attempts (%s:%s)", ticker, FETCH_RETRIES, exchange, symbol)
        return None
    df = df.copy()
    df.index = pd.to_datetime(df.index).normalize()
    df.index.name = "date"
    log.info("%s: %d bars", ticker, len(df))
    return df[["open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# Shape + save
# ---------------------------------------------------------------------------

def _to_flat(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """OHLCV (date-indexed) -> flat rows: date,ticker,H,L,O,C,V."""
    out = pd.DataFrame({
        "date": df.index.strftime("%Y-%m-%d"),
        "ticker": ticker,
        "H": df["high"].round(4),
        "L": df["low"].round(4),
        "O": df["open"].round(4),
        "C": df["close"].round(4),
        "V": df["volume"].round().astype("Int64"),
    })
    out.index = range(len(out))   # drop the named 'date' index (avoids sort ambiguity)
    return out[DATA_COLUMNS].sort_values("date").reset_index(drop=True)


def save(ticker: str, df: pd.DataFrame) -> Path:
    OHLCV_DIR.mkdir(parents=True, exist_ok=True)
    path = _data_path(ticker)
    _to_flat(df, ticker).to_csv(path, index=False)
    log.info("Saved %d rows -> %s", len(df), path)
    return path


def save_merge(ticker: str, df: pd.DataFrame) -> int:
    """Merge new bars into an existing CSV (dedup by date). Returns rows added."""
    path = _data_path(ticker)
    new_flat = _to_flat(df, ticker)
    if not path.exists():
        save(ticker, df)
        return len(new_flat)
    old = pd.read_csv(path, dtype={"date": str})
    merged = (pd.concat([old, new_flat])
              .drop_duplicates(subset="date", keep="last")
              .sort_values("date").reset_index(drop=True))
    OHLCV_DIR.mkdir(parents=True, exist_ok=True)
    merged.to_csv(path, index=False)
    added = len(merged) - len(old)
    log.info("%s: +%d new rows (now %d)", ticker, added, len(merged))
    return added


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(tickers: list[str], bars: int, update: bool) -> None:
    tv = tv_client()
    ok, failed = 0, []
    for ticker in tickers:
        try:
            df = fetch_tv(tv, ticker, UPDATE_BARS if update else bars)
            if df is not None:
                save_merge(ticker, df) if update else save(ticker, df)
                ok += 1
            else:
                failed.append(ticker)
        except Exception as e:                      # one bad stock must not abort the batch
            log.error("%s: skipped (%s)", ticker, e)
            failed.append(ticker)
        time.sleep(RATE_LIMIT_SECONDS)

    action = "updated" if update else "fetched"
    print(f"\n[done] {action} {ok}/{len(tickers)} tickers -> {OHLCV_DIR}")
    if failed:
        print(f"[failed] {len(failed)}: {', '.join(failed)}")
        print("  (rerun to retry; transient TradingView drops are common on anonymous sessions)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="OHLCV fetcher (TradingView, any exchange)")
    p.add_argument("--tickers", nargs="+", default=None,
                   help="Tickers e.g. MYX:1155 NASDAQ:AAPL (overrides --file)")
    p.add_argument("--file", type=Path, default=TICKERS_FILE,
                   help=f"Ticker list file (default: {TICKERS_FILE})")
    p.add_argument("--bars", type=int, default=DEFAULT_BARS,
                   help=f"Bars per stock on full fetch (default {DEFAULT_BARS}, max 5000)")
    p.add_argument("--update", action="store_true",
                   help="Top up recent days into existing CSVs (dedup)")
    args = p.parse_args()

    tickers = args.tickers or load_tickers(args.file)
    run(tickers, bars=args.bars, update=args.update)
