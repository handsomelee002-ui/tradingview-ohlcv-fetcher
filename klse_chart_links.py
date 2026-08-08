"""
KLSE Chart Links — TradingView Malaysia market movers -> KLSEScreener chart links (HTML)

Pulls 9 TradingView Malaysia market-movers lists (unusual volume, active,
most volatile, high beta, best performing, top gainers, top volume,
all-time high, at 52-week high) via TradingView's public scanner API,
merges/dedupes the tickers, resolves each to its KLSEScreener numeric stock
code, and writes a static HTML file of clickable chart links
(https://www.klsescreener.com/v2/charting/chart/<code>), each with a
checkbox you can tick off (state persists across reloads via localStorage).

Data sources (all public, unofficial JSON endpoints — no headless browser):
  - TradingView scanner API: https://scanner.tradingview.com/malaysia/scan
    (same endpoint market_movers.py uses; sort fields below were verified
    against TradingView's own market-movers page descriptions). Most lists
    use an explicit sortBy field; "all_time_high" instead uses TradingView's
    undocumented "preset" field, reproducing one of its own market-movers
    menu categories; "at_52w_high" uses an extra filter condition (close >=
    price_52_week_high) since no working preset name could be found for it
    — see FILTER_LISTS in the code for details. Used only for the 9
    market-movers rankings in step 1.
  - KLSEScreener's live search box endpoint:
    https://www.klsescreener.com/v2/screener/search/<query> -> [{"code","name"}]
  - KLSEScreener's own chart data feed (the same TradingView Charting
    Library UDF endpoint that powers the chart your link opens):
    https://www.klsescreener.com/v2/trading_view/history?symbol=<code>&resolution=D&from=<ts>&to=<ts>

Writes one dated HTML file per run into data/klse_links/ (same data/
convention as ohlcv_fetcher.py and market_movers.py):
    data/klse_links/klse_chart_links_<YYYY-MM-DD>.html  overwritten if rerun same day

For every resolved stock, also pulls ~1.5 years of daily bars from
KLSEScreener's own chart data feed (not TradingView's — deliberately, so the
checks match the exact chart the link opens) and computes seven checks —
bullish candle (close > open), the full SMA stack (close > SMA10 > SMA20 >
SMA60 > SMA200), volume > 5,000,000, a MACD(12,26,9) golden cross (MACD line
currently above its signal line right now — a state, not "crossed on this
exact bar"), close > previous day's close, close >= RM 0.20, and listed at
least 1 year. Only stocks passing all seven are written to the HTML — the
filtering happens here in Python, so the page is already the shortlist.

Run:
    python klse_chart_links.py                       # top 100 per list
    python klse_chart_links.py --top 50

For individual, non-commercial use.
"""

import html
import logging
import sys
from datetime import datetime
from pathlib import Path
import time
from urllib.parse import quote

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stderr)
log = logging.getLogger(__name__)

# dailyRun.bat appends every run to task.log via `>> task.log 2>&1`, so runs
# stack up in one file and need delimiting. Both banners are 25 chars wide.
BANNER_START = "######### START #########"
BANNER_END = "######### END ###########"


def write_banner(text: str) -> None:
    """Banners carry no timestamp, so they bypass the log formatter — but they
    go to the same stream logging uses, and flush, so they cannot drift out of
    order relative to the log lines they wrap."""
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def write_blank_lines(count: int) -> None:
    """Blank lines after a run's END banner, so consecutive runs appended to
    task.log stay visually separated."""
    sys.stderr.write("\n" * count)
    sys.stderr.flush()

OUT_DIR = Path("data/klse_links")
SCANNER_URL = "https://scanner.tradingview.com/malaysia/scan"
KLSE_SEARCH_URL = "https://www.klsescreener.com/v2/screener/search/{}"
KLSE_CHART_URL = "https://www.klsescreener.com/v2/charting/chart/{}"
TV_CHART_URL = "https://www.tradingview.com/chart/?symbol={}"
KLSE_HISTORY_URL = "https://www.klsescreener.com/v2/trading_view/history"
KLSE_RATE_LIMIT_SECONDS = 0.3   # be polite to klsescreener (same host for search + history)
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Sort/metric field per list, verified against TradingView's own page text
# (see market_movers.py for the base scanner-POST pattern this extends).
MOVER_LISTS = {
    "unusual_volume": ("relative_volume_10d_calc", "desc", "Rel. Volume (10d)"),
    "active":         ("Value.Traded",              "desc", "Value Traded"),
    "most_volatile":  ("Volatility.D",               "desc", "Volatility (D)"),
    "high_beta":      ("beta_1_year",                "desc", "Beta (1Y)"),
    "best_performing":("Perf.Y",                     "desc", "Perf 1Y %"),
    "top_gainers":    ("change",                     "desc", "Change %"),
    "top_volume":     ("volume",                     "desc", "Volume"),
}

# TradingView's scanner also accepts an (undocumented) "preset" field matching
# its own market-movers menu categories, applying whatever extra filters
# TradingView's own page uses (e.g. liquidity minimums) — found by inspecting
# https://www.tradingview.com/markets/stocks-malaysia/market-movers-ath/.
# Verified this preset works; couldn't find a working preset name for
# "52-week high" despite extensive trial (its data-fetch config isn't exposed
# anywhere in the page source) — see FILTER_LISTS below for how that one is
# reproduced instead.
PRESET_LISTS = {
    "all_time_high": "All-Time High",
}

# Lists built from an extra scanner filter condition (column-to-column
# comparison) rather than a sort field or preset. "52-week high" has no
# reachable preset name, but the same result is achievable directly: filter
# for close >= price_52_week_high (today's close at/above its 52-week high),
# sorted by volume desc so illiquid/stale-price matches (zero volume today —
# common for thinly-traded stocks whose "52-week high" is just an old,
# never-revisited print) sink to the bottom rather than crowding out real
# breakouts under --top N. Verified live: 0 false positives, sort correctly
# pushes 32/39 zero-volume rows to the end.
FILTER_LISTS = {
    "at_52w_high": (
        {"left": "close", "operation": "egreater", "right": "price_52_week_high"},
        "volume", "desc", "At 52-Week High",
    ),
}

CHECK_MIN_VOLUME = 5_000_000
CHECK_MIN_PRICE = 0.20        # ringgit; stocks trading below this are excluded
CHECK_MIN_LISTED_DAYS = 365   # oldest available bar must be at least this old

# Primary path: one scanner call returns every input all 7 checks need, for the
# whole Bursa main board, so there is no per-ticker fetch loop at all. The 9
# market-movers lists below are only used by the --source klsescreener backup:
# they are 9 orderings of one universe, and every stock that could survive the
# volume gate is already inside this single screen.
SCREEN_COLUMNS = [
    "name", "description", "close", "open", "volume", "change",
    "SMA10", "SMA20", "SMA60", "SMA200",
    "MACD.macd", "MACD.signal", "first_bar_time",
]
CHECK_HISTORY_DAYS = 500  # calendar days back to request; ~330 trading bars, enough for SMA200 and MACD(12,26,9) to converge


# ---------------------------------------------------------------------------
# Step 1 — TradingView scanner
# ---------------------------------------------------------------------------

def fetch_movers_list(list_key: str, top_n: int) -> list[dict]:
    """Query TradingView's scanner API for one market-movers list. Returns ranked rows."""
    sort_by, sort_order, label = MOVER_LISTS[list_key]
    columns = ["name", "close", "volume", "description", sort_by]

    payload = {
        "filter": [{"left": "is_primary", "operation": "equal", "right": True}],
        "options": {"lang": "en"},
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "columns": columns,
        "sort": {"sortBy": sort_by, "sortOrder": sort_order},
        "range": [0, top_n],
    }
    try:
        resp = requests.post(SCANNER_URL, json=payload, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach TradingView scanner ({SCANNER_URL}): {e}")
    resp.raise_for_status()

    rows = []
    for item in resp.json().get("data", []):
        name, close, volume, description, metric = item["d"]
        rows.append({
            "ticker": item["s"],
            "name": name,
            "description": description,
            "metric": metric,
            "metric_label": label,
            "source": list_key,
        })
    log.info("Scanner: %d tickers for list=%s", len(rows), list_key)
    return rows


def fetch_preset_list(list_key: str, top_n: int) -> list[dict]:
    """Query TradingView's scanner API using its "preset" field (TradingView's
    own market-movers category, with whatever extra filters that implies —
    see PRESET_LISTS above). No explicit sort: the preset determines order."""
    label = PRESET_LISTS[list_key]
    payload = {
        "filter": [{"left": "is_primary", "operation": "equal", "right": True}],
        "options": {"lang": "en"},
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "columns": ["name", "close", "volume", "description"],
        "preset": list_key,
        "range": [0, top_n],
    }
    try:
        resp = requests.post(SCANNER_URL, json=payload, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach TradingView scanner ({SCANNER_URL}): {e}")
    resp.raise_for_status()

    rows = []
    for item in resp.json().get("data", []):
        name, close, volume, description = item["d"]
        rows.append({
            "ticker": item["s"],
            "name": name,
            "description": description,
            "metric": None,
            "metric_label": label,
            "source": list_key,
        })
    log.info("Scanner: %d tickers for list=%s", len(rows), list_key)
    return rows


def fetch_filter_list(list_key: str, top_n: int) -> list[dict]:
    """Query TradingView's scanner API with an extra filter condition on top
    of the base is_primary filter (see FILTER_LISTS above)."""
    extra_filter, sort_by, sort_order, label = FILTER_LISTS[list_key]
    columns = ["name", "close", "volume", "description", sort_by]

    payload = {
        "filter": [{"left": "is_primary", "operation": "equal", "right": True}, extra_filter],
        "options": {"lang": "en"},
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "columns": columns,
        "sort": {"sortBy": sort_by, "sortOrder": sort_order},
        "range": [0, top_n],
    }
    try:
        resp = requests.post(SCANNER_URL, json=payload, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach TradingView scanner ({SCANNER_URL}): {e}")
    resp.raise_for_status()

    rows = []
    for item in resp.json().get("data", []):
        name, close, volume, description, metric = item["d"]
        rows.append({
            "ticker": item["s"],
            "name": name,
            "description": description,
            "metric": metric,
            "metric_label": label,
            "source": list_key,
        })
    log.info("Scanner: %d tickers for list=%s", len(rows), list_key)
    return rows


# ---------------------------------------------------------------------------
# Step 2 — merge + dedupe
# ---------------------------------------------------------------------------

def merge_unique(all_rows: list[dict]) -> dict[str, dict]:
    """Merge ranked rows from multiple lists into one dict keyed by ticker (first-seen wins)."""
    merged: dict[str, dict] = {}
    for row in all_rows:
        ticker = row["ticker"]
        if ticker not in merged:
            merged[ticker] = {
                "ticker": ticker,
                "name": row["name"],
                "description": row["description"],
                "sources": [],
            }
        merged[ticker]["sources"].append(row["source"])
    for info in merged.values():
        info["sources"] = sorted(set(info["sources"]))
    return merged


# ---------------------------------------------------------------------------
# Step 3 — KLSEScreener ticker -> numeric code
# ---------------------------------------------------------------------------

def resolve_klse_code(symbol: str) -> str | None:
    """Look up KLSEScreener's numeric code for a bare symbol (e.g. 'MAYBANK' -> '1155').

    Uses KLSEScreener's own live-search endpoint and takes the entry whose
    'name' exactly matches the symbol, to skip warrants/call-warrants that
    share the same prefix (e.g. searching MAYBANK also returns MAYBANKC2H).
    """
    url = KLSE_SEARCH_URL.format(symbol)
    try:
        resp = requests.get(url, timeout=15, headers=HTTP_HEADERS)
    except requests.RequestException as e:
        log.warning("%s: KLSEScreener search failed: %s", symbol, e)
        return None
    if resp.status_code != 200:
        return None
    try:
        candidates = resp.json()
    except ValueError:
        return None
    for item in candidates:
        if item.get("name", "").upper() == symbol.upper():
            return item.get("code")
    return None


def resolve_all(merged: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """Resolve every merged ticker to a KLSE code. Returns (resolved rows, skipped tickers)."""
    resolved, skipped = [], []
    for i, (ticker, info) in enumerate(sorted(merged.items()), 1):
        symbol = ticker.split(":", 1)[1] if ":" in ticker else ticker
        code = resolve_klse_code(symbol)
        if code:
            resolved.append({**info, "code": code})
        else:
            skipped.append(ticker)
            log.warning("%s: no KLSEScreener code found, skipping", ticker)
        if i < len(merged):
            time.sleep(KLSE_RATE_LIMIT_SECONDS)
    log.info("Resolved %d/%d tickers to KLSE codes", len(resolved), len(merged))
    return resolved, skipped


# ---------------------------------------------------------------------------
# Step 5 — bullish / above-SMA10&20 / volume / MACD golden cross checks,
# computed from KLSEScreener's own daily bars (the same TradingView Charting
# Library UDF data feed that powers the /v2/charting/chart/<code> page the
# link opens) — deliberately not TradingView's scanner, so the checks match
# what you see on the chart you actually click through to. Shown as columns
# in the HTML and filterable there, not via a CLI flag.
# ---------------------------------------------------------------------------

EMPTY_CHECKS = {
    "bullish": None, "sma_stack": None, "volume_ok": None, "macd_cross": None,
    "price_up": None, "price_ok": None, "listed_1y": None,
}


def fetch_klse_history(code: str) -> dict | None:
    """Daily OHLCV bars for a KLSE numeric code from KLSEScreener's own chart
    data feed. Returns None on any failure or if the feed reports no data."""
    now = int(time.time())
    params = {
        "symbol": code,
        "resolution": "D",
        "from": now - CHECK_HISTORY_DAYS * 86400,
        "to": now,
    }
    try:
        resp = requests.get(KLSE_HISTORY_URL, params=params, timeout=15, headers=HTTP_HEADERS)
    except requests.RequestException as e:
        log.warning("%s: KLSEScreener history fetch failed: %s", code, e)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if data.get("s") != "ok" or not data.get("c"):
        return None
    return data


def compute_checks(history: dict) -> dict:
    """Each check is True/False, or None if there isn't enough history for
    that specific check — None is treated as "not passing" by the HTML's
    filters. Bullish/volume only need today's bar, so they're still computed
    even when there's too little history for SMA/MACD.

    macd_cross is "golden cross" as a current state — MACD line currently
    above its signal line right now — not "crossed on this exact bar".

    sma_stack is the full four-line alignment (close > SMA10 > SMA20 > SMA60 >
    SMA200), so it needs 200 bars — the strictest history requirement here.

    price_up needs yesterday's bar too (today vs. previous day), so it's None
    with only 1 bar of history.

    listed_1y uses the oldest bar the feed returned: we always ask for
    CHECK_HISTORY_DAYS (500) calendar days, so a stock listed more recently
    than a year ago can only return bars newer than that cutoff."""
    close = pd.Series(history["c"])
    n = len(close)
    bullish = bool(history["c"][-1] > history["o"][-1])
    volume_ok = bool(history["v"][-1] > CHECK_MIN_VOLUME)
    price_ok = bool(history["c"][-1] >= CHECK_MIN_PRICE)

    sma_stack = None
    if n >= 200:
        sma10 = close.rolling(10).mean().iloc[-1]
        sma20 = close.rolling(20).mean().iloc[-1]
        sma60 = close.rolling(60).mean().iloc[-1]
        sma200 = close.rolling(200).mean().iloc[-1]
        sma_stack = bool(close.iloc[-1] > sma10 > sma20 > sma60 > sma200)

    macd_cross = None
    if n >= 35:   # 26-period slow EMA + 9-period signal warm-up
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_cross = bool(macd.iloc[-1] > signal.iloc[-1])

    price_up = None
    if n >= 2:
        price_up = bool(history["c"][-1] > history["c"][-2])

    listed_1y = None
    timestamps = history.get("t")
    if timestamps:
        listed_1y = bool(timestamps[0] <= time.time() - CHECK_MIN_LISTED_DAYS * 86400)

    return {
        "bullish": bullish, "sma_stack": sma_stack, "volume_ok": volume_ok, "macd_cross": macd_cross,
        "price_up": price_up, "price_ok": price_ok, "listed_1y": listed_1y,
    }


def compute_checks_from_scanner(d: dict) -> dict:
    """The same 7 checks as compute_checks(), but read straight off TradingView's
    scanner columns instead of recomputing from raw bars. A column TradingView
    returns as null (young listings have no SMA200, for example) makes that one
    check None, matching compute_checks()'s "not enough history" convention.

    'change' is percent change against the previous close, so change > 0 is the
    same test as close > previous close. 'first_bar_time' is the epoch seconds
    of the stock's oldest daily bar, which is what dates the listing."""
    def val(*keys):
        vals = [d.get(k) for k in keys]
        return None if any(v is None for v in vals) else vals

    pair = val("close", "open")
    bullish = bool(pair[0] > pair[1]) if pair else None

    stack = val("close", "SMA10", "SMA20", "SMA60", "SMA200")
    sma_stack = bool(stack[0] > stack[1] > stack[2] > stack[3] > stack[4]) if stack else None

    vol = val("volume")
    volume_ok = bool(vol[0] > CHECK_MIN_VOLUME) if vol else None

    macd = val("MACD.macd", "MACD.signal")
    macd_cross = bool(macd[0] > macd[1]) if macd else None

    chg = val("change")
    price_up = bool(chg[0] > 0) if chg else None

    px = val("close")
    price_ok = bool(px[0] >= CHECK_MIN_PRICE) if px else None

    first_bar = val("first_bar_time")
    listed_1y = (
        bool(first_bar[0] <= time.time() - CHECK_MIN_LISTED_DAYS * 86400)
        if first_bar else None
    )

    return {
        "bullish": bullish, "sma_stack": sma_stack, "volume_ok": volume_ok,
        "macd_cross": macd_cross, "price_up": price_up, "price_ok": price_ok,
        "listed_1y": listed_1y,
    }


def fetch_screen(top_n: int) -> list[dict]:
    """One scanner call for the whole market, pre-filtered on volume so the
    response contains only stocks that could pass. Both the filter and the
    volume_ok check read the same TradingView `volume` field from the same
    response, so the filter is set to exactly CHECK_MIN_VOLUME with the same
    strict > operator — verified against a direct comparison to return an
    identical set. volume_ok is therefore always True here; it stays in
    CONDITIONS because it is a real condition, is shown in the legend, and is
    still doing work on the --source klsescreener path, where the filter is
    TradingView's and the check is KLSEScreener's.

    Returns rows with checks already attached."""
    payload = {
        "filter": [
            {"left": "is_primary", "operation": "equal", "right": True},
            {"left": "volume", "operation": "greater", "right": CHECK_MIN_VOLUME},
        ],
        "options": {"lang": "en"},
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "columns": SCREEN_COLUMNS,
        "sort": {"sortBy": "volume", "sortOrder": "desc"},
        "range": [0, top_n],
    }
    try:
        resp = requests.post(SCANNER_URL, json=payload, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach TradingView scanner ({SCANNER_URL}): {e}")
    resp.raise_for_status()

    rows = []
    # The response is sorted by volume desc and filtered on the same field, so
    # it is a prefix of the full Bursa volume ranking — position here is the
    # stock's market-wide volume rank for the day, not a rank within a subset.
    for rank, item in enumerate(resp.json().get("data", []), 1):
        d = dict(zip(SCREEN_COLUMNS, item["d"]))
        rows.append({
            "ticker": item["s"],
            "name": d["name"],
            "description": d["description"] or d["name"],
            "source_line": f"#{rank} by volume",
            **compute_checks_from_scanner(d),
        })
    log.info("Scanner screen: %d stocks above the volume floor", len(rows))
    return rows


def annotate_checks(resolved: list[dict]) -> list[dict]:
    """Adds bullish/sma_stack/volume_ok/macd_cross/price_up/price_ok/listed_1y to every resolved row."""
    annotated = []
    for i, row in enumerate(resolved, 1):
        history = fetch_klse_history(row["code"])
        checks = compute_checks(history) if history else EMPTY_CHECKS
        annotated.append({**row, **checks})
        if i % 25 == 0 or i == len(resolved):
            log.info("Checked %d/%d tickers", i, len(resolved))
        if i < len(resolved):
            time.sleep(KLSE_RATE_LIMIT_SECONDS)
    return annotated


# ---------------------------------------------------------------------------
# Step 4 — HTML output
# ---------------------------------------------------------------------------

# (key, label) for the conditions a stock must pass to appear in the output at all.
CONDITIONS = [
    ("bullish", "Bullish Bar (Close > Open)"),
    ("sma_stack", "Close > 四线上扬"),
    ("volume_ok", "Volume > 5,000,000"),
    ("macd_cross", "MACD golden cross"),
    ("price_up", "Close > previous close"),
    ("price_ok", "Price >= RM 0.20"),
    ("listed_1y", "Listed at least 1 year"),
]


def format_run_note(stats: dict | None) -> str:
    """Run summary for the page header: how many stocks survived, out of how
    many screened, from which vendor. Returns '' when stats aren't supplied,
    so the page still builds.

    Deliberately omits the condition count (the legend card right below
    already states it) and the HTTP request count (implementation detail —
    nothing a reader of the watchlist would act on)."""
    if not stats:
        return ""
    return (
        f"{stats['passed']} passed out of {stats['screened']} "
        f"from {html.escape(stats['source'])}."
    )


def build_html(rows: list[dict], stats: dict | None = None) -> str:
    rows = sorted(rows, key=lambda r: r["ticker"])
    body_rows = []
    for row in rows:
        symbol = row["ticker"].split(":", 1)[1] if ":" in row["ticker"] else row["ticker"]
        tv_url = TV_CHART_URL.format(quote(row["ticker"], safe=""))
        # No KLSEScreener code (lookup failed, or a TradingView-only symbol):
        # point the KLSEScreener toggle at TradingView rather than a dead link.
        klse_url = KLSE_CHART_URL.format(row["code"]) if row.get("code") else tv_url
        name = html.escape(row["description"])
        ticker_esc = html.escape(row["ticker"])
        search_text = html.escape(f"{symbol} {row['description']}".lower())
        # The screen path supplies its own complete label (a volume rank, which
        # "From:" would read wrong in front of); the market-movers path still
        # names the lists a stock came from.
        source_line = html.escape(
            row.get("source_line") or f"From: {', '.join(row.get('sources', []))}"
        )

        body_rows.append(f"""      <tr class="row" data-ticker="{ticker_esc}" data-search="{search_text}">
        <td class="check-cell"><input type="checkbox" class="review" data-ticker="{ticker_esc}"></td>
        <td class="note-cell"><button type="button" class="note-btn" data-ticker="{ticker_esc}" title="Add note" aria-label="Add note"><span class="note-icon">&#9998;</span><span class="note-dot"></span></button></td>
        <td class="decision-cell">
          <button type="button" class="decision-summary" data-ticker="{ticker_esc}" data-company="{name}" aria-label="Open decision calendar">Set decision</button>
        </td>
        <td>
          <a class="stock-link" href="{html.escape(klse_url)}" data-klse-url="{html.escape(klse_url)}" data-tv-url="{html.escape(tv_url)}" target="_blank" rel="noopener">{name}</a>
          <div class="sources-line">{source_line}</div>
        </td>
      </tr>""")

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    run_note = format_run_note(stats)
    meta_line = f"Generated {generated}." + (f"<br>{run_note}" if run_note else "")
    legend_items = "\n".join(
        f'        <div class="legend-item"><span class="legend-check">&#10003;</span>{html.escape(label)}</div>'
        for _, label in CONDITIONS
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KLSE Market Movers — Chart Links</title>
<style>
  :root {{
    --surface: #fcfcfb;
    --page: #f9f9f7;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --ink-muted: #898781;
    --gridline: #e1e0d9;
    --border: rgba(11,11,11,0.10);
    --accent: #2a78d6;
    --good: #0ca30c;
    --warning: #c98500;
    --critical: #d03b3b;
    --chip-bg: #f2f1ee;
    --decision-buy-bg: #a8d8b9;
    --decision-buy-ink: #163d2b;
    --decision-watch-bg: #f3c481;
    --decision-watch-ink: #3f2400;
    --decision-ignore-bg: #e78f8f;
    --decision-ignore-ink: #6b1f1f;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --surface: #1a1a19;
      --page: #0d0d0d;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --ink-muted: #898781;
      --gridline: #2c2c2a;
      --border: rgba(255,255,255,0.10);
      --accent: #3987e5;
      --good: #0ca30c;
      --warning: #fab219;
      --critical: #e66767;
      --chip-bg: #242422;
      --decision-buy-bg: #a8d8b9;
      --decision-buy-ink: #163d2b;
      --decision-watch-bg: #f3c481;
      --decision-watch-ink: #3f2400;
      --decision-ignore-bg: #e78f8f;
      --decision-ignore-ink: #6b1f1f;
    }}
  }}
  /* Explicit override from the in-page toggle — wins over prefers-color-scheme
     in both directions (a [data-theme] rule is more specific than the bare
     :root inside the media query above, regardless of source order). */
  :root[data-theme="dark"] {{
    --surface: #1a1a19; --page: #0d0d0d; --ink: #ffffff; --ink-2: #c3c2b7;
    --ink-muted: #898781; --gridline: #2c2c2a; --border: rgba(255,255,255,0.10);
    --accent: #3987e5; --good: #0ca30c; --warning: #fab219; --critical: #e66767;
    --chip-bg: #242422;
    --decision-buy-bg: #a8d8b9; --decision-buy-ink: #163d2b;
    --decision-watch-bg: #f3c481; --decision-watch-ink: #3f2400;
    --decision-ignore-bg: #e78f8f; --decision-ignore-ink: #6b1f1f;
  }}
  :root[data-theme="light"] {{
    --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink-2: #52514e;
    --ink-muted: #898781; --gridline: #e1e0d9; --border: rgba(11,11,11,0.10);
    --accent: #2a78d6; --good: #0ca30c; --warning: #c98500; --critical: #d03b3b;
    --chip-bg: #f2f1ee;
    --decision-buy-bg: #a8d8b9; --decision-buy-ink: #163d2b;
    --decision-watch-bg: #f3c481; --decision-watch-ink: #3f2400;
    --decision-ignore-bg: #e78f8f; --decision-ignore-ink: #6b1f1f;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page); color: var(--ink);
    margin: 0; padding: 2rem 1rem 4rem;
  }}
  .wrap {{ max-width: 900px; margin: 0 auto; }}
  .header-row {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; }}
  h1 {{ font-size: 1.5rem; font-weight: 600; margin: 0 0 0.25rem; }}
  .meta {{ color: var(--ink-2); font-size: 0.875rem; margin: 0 0 1.25rem; }}
  .header-actions {{ display: flex; gap: 0.5rem; flex: none; margin-top: 0.15rem; }}
  .theme-toggle {{
    flex: none; padding: 0.4rem 0.75rem;
    border: 1px solid var(--border); border-radius: 999px;
    background: var(--surface); color: var(--ink-2); font-size: 0.78rem;
    cursor: pointer; font-family: inherit;
  }}
  .theme-toggle:hover {{ color: var(--ink); }}

  .search {{
    width: 100%; padding: 0.55rem 0.8rem; margin-bottom: 0.75rem;
    border: 1px solid var(--border); border-radius: 8px;
    background: var(--surface); color: var(--ink); font-size: 0.9rem;
  }}
  .search:focus {{ outline: 2px solid var(--accent); outline-offset: 1px; }}

  .bulk-row {{ display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; margin: 0 0 0.6rem; }}
  #counter {{ color: var(--ink-2); font-size: 0.85rem; margin: 0; }}
  .bulk-actions {{ display: flex; gap: 0.4rem; flex: none; }}
  .bulk-btn {{
    padding: 0.3rem 0.65rem; border: 1px solid var(--border); border-radius: 999px;
    background: var(--surface); color: var(--ink-2); font-size: 0.78rem;
    cursor: pointer; font-family: inherit;
  }}
  .bulk-btn:hover {{ color: var(--ink); }}
  .bulk-btn.primary {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
  .bulk-btn.primary:hover {{ color: #fff; }}

  .table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface); }}
  thead th {{
    position: sticky; top: 0; background: var(--surface); text-align: left;
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em;
    color: var(--ink-muted); padding: 0.6rem 0.75rem; border-bottom: 1px solid var(--gridline);
    white-space: nowrap;
  }}

  tr.row {{ border-bottom: 1px solid var(--gridline); }}
  tr.row:hover {{ background: var(--chip-bg); }}
  tr.row.checked .stock-link {{ color: var(--ink-muted); text-decoration: line-through; }}
  tr.row.hidden-row {{ display: none; }}
  td {{ padding: 0.6rem 0.75rem; font-size: 0.9rem; vertical-align: middle; }}
  td.check-cell {{ width: 2rem; }}
  input[type="checkbox"] {{ width: 1.05rem; height: 1.05rem; cursor: pointer; }}
  a.stock-link {{ color: var(--ink); text-decoration: none; font-weight: 500; }}
  a.stock-link:hover {{ color: var(--accent); text-decoration: underline; }}

  td.note-cell {{ width: 1.75rem; }}
  .note-btn {{
    position: relative; display: inline-flex; align-items: center; justify-content: center;
    width: 1.6rem; height: 1.6rem; padding: 0; border: none; background: none;
    color: var(--ink-muted); cursor: pointer; border-radius: 6px;
  }}
  .note-btn:hover {{ color: var(--accent); background: var(--chip-bg); }}
  .note-icon {{ font-size: 0.9rem; }}
  .note-dot {{
    display: none; position: absolute; top: 2px; right: 2px;
    width: 6px; height: 6px; border-radius: 50%; background: var(--accent);
  }}
  .note-btn.has-note .note-dot {{ display: block; }}

  td.decision-cell {{ width: 1%; white-space: nowrap; }}
  .decision-summary {{
    display: inline-flex; align-items: center; justify-content: center;
    min-height: 1.75rem; min-width: 5.5rem; padding: 0.25rem 0.55rem;
    border: 1px solid var(--border); border-radius: 999px;
    background: var(--surface); color: var(--ink-muted);
    cursor: pointer; font: inherit; font-size: 0.72rem;
  }}
  .decision-summary:hover {{ color: var(--ink); border-color: currentColor; }}
  .decision-summary.buy {{
    background: var(--decision-buy-bg); border-color: transparent;
    color: var(--decision-buy-ink);
  }}
  .decision-summary.watch {{
    background: var(--decision-watch-bg); border-color: transparent;
    color: var(--decision-watch-ink);
  }}
  .decision-summary.ignore {{
    background: var(--decision-ignore-bg); border-color: transparent;
    color: var(--decision-ignore-ink);
  }}
  tr.row.ignored .stock-link {{ color: var(--ink-muted); }}
  tr.row.ignored .sources-line {{ opacity: 0.65; }}

  .modal-overlay {{
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.45);
    align-items: center; justify-content: center; padding: 1rem; z-index: 10;
  }}
  .modal-overlay.open {{ display: flex; }}
  .modal {{
    width: 100%; max-width: 420px; background: var(--surface); color: var(--ink);
    border: 1px solid var(--border); border-radius: 10px; padding: 1.1rem;
    box-shadow: 0 10px 40px rgba(0,0,0,0.25);
  }}
  .modal h2 {{ margin: 0 0 0.75rem; font-size: 1rem; font-weight: 600; }}
  .note-textarea {{
    width: 100%; padding: 0.6rem 0.7rem; border: 1px solid var(--border); border-radius: 8px;
    background: var(--page); color: var(--ink); font-size: 0.88rem; font-family: inherit;
    resize: vertical;
  }}
  .note-textarea:focus {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
  .modal-actions {{ display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 0.85rem; }}

  .calendar-modal {{ max-width: 450px; }}
  .calendar-help {{ margin: -0.35rem 0 0.8rem; color: var(--ink-2); font-size: 0.8rem; }}
  .calendar-nav {{ display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; margin-bottom: 0.55rem; }}
  .calendar-nav-btn {{
    width: 2rem; height: 2rem; padding: 0; border: 1px solid var(--border);
    border-radius: 999px; background: var(--surface); color: var(--ink);
    cursor: pointer; font: inherit;
  }}
  .calendar-month {{ font-size: 0.92rem; font-weight: 600; }}
  .calendar-weekdays, .calendar-grid {{
    display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 0.3rem;
  }}
  .calendar-weekdays {{ margin-bottom: 0.3rem; }}
  .calendar-weekdays span {{
    text-align: center; color: var(--ink-muted); font-size: 0.67rem; text-transform: uppercase;
  }}
  .calendar-day {{
    aspect-ratio: 1; min-width: 0; padding: 0; border: 1px solid transparent;
    border-radius: 8px; background: var(--chip-bg); color: var(--ink);
    cursor: pointer; font: inherit; font-size: 0.78rem; position: relative;
  }}
  .calendar-day:hover {{ border-color: var(--accent); }}
  .calendar-day.today::after {{
    content: ""; position: absolute; bottom: 3px; left: 50%; transform: translateX(-50%);
    width: 4px; height: 4px; border-radius: 50%; background: currentColor;
  }}
  .calendar-day.buy {{
    background: var(--decision-buy-bg); border-color: transparent;
    color: var(--decision-buy-ink);
  }}
  .calendar-day.watch {{
    background: var(--decision-watch-bg); border-color: transparent;
    color: var(--decision-watch-ink);
  }}
  .calendar-day.ignore {{
    background: var(--decision-ignore-bg); border-color: transparent;
    color: var(--decision-ignore-ink);
  }}
  .calendar-day.selected {{ outline: 3px solid var(--accent); outline-offset: 1px; }}
  .calendar-blank {{ aspect-ratio: 1; }}
  .decision-editor {{
    border-top: 1px solid var(--gridline); margin-top: 0.85rem; padding-top: 0.8rem;
  }}
  .selected-date {{ margin: 0 0 0.55rem; font-size: 0.85rem; color: var(--ink-2); }}
  .status-options {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.4rem; }}
  .status-option input {{ position: absolute; opacity: 0; pointer-events: none; }}
  .status-option span {{
    display: block; padding: 0.42rem 0.35rem; border: 1px solid var(--border);
    border-radius: 8px; text-align: center; color: var(--ink-2);
    cursor: pointer; font-size: 0.8rem;
  }}
  .status-option input:focus-visible + span {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
  .status-option.buy input:checked + span {{
    background: var(--decision-buy-bg); border-color: transparent;
    color: var(--decision-buy-ink); font-weight: 600;
  }}
  .status-option.watch input:checked + span {{
    background: var(--decision-watch-bg); border-color: transparent;
    color: var(--decision-watch-ink); font-weight: 600;
  }}
  .status-option.ignore input:checked + span {{
    background: var(--decision-ignore-bg); border-color: transparent;
    color: var(--decision-ignore-ink); font-weight: 600;
  }}
  .status-option input:disabled + span {{ cursor: not-allowed; opacity: 0.45; }}
  .calendar-status {{ min-height: 1.2rem; margin: 0.55rem 0 0; font-size: 0.76rem; color: var(--ink-2); }}
  .destructive-actions {{ display: flex; gap: 0.4rem; margin-right: auto; }}
  .delete-btn {{ color: var(--critical); }}
  .delete-btn[hidden] {{ display: none; }}
  button:disabled {{ cursor: not-allowed; opacity: 0.5; }}

  .sources-line {{ margin-top: 0.15rem; font-size: 0.78rem; color: var(--ink-muted); }}

  .legend {{
    border: 1px solid var(--border); border-radius: 10px; background: var(--chip-bg);
    padding: 0.85rem 1rem; margin-bottom: 1rem;
  }}
  .legend-title {{
    margin: 0 0 0.6rem; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em;
    color: var(--ink-muted); font-weight: 600;
  }}
  .legend-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 0.45rem 1.25rem;
  }}
  .legend-item {{ display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem; color: var(--ink-2); }}
  .legend-check {{
    display: inline-flex; align-items: center; justify-content: center; flex: none;
    width: 16px; height: 16px; border-radius: 50%; background: var(--good);
    color: #fff; font-size: 0.6rem; font-weight: bold;
  }}

  @media (max-width: 560px) {{
    body {{ padding: 1.25rem 0.6rem 3rem; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="header-row">
      <h1>KLSE Market Movers</h1>
      <div class="header-actions">
        <button type="button" class="theme-toggle" id="sourceToggle">Chart: KLSEScreener</button>
        <button type="button" class="theme-toggle" id="themeToggle">Theme: System</button>
      </div>
    </div>
    <p class="meta">{meta_line}</p>

    <div class="legend">
      <p class="legend-title">Every stock below passes all {len(CONDITIONS)} conditions</p>
      <div class="legend-grid">
{legend_items}
      </div>
    </div>

    <input type="search" class="search" id="search" placeholder="Search ticker or company name&hellip;">

    <div class="bulk-row">
      <div class="bulk-actions">
        <button type="button" class="bulk-btn" id="selectAllBtn">Select all</button>
        <button type="button" class="bulk-btn" id="deselectAllBtn">Deselect all</button>
      </div>
      <p id="counter"></p>
    </div>

    <div class="table-wrap">
      <table id="movers">
        <thead>
          <tr>
            <th></th>
            <th></th>
            <th>Decision dates</th>
            <th>Stock</th>
          </tr>
        </thead>
        <tbody id="tbody">
{chr(10).join(body_rows)}
        </tbody>
      </table>
    </div>
  </div>

  <div class="modal-overlay" id="noteOverlay">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="noteModalTitle">
      <h2 id="noteModalTitle">Note &mdash; <span id="noteModalTicker"></span></h2>
      <textarea id="noteText" class="note-textarea" rows="5" placeholder="Add a note for this stock&hellip;"></textarea>
      <div class="modal-actions">
        <button type="button" class="bulk-btn" id="noteCancelBtn">Cancel</button>
        <button type="button" class="bulk-btn primary" id="noteSaveBtn">Save</button>
      </div>
    </div>
  </div>

  <div class="modal-overlay" id="decisionOverlay">
    <div class="modal calendar-modal" role="dialog" aria-modal="true" aria-labelledby="decisionModalTitle">
      <h2 id="decisionModalTitle"><span id="decisionModalTicker"></span></h2>
      <p class="calendar-help">Select a date and one decision. Changes apply only after Save.</p>
      <div class="calendar-nav">
        <button type="button" class="calendar-nav-btn" id="calendarPrevBtn" aria-label="Previous month">&lsaquo;</button>
        <span class="calendar-month" id="calendarMonth"></span>
        <button type="button" class="calendar-nav-btn" id="calendarNextBtn" aria-label="Next month">&rsaquo;</button>
      </div>
      <div class="calendar-weekdays" aria-hidden="true">
        <span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span><span>Sun</span>
      </div>
      <div class="calendar-grid" id="calendarGrid"></div>
      <div class="decision-editor">
        <p class="selected-date" id="selectedDecisionDate">Select a date</p>
        <div class="status-options" role="radiogroup" aria-label="Decision">
          <label class="status-option buy"><input type="radio" name="decisionStatus" value="buy" disabled><span>Buy</span></label>
          <label class="status-option watch"><input type="radio" name="decisionStatus" value="watch" disabled><span>Watch</span></label>
          <label class="status-option ignore"><input type="radio" name="decisionStatus" value="ignore" disabled><span>Ignore</span></label>
        </div>
        <p class="calendar-status" id="calendarStatus">No unsaved changes.</p>
      </div>
      <div class="modal-actions">
        <div class="destructive-actions">
          <button type="button" class="bulk-btn delete-btn" id="decisionDeleteBtn" hidden>Delete entry</button>
          <button type="button" class="bulk-btn delete-btn" id="decisionClearAllBtn" hidden>Clear all</button>
        </div>
        <button type="button" class="bulk-btn" id="decisionCancelBtn">Cancel</button>
        <button type="button" class="bulk-btn primary" id="decisionSaveBtn" disabled>Save</button>
      </div>
    </div>
  </div>
  <script>
    // Theme toggle: cycles System -> Light -> Dark -> System. "System" means no
    // data-theme attribute at all, so prefers-color-scheme (and any browser
    // extension that flips it) decides. Light/Dark pin an explicit choice,
    // persisted so it survives a reload of this file.
    const THEME_KEY = 'klse_theme';
    const themeToggle = document.getElementById('themeToggle');
    const themeCycle = ['system', 'light', 'dark'];

    function applyTheme(mode) {{
      if (mode === 'system') {{
        delete document.documentElement.dataset.theme;
      }} else {{
        document.documentElement.dataset.theme = mode;
      }}
      themeToggle.textContent = 'Theme: ' + mode[0].toUpperCase() + mode.slice(1);
    }}

    let currentTheme = localStorage.getItem(THEME_KEY) || 'system';
    applyTheme(currentTheme);
    themeToggle.addEventListener('click', () => {{
      currentTheme = themeCycle[(themeCycle.indexOf(currentTheme) + 1) % themeCycle.length];
      localStorage.setItem(THEME_KEY, currentTheme);
      applyTheme(currentTheme);
    }});

    // Chart-source toggle: KLSEScreener <-> TradingView, persisted so the
    // choice survives a reload, same pattern as the theme toggle above.
    const SOURCE_KEY = 'klse_link_source';
    const sourceToggle = document.getElementById('sourceToggle');
    const sourceLabels = {{ klse: 'KLSEScreener', tradingview: 'TradingView' }};

    function applySource(source) {{
      document.querySelectorAll('a.stock-link').forEach(a => {{
        a.href = source === 'tradingview' ? a.dataset.tvUrl : a.dataset.klseUrl;
      }});
      sourceToggle.textContent = 'Chart: ' + sourceLabels[source];
    }}

    let currentSource = localStorage.getItem(SOURCE_KEY) || 'klse';
    applySource(currentSource);
    sourceToggle.addEventListener('click', () => {{
      currentSource = currentSource === 'klse' ? 'tradingview' : 'klse';
      localStorage.setItem(SOURCE_KEY, currentSource);
      applySource(currentSource);
    }});

    const tbody = document.getElementById('tbody');
    const search = document.getElementById('search');
    const counter = document.getElementById('counter');

    function rows() {{
      return Array.from(tbody.querySelectorAll('tr.row'));
    }}

    function keyFor(cb) {{ return 'klse_check_' + cb.dataset.ticker; }}

    function updateCounter() {{
      const boxes = Array.from(document.querySelectorAll('input.review'));
      const checked = boxes.filter(cb => cb.checked).length;
      counter.textContent = checked + ' / ' + boxes.length + ' reviewed';
    }}

    function applyFilters() {{
      const term = search.value.trim().toLowerCase();
      rows().forEach(row => {{
        const show = !term || row.dataset.search.includes(term);
        row.classList.toggle('hidden-row', !show);
      }});
      updateCounter();
    }}

    search.addEventListener('input', applyFilters);

    function setChecked(cb, checked) {{
      cb.checked = checked;
      localStorage.setItem(keyFor(cb), checked ? '1' : '0');
      cb.closest('tr').classList.toggle('checked', checked);
    }}

    document.querySelectorAll('input.review').forEach(cb => {{
      if (localStorage.getItem(keyFor(cb)) === '1') {{
        cb.checked = true;
        cb.closest('tr').classList.add('checked');
      }}
      cb.addEventListener('change', () => {{
        setChecked(cb, cb.checked);
        updateCounter();
      }});
    }});

    function bulkSetVisible(checked) {{
      rows().forEach(row => {{
        if (row.classList.contains('hidden-row')) return;
        const cb = row.querySelector('input.review');
        if (cb) setChecked(cb, checked);
      }});
      updateCounter();
    }}
    document.getElementById('selectAllBtn').addEventListener('click', () => bulkSetVisible(true));
    document.getElementById('deselectAllBtn').addEventListener('click', () => bulkSetVisible(false));

    // Decision history: each date has exactly one Buy, Watch or Ignore value.
    // Editing happens in a draft and localStorage changes only after Save.
    const HISTORY_PREFIX = 'klse_decision_history_';
    const decisionLabels = {{ buy: 'Buy', watch: 'Watch', ignore: 'Ignore' }};
    const validDecisions = new Set(Object.keys(decisionLabels));

    function historyKeyFor(ticker) {{ return HISTORY_PREFIX + ticker; }}

    function loadHistory(ticker) {{
      try {{
        const saved = JSON.parse(localStorage.getItem(historyKeyFor(ticker)) || '{{}}');
        if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return {{}};
        return Object.fromEntries(Object.entries(saved).filter(
          ([date, decision]) => /^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(date) && validDecisions.has(decision)
        ));
      }} catch (e) {{
        return {{}};
      }}
    }}

    function saveHistory(ticker, history) {{
      if (Object.keys(history).length) {{
        localStorage.setItem(historyKeyFor(ticker), JSON.stringify(history));
      }} else {{
        localStorage.removeItem(historyKeyFor(ticker));
      }}
    }}

    function localDateStamp(date = new Date()) {{
      const year = String(date.getFullYear());
      const month = String(date.getMonth() + 1).padStart(2, '0');
      const day = String(date.getDate()).padStart(2, '0');
      return year + '-' + month + '-' + day;
    }}

    function shortDate(stamp) {{
      const parts = stamp.split('-');
      return parts.length === 3 ? parts[2] + '/' + parts[1] : stamp;
    }}

    function dateFromStamp(stamp) {{
      const [year, month, day] = stamp.split('-').map(Number);
      return new Date(year, month - 1, day);
    }}

    function longDate(stamp) {{
      return dateFromStamp(stamp).toLocaleDateString(undefined, {{
        weekday: 'short', year: 'numeric', month: 'short', day: 'numeric'
      }});
    }}

    function normalizedHistory(history) {{
      return JSON.stringify(Object.keys(history).sort().map(date => [date, history[date]]));
    }}

    function refreshDecisionSummary(btn) {{
      const history = loadHistory(btn.dataset.ticker);
      const dates = Object.keys(history).sort();
      const latestDate = dates.length ? dates[dates.length - 1] : '';
      const latestDecision = latestDate ? history[latestDate] : '';
      btn.classList.remove('active', 'buy', 'watch', 'ignore');
      if (latestDecision) {{
        btn.classList.add('active', latestDecision);
        btn.textContent = decisionLabels[latestDecision] + ' ' + shortDate(latestDate);
        btn.title = 'Latest decision: ' + decisionLabels[latestDecision] + ' on ' + latestDate
          + '. Click to open full history.';
        btn.setAttribute('aria-label', 'Open decision calendar. Latest decision: '
          + decisionLabels[latestDecision] + ' on ' + latestDate);
      }} else {{
        btn.textContent = 'Set decision';
        btn.title = 'Open decision calendar';
        btn.setAttribute('aria-label', 'Open decision calendar. No decision set.');
      }}
      btn.closest('tr').classList.toggle('ignored', latestDecision === 'ignore');
    }}

    document.querySelectorAll('.decision-summary').forEach(refreshDecisionSummary);

    const decisionOverlay = document.getElementById('decisionOverlay');
    const decisionModalTicker = document.getElementById('decisionModalTicker');
    const calendarMonthLabel = document.getElementById('calendarMonth');
    const calendarGrid = document.getElementById('calendarGrid');
    const selectedDecisionDateLabel = document.getElementById('selectedDecisionDate');
    const calendarStatus = document.getElementById('calendarStatus');
    const decisionSaveBtn = document.getElementById('decisionSaveBtn');
    const decisionDeleteBtn = document.getElementById('decisionDeleteBtn');
    const decisionClearAllBtn = document.getElementById('decisionClearAllBtn');
    const decisionStatusInputs = Array.from(document.querySelectorAll('input[name="decisionStatus"]'));
    let activeDecisionBtn = null;
    let savedHistory = {{}};
    let draftHistory = {{}};
    let selectedDecisionDate = '';
    let calendarView = new Date();

    function hasUnsavedDecisionChanges() {{
      return normalizedHistory(savedHistory) !== normalizedHistory(draftHistory);
    }}

    function renderDecisionEditor() {{
      const selectedValue = selectedDecisionDate ? draftHistory[selectedDecisionDate] || '' : '';
      selectedDecisionDateLabel.textContent = selectedDecisionDate
        ? longDate(selectedDecisionDate)
        : 'Select a date';
      decisionStatusInputs.forEach(input => {{
        input.disabled = !selectedDecisionDate;
        input.checked = input.value === selectedValue;
      }});
      decisionDeleteBtn.hidden = !selectedValue;
      decisionClearAllBtn.hidden = Object.keys(draftHistory).length === 0;
      const changed = hasUnsavedDecisionChanges();
      decisionSaveBtn.disabled = !changed;
      calendarStatus.textContent = changed
        ? 'Unsaved changes - press Save to apply.'
        : 'No unsaved changes.';
    }}

    function selectDecisionDate(stamp) {{
      if (stamp === selectedDecisionDate && draftHistory[stamp]) {{
        delete draftHistory[stamp];
        renderCalendar();
        renderDecisionEditor();
        return;
      }}
      selectedDecisionDate = stamp;
      renderCalendar();
      renderDecisionEditor();
    }}

    function renderCalendar() {{
      const year = calendarView.getFullYear();
      const month = calendarView.getMonth();
      const firstDay = new Date(year, month, 1);
      const mondayOffset = (firstDay.getDay() + 6) % 7;
      const daysInMonth = new Date(year, month + 1, 0).getDate();
      const today = localDateStamp();
      calendarMonthLabel.textContent = firstDay.toLocaleDateString(undefined, {{
        month: 'long', year: 'numeric'
      }});
      calendarGrid.replaceChildren();

      for (let cell = 0; cell < mondayOffset; cell += 1) {{
        const blank = document.createElement('span');
        blank.className = 'calendar-blank';
        calendarGrid.appendChild(blank);
      }}

      for (let day = 1; day <= daysInMonth; day += 1) {{
        const date = new Date(year, month, day);
        const stamp = localDateStamp(date);
        const decision = draftHistory[stamp] || '';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'calendar-day';
        btn.textContent = String(day);
        btn.dataset.date = stamp;
        btn.setAttribute('aria-label', longDate(stamp)
          + (decision ? ', ' + decisionLabels[decision] : ', no decision'));
        if (decision) btn.classList.add(decision);
        if (stamp === today) btn.classList.add('today');
        if (stamp === selectedDecisionDate) btn.classList.add('selected');
        btn.addEventListener('click', () => selectDecisionDate(stamp));
        calendarGrid.appendChild(btn);
      }}
    }}

    function openDecisionModal(btn) {{
      activeDecisionBtn = btn;
      savedHistory = loadHistory(btn.dataset.ticker);
      draftHistory = {{...savedHistory}};
      calendarView = new Date();
      selectedDecisionDate = '';
      decisionModalTicker.textContent = btn.dataset.ticker + ' — ' + btn.dataset.company;
      renderCalendar();
      renderDecisionEditor();
      decisionOverlay.classList.add('open');
    }}

    function closeDecisionModal() {{
      decisionOverlay.classList.remove('open');
      activeDecisionBtn = null;
      savedHistory = {{}};
      draftHistory = {{}};
      selectedDecisionDate = '';
    }}

    function saveDecisionHistory() {{
      if (!activeDecisionBtn || !hasUnsavedDecisionChanges()) return;
      saveHistory(activeDecisionBtn.dataset.ticker, draftHistory);
      refreshDecisionSummary(activeDecisionBtn);
      closeDecisionModal();
    }}

    document.querySelectorAll('.decision-summary').forEach(btn => {{
      btn.addEventListener('click', (e) => {{
        e.stopPropagation();
        openDecisionModal(btn);
      }});
    }});

    decisionStatusInputs.forEach(input => {{
      input.addEventListener('change', () => {{
        if (!selectedDecisionDate || !input.checked) return;
        draftHistory[selectedDecisionDate] = input.value;
        renderCalendar();
        renderDecisionEditor();
      }});
    }});

    document.getElementById('calendarPrevBtn').addEventListener('click', () => {{
      calendarView = new Date(calendarView.getFullYear(), calendarView.getMonth() - 1, 1);
      selectedDecisionDate = '';
      renderCalendar();
      renderDecisionEditor();
    }});
    document.getElementById('calendarNextBtn').addEventListener('click', () => {{
      calendarView = new Date(calendarView.getFullYear(), calendarView.getMonth() + 1, 1);
      selectedDecisionDate = '';
      renderCalendar();
      renderDecisionEditor();
    }});
    decisionDeleteBtn.addEventListener('click', () => {{
      if (!selectedDecisionDate || !draftHistory[selectedDecisionDate]) return;
      delete draftHistory[selectedDecisionDate];
      renderCalendar();
      renderDecisionEditor();
    }});
    decisionClearAllBtn.addEventListener('click', () => {{
      draftHistory = {{}};
      selectedDecisionDate = '';
      renderCalendar();
      renderDecisionEditor();
    }});
    decisionSaveBtn.addEventListener('click', saveDecisionHistory);
    document.getElementById('decisionCancelBtn').addEventListener('click', closeDecisionModal);
    decisionOverlay.addEventListener('click', (e) => {{
      if (e.target === decisionOverlay) closeDecisionModal();
    }});

    // Per-stock notes: stored in localStorage per ticker, edited via a popup
    // modal (opened from the pencil icon next to each row's checkbox).
    function noteKeyFor(ticker) {{ return 'klse_note_' + ticker; }}

    function refreshNoteDot(btn) {{
      const note = localStorage.getItem(noteKeyFor(btn.dataset.ticker));
      btn.classList.toggle('has-note', !!note);
    }}

    document.querySelectorAll('.note-btn').forEach(refreshNoteDot);

    const noteOverlay = document.getElementById('noteOverlay');
    const noteModalTicker = document.getElementById('noteModalTicker');
    const noteText = document.getElementById('noteText');
    let activeNoteBtn = null;

    function openNoteModal(btn) {{
      activeNoteBtn = btn;
      noteModalTicker.textContent = btn.dataset.ticker;
      noteText.value = localStorage.getItem(noteKeyFor(btn.dataset.ticker)) || '';
      noteOverlay.classList.add('open');
      noteText.focus();
    }}

    function closeNoteModal() {{
      noteOverlay.classList.remove('open');
      activeNoteBtn = null;
    }}

    function saveNote() {{
      if (!activeNoteBtn) return;
      const key = noteKeyFor(activeNoteBtn.dataset.ticker);
      const value = noteText.value.trim();
      if (value) {{
        localStorage.setItem(key, value);
      }} else {{
        localStorage.removeItem(key);
      }}
      refreshNoteDot(activeNoteBtn);
      closeNoteModal();
    }}

    document.querySelectorAll('.note-btn').forEach(btn => {{
      btn.addEventListener('click', (e) => {{
        e.stopPropagation();
        openNoteModal(btn);
      }});
    }});

    document.getElementById('noteSaveBtn').addEventListener('click', saveNote);
    document.getElementById('noteCancelBtn').addEventListener('click', closeNoteModal);
    noteOverlay.addEventListener('click', (e) => {{
      if (e.target === noteOverlay) closeNoteModal();
    }});
    document.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape' && noteOverlay.classList.contains('open')) closeNoteModal();
      if (e.key === 'Escape' && decisionOverlay.classList.contains('open')) closeDecisionModal();
    }});

    updateCounter();
  </script>
</body>
</html>
"""


def save_html(rows: list[dict], stats: dict | None = None) -> Path:
    """Writes a dated daily snapshot into OUT_DIR (overwritten if rerun same day)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dated_path = OUT_DIR / f"klse_chart_links_{datetime.now():%Y-%m-%d}.html"
    dated_path.write_text(build_html(rows, stats), encoding="utf-8")
    log.info("[DONE] Saved %d stock(s) -> %s", len(rows), dated_path)
    return dated_path


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def passes_all(row: dict) -> bool:
    return all(row.get(key) is True for key, _ in CONDITIONS)


def run_tradingview(top_n: int) -> tuple[list[dict], dict]:
    """Primary path: one scanner call for candidates *and* checks."""
    candidates = fetch_screen(top_n)
    qualified = [r for r in candidates if passes_all(r)]
    stats = {"screened": len(candidates), "passed": len(qualified), "source": "TradingView"}
    return qualified, stats


def run_klsescreener(top_n: int) -> tuple[list[dict], dict]:
    """Backup path: the 9 market-movers lists, with every check recomputed from
    KLSEScreener's own bars. Kept because it is the only path whose numbers are
    guaranteed to match a KLSEScreener chart exactly, but it costs two
    rate-limited HTTP calls per candidate instead of one call in total."""
    all_rows = []
    for list_key in MOVER_LISTS:
        all_rows.extend(fetch_movers_list(list_key, top_n))
    for list_key in PRESET_LISTS:
        all_rows.extend(fetch_preset_list(list_key, top_n))
    for list_key in FILTER_LISTS:
        all_rows.extend(fetch_filter_list(list_key, top_n))

    total_lists = len(MOVER_LISTS) + len(PRESET_LISTS) + len(FILTER_LISTS)
    merged = merge_unique(all_rows)
    log.info("Merged %d unique tickers from %d ranked rows across %d lists",
             len(merged), len(all_rows), total_lists)

    resolved, code_skipped = resolve_all(merged)
    if code_skipped:
        log.warning("%d ticker(s) had no KLSEScreener code match: %s",
                    len(code_skipped), ", ".join(code_skipped))

    resolved = annotate_checks(resolved)

    qualified = [r for r in resolved if passes_all(r)]
    stats = {"screened": len(resolved), "passed": len(qualified), "source": "KLSEScreener"}
    return qualified, stats


def run(top_n: int, source: str = "tradingview") -> None:
    qualified, stats = run_tradingview(top_n) if source == "tradingview" else run_klsescreener(top_n)

    # Chart links need KLSEScreener's numeric code, but only for stocks that
    # already qualified — resolving the whole candidate list first was most of
    # the old runtime. Rows that fail to resolve keep working: build_html falls
    # back to the TradingView link for them.
    for row in qualified:
        if row.get("code"):
            continue
        symbol = row["ticker"].split(":", 1)[1] if ":" in row["ticker"] else row["ticker"]
        row["code"] = resolve_klse_code(symbol)
        if not row["code"]:
            log.warning("%s: no KLSEScreener code; link falls back to TradingView", row["ticker"])
        time.sleep(KLSE_RATE_LIMIT_SECONDS)

    unresolved = [r["ticker"] for r in qualified if not r.get("code")]
    if unresolved:
        log.warning("%d qualifier(s) link to TradingView only: %s",
                    len(unresolved), ", ".join(unresolved))

    save_html(qualified, stats)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="TradingView Malaysia market movers -> KLSEScreener chart links (HTML)")
    p.add_argument("--top", type=int, default=500,
                   help="Cap on candidates: the whole screen for --source tradingview, "
                        "or per-list before merging for --source klsescreener (default 500)")
    p.add_argument("--source", choices=["tradingview", "klsescreener"], default="tradingview",
                   help="Where candidates and checks come from. 'tradingview' (default) is one "
                        "request total; 'klsescreener' is the slower backup that recomputes every "
                        "check from KLSEScreener's own bars (default tradingview)")
    args = p.parse_args()

    # The END banner is in a finally block on purpose: a run that dies partway
    # must still close its block, or every later run in task.log reads as part
    # of the failed one.
    write_banner(BANNER_START)
    try:
        run(args.top, args.source)
    except (ValueError, RuntimeError, requests.RequestException) as e:
        log.error("%s", e)
        raise SystemExit(1)
    finally:
        write_banner(BANNER_END)
        write_blank_lines(2)
