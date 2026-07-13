"""
KLSE Chart Links — TradingView Malaysia market movers -> KLSEScreener chart links (HTML)

Pulls 5 TradingView Malaysia market-movers lists (unusual volume, active,
most volatile, high beta, best performing) via TradingView's public scanner
API, merges/dedupes the tickers, resolves each to its KLSEScreener numeric
stock code, and writes a static HTML file of clickable chart links
(https://www.klsescreener.com/v2/charting/chart/<code>), each with a
checkbox you can tick off (state persists across reloads via localStorage).

Data sources (all public, unofficial JSON endpoints — no headless browser):
  - TradingView scanner API: https://scanner.tradingview.com/malaysia/scan
    (same endpoint market_movers.py uses; sort fields below were verified
    against TradingView's own market-movers page descriptions) — used only
    for the 5 market-movers rankings in step 1.
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
checks match the exact chart the link opens) and computes six checks —
bullish candle (close > open), close above BOTH SMA10 and SMA20, volume >
2,000,000, a MACD(12,26,9) golden cross (MACD line currently above its
signal line right now — a state, not "crossed on this exact bar"), volume
>= 1.5x the previous day's volume, and close > previous day's close.
Written as columns in the HTML with a checkbox per column so you can filter
the page itself (client-side, no rerun needed) rather than via a CLI flag.

Run:
    python klse_chart_links.py                       # top 100 per list
    python klse_chart_links.py --top 50

For individual, non-commercial use.
"""

import html
import logging
from datetime import datetime
from pathlib import Path
import time

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OUT_DIR = Path("data/klse_links")
SCANNER_URL = "https://scanner.tradingview.com/malaysia/scan"
KLSE_SEARCH_URL = "https://www.klsescreener.com/v2/screener/search/{}"
KLSE_CHART_URL = "https://www.klsescreener.com/v2/charting/chart/{}"
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
}

CHECK_MIN_VOLUME = 2_000_000
CHECK_HISTORY_DAYS = 500  # calendar days back to request; ~330 trading bars, plenty for MACD(12,26,9) to converge


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

VOLUME_SURGE_MULTIPLE = 1.5   # today's volume must be >= this many times yesterday's

EMPTY_CHECKS = {
    "bullish": None, "above_sma": None, "volume_ok": None, "macd_cross": None,
    "volume_surge": None, "price_up": None,
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

    volume_surge/price_up need yesterday's bar too (today vs. previous day),
    so they're None with only 1 bar of history, same as the others."""
    close = pd.Series(history["c"])
    n = len(close)
    bullish = bool(history["c"][-1] > history["o"][-1])
    volume_ok = bool(history["v"][-1] > CHECK_MIN_VOLUME)

    above_sma = None
    if n >= 20:
        sma10 = close.rolling(10).mean().iloc[-1]
        sma20 = close.rolling(20).mean().iloc[-1]
        above_sma = bool(close.iloc[-1] > sma10 and close.iloc[-1] > sma20)

    macd_cross = None
    if n >= 35:   # 26-period slow EMA + 9-period signal warm-up
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_cross = bool(macd.iloc[-1] > signal.iloc[-1])

    volume_surge = None
    price_up = None
    if n >= 2:
        volume_surge = bool(history["v"][-1] >= VOLUME_SURGE_MULTIPLE * history["v"][-2])
        price_up = bool(history["c"][-1] > history["c"][-2])

    return {
        "bullish": bullish, "above_sma": above_sma, "volume_ok": volume_ok, "macd_cross": macd_cross,
        "volume_surge": volume_surge, "price_up": price_up,
    }


def annotate_checks(resolved: list[dict]) -> list[dict]:
    """Adds bullish/above_sma/volume_ok/macd_cross/volume_surge/price_up to every resolved row."""
    annotated = []
    for i, row in enumerate(resolved, 1):
        history = fetch_klse_history(row["code"])
        checks = compute_checks(history) if history else EMPTY_CHECKS
        annotated.append({**row, **checks})
        if i % 25 == 0 or i == len(resolved):
            print(f"[checks] {i}/{len(resolved)} tickers checked")
        if i < len(resolved):
            time.sleep(KLSE_RATE_LIMIT_SECONDS)
    return annotated


# ---------------------------------------------------------------------------
# Step 4 — HTML output
# ---------------------------------------------------------------------------

def _check_attr(value: bool | None) -> str:
    """'1'/'0'/'' for the row's data-* attribute (client-side filters treat '' as not-passing)."""
    return "" if value is None else ("1" if value else "0")


# (key, data-attribute suffix, label) for the 6 conditions, in display order.
CONDITIONS = [
    ("bullish", "bullish", "Bullish"),
    ("above_sma", "sma", "Close > SMA10 & SMA20"),
    ("volume_ok", "volume", "Volume > 2,000,000"),
    ("macd_cross", "macd", "MACD golden cross"),
    ("volume_surge", "volsurge", "Volume > 1.5× previous day"),
    ("price_up", "priceup", "Close > previous close"),
]


def _condition_chip(value: bool | None, label: str) -> str:
    cls = "unk" if value is None else ("ok" if value else "no")
    mark = "&#8213;" if value is None else ("&#10003;" if value else "&#10007;")
    return f'<div class="cond"><span class="cond-dot {cls}">{mark}</span><span class="cond-label">{html.escape(label)}</span></div>'


def _score_badge(score: int, total: int) -> str:
    tier = "good" if score >= 5 else ("warn" if score >= 3 else "crit")
    return f'<span class="score-badge"><span class="score-dot {tier}"></span>{score}/{total}</span>'


def build_html(rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda r: r["ticker"])
    body_rows = []
    for row in rows:
        symbol = row["ticker"].split(":", 1)[1] if ":" in row["ticker"] else row["ticker"]
        url = KLSE_CHART_URL.format(row["code"])
        name = html.escape(f"{symbol} — {row['description']}")
        ticker_esc = html.escape(row["ticker"])
        search_text = html.escape(f"{symbol} {row['description']}".lower())
        score = sum(1 for key, _, _ in CONDITIONS if row.get(key) is True)

        data_attrs = " ".join([f'data-ticker="{ticker_esc}"', f'data-search="{search_text}"', f'data-score="{score}"'] + [
            f'data-{attr}="{_check_attr(row.get(key))}"' for key, attr, _ in CONDITIONS
        ])
        chips = "".join(_condition_chip(row.get(key), label) for key, _, label in CONDITIONS)
        sources = html.escape(", ".join(row["sources"]))

        body_rows.append(f"""      <tr class="row" {data_attrs}>
        <td class="check-cell"><input type="checkbox" class="review" data-ticker="{ticker_esc}"></td>
        <td><a class="stock-link" href="{html.escape(url)}" target="_blank" rel="noopener">{name}</a></td>
        <td>{_score_badge(score, len(CONDITIONS))}</td>
        <td class="chevron">&#9656;</td>
      </tr>
      <tr class="detail">
        <td colspan="4">
          <div class="detail-grid">{chips}</div>
          <div class="sources-line">From: {sources}</div>
        </td>
      </tr>""")

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

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
  }}
  :root[data-theme="light"] {{
    --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink-2: #52514e;
    --ink-muted: #898781; --gridline: #e1e0d9; --border: rgba(11,11,11,0.10);
    --accent: #2a78d6; --good: #0ca30c; --warning: #c98500; --critical: #d03b3b;
    --chip-bg: #f2f1ee;
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
  .theme-toggle {{
    flex: none; padding: 0.4rem 0.75rem; margin-top: 0.15rem;
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

  .chips {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.9rem; }}
  .chip {{
    display: inline-flex; align-items: center; gap: 0.35rem;
    padding: 0.35rem 0.7rem; border: 1px solid var(--border); border-radius: 999px;
    background: var(--surface); color: var(--ink-2); font-size: 0.8rem;
    cursor: pointer; user-select: none; font-family: inherit;
  }}
  .chip[aria-pressed="true"] {{ background: var(--accent); border-color: var(--accent); color: #fff; }}

  .bulk-row {{ display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; margin: 0 0 0.6rem; }}
  #counter {{ color: var(--ink-2); font-size: 0.85rem; margin: 0; }}
  .bulk-actions {{ display: flex; gap: 0.4rem; flex: none; }}
  .bulk-btn {{
    padding: 0.3rem 0.65rem; border: 1px solid var(--border); border-radius: 999px;
    background: var(--surface); color: var(--ink-2); font-size: 0.78rem;
    cursor: pointer; font-family: inherit;
  }}
  .bulk-btn:hover {{ color: var(--ink); }}

  .table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface); }}
  thead th {{
    position: sticky; top: 0; background: var(--surface); text-align: left;
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em;
    color: var(--ink-muted); padding: 0.6rem 0.75rem; border-bottom: 1px solid var(--gridline);
    white-space: nowrap;
  }}
  th.sortable {{ cursor: pointer; }}
  th.sortable:hover {{ color: var(--ink-2); }}
  th.sort-active {{ color: var(--ink); }}

  tr.row {{ border-bottom: 1px solid var(--gridline); cursor: pointer; }}
  tr.row:hover {{ background: var(--chip-bg); }}
  tr.row.checked .stock-link {{ color: var(--ink-muted); text-decoration: line-through; }}
  tr.row.hidden-row, tr.row.hidden-row + tr.detail {{ display: none; }}
  td {{ padding: 0.6rem 0.75rem; font-size: 0.9rem; vertical-align: middle; }}
  td.check-cell {{ width: 2rem; }}
  input[type="checkbox"] {{ width: 1.05rem; height: 1.05rem; cursor: pointer; }}
  a.stock-link {{ color: var(--ink); text-decoration: none; font-weight: 500; }}
  a.stock-link:hover {{ color: var(--accent); text-decoration: underline; }}

  td.chevron {{ width: 1.5rem; text-align: center; color: var(--ink-muted); transition: transform 0.15s; }}
  tr.row.open td.chevron {{ transform: rotate(90deg); }}

  .score-badge {{
    display: inline-flex; align-items: center; gap: 0.4rem;
    padding: 0.2rem 0.6rem; border-radius: 999px; border: 1px solid var(--border);
    font-variant-numeric: tabular-nums; font-weight: 600; font-size: 0.85rem;
  }}
  .score-dot {{ width: 8px; height: 8px; border-radius: 50%; flex: none; }}
  .score-dot.good {{ background: var(--good); }}
  .score-dot.warn {{ background: var(--warning); }}
  .score-dot.crit {{ background: var(--critical); }}

  tr.detail {{ display: none; }}
  tr.detail.open {{ display: table-row; }}
  tr.detail td {{ padding: 0.85rem 0.75rem; background: var(--chip-bg); border-bottom: 1px solid var(--gridline); }}
  .detail-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 0.5rem 1rem; }}
  .cond {{ display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem; }}
  .cond-dot {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 16px; height: 16px; border-radius: 50%; flex: none;
    font-size: 0.6rem; font-weight: bold; color: #fff;
  }}
  .cond-dot.ok {{ background: var(--good); }}
  .cond-dot.no {{ background: var(--critical); }}
  .cond-dot.unk {{ background: var(--ink-muted); }}
  .cond-label {{ color: var(--ink-2); }}
  .sources-line {{ margin-top: 0.75rem; font-size: 0.78rem; color: var(--ink-muted); }}

  @media (max-width: 560px) {{
    body {{ padding: 1.25rem 0.6rem 3rem; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="header-row">
      <h1>KLSE Market Movers</h1>
      <button type="button" class="theme-toggle" id="themeToggle">Theme: System</button>
    </div>
    <p class="meta">Generated {generated} &middot; {len(rows)} stocks &middot; click a row for the full breakdown
      of its 6 conditions &middot; "&#8213;" means a condition couldn't be computed (insufficient history
      or a fetch error).</p>

    <input type="search" class="search" id="search" placeholder="Search ticker or company name&hellip;">

    <div class="chips" role="group" aria-label="Condition filters">
      <button type="button" class="chip" data-filter="bullish" aria-pressed="false">Bullish</button>
      <button type="button" class="chip" data-filter="sma" aria-pressed="false">&gt;SMA10&amp;20</button>
      <button type="button" class="chip" data-filter="volume" aria-pressed="false">Vol&gt;2M</button>
      <button type="button" class="chip" data-filter="macd" aria-pressed="false">MACD Cross</button>
      <button type="button" class="chip" data-filter="volsurge" aria-pressed="false">Vol&gt;1.5&times; Prev</button>
      <button type="button" class="chip" data-filter="priceup" aria-pressed="false">Price&gt;Prev</button>
    </div>

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
            <th class="sortable" data-sort="stock">Stock</th>
            <th class="sortable" data-sort="score">Score</th>
            <th></th>
          </tr>
        </thead>
        <tbody id="tbody">
{chr(10).join(body_rows)}
        </tbody>
      </table>
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

    const tbody = document.getElementById('tbody');
    const search = document.getElementById('search');
    const chips = Array.from(document.querySelectorAll('.chip'));
    const counter = document.getElementById('counter');
    const headers = Array.from(document.querySelectorAll('th.sortable'));

    function rowPairs() {{
      return Array.from(tbody.querySelectorAll('tr.row')).map(row => [row, row.nextElementSibling]);
    }}

    function keyFor(cb) {{ return 'klse_check_' + cb.dataset.ticker; }}

    function updateCounter() {{
      const pairs = rowPairs();
      const visible = pairs.filter(([row]) => !row.classList.contains('hidden-row'));
      const boxes = Array.from(document.querySelectorAll('input.review'));
      const checked = boxes.filter(cb => cb.checked).length;
      counter.textContent = checked + ' / ' + boxes.length + ' reviewed · ' + visible.length + ' / ' + pairs.length + ' shown';
    }}

    function applyFilters() {{
      const term = search.value.trim().toLowerCase();
      const active = chips.filter(c => c.getAttribute('aria-pressed') === 'true').map(c => c.dataset.filter);
      rowPairs().forEach(([row]) => {{
        let show = true;
        if (term && !row.dataset.search.includes(term)) show = false;
        for (const f of active) {{
          if (row.dataset[f] !== '1') show = false;
        }}
        row.classList.toggle('hidden-row', !show);
      }});
      updateCounter();
    }}

    search.addEventListener('input', applyFilters);
    chips.forEach(chip => chip.addEventListener('click', () => {{
      chip.setAttribute('aria-pressed', chip.getAttribute('aria-pressed') !== 'true');
      applyFilters();
    }}));

    let sortState = {{ key: null, dir: 1 }};
    function applySort(key) {{
      sortState.dir = (sortState.key === key) ? -sortState.dir : 1;
      sortState.key = key;
      headers.forEach(h => h.classList.toggle('sort-active', h.dataset.sort === key));
      const pairs = rowPairs();
      pairs.sort((a, b) => {{
        const [rowA] = a, [rowB] = b;
        if (key === 'score') {{
          return (Number(rowA.dataset.score) - Number(rowB.dataset.score)) * sortState.dir;
        }}
        return rowA.dataset.search.localeCompare(rowB.dataset.search) * sortState.dir;
      }});
      pairs.forEach(([row, detail]) => {{ tbody.appendChild(row); tbody.appendChild(detail); }});
    }}
    headers.forEach(h => h.addEventListener('click', () => applySort(h.dataset.sort)));

    tbody.addEventListener('click', (e) => {{
      if (e.target.closest('input.review') || e.target.closest('a.stock-link')) return;
      const row = e.target.closest('tr.row');
      if (!row) return;
      row.classList.toggle('open');
      row.nextElementSibling.classList.toggle('open');
    }});

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
      cb.addEventListener('click', (e) => e.stopPropagation());
      cb.addEventListener('change', () => {{
        setChecked(cb, cb.checked);
        updateCounter();
      }});
    }});

    function bulkSetVisible(checked) {{
      rowPairs().forEach(([row]) => {{
        if (row.classList.contains('hidden-row')) return;
        const cb = row.querySelector('input.review');
        if (cb) setChecked(cb, checked);
      }});
      updateCounter();
    }}
    document.getElementById('selectAllBtn').addEventListener('click', () => bulkSetVisible(true));
    document.getElementById('deselectAllBtn').addEventListener('click', () => bulkSetVisible(false));

    updateCounter();
  </script>
</body>
</html>
"""


def save_html(rows: list[dict]) -> Path:
    """Writes a dated daily snapshot into OUT_DIR (overwritten if rerun same day)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dated_path = OUT_DIR / f"klse_chart_links_{datetime.now():%Y-%m-%d}.html"
    dated_path.write_text(build_html(rows), encoding="utf-8")
    log.info("Saved %d links -> %s", len(rows), dated_path)
    return dated_path


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(top_n: int) -> None:
    all_rows = []
    for list_key in MOVER_LISTS:
        all_rows.extend(fetch_movers_list(list_key, top_n))

    merged = merge_unique(all_rows)
    print(f"[merge] {len(merged)} unique tickers from {len(all_rows)} ranked rows across {len(MOVER_LISTS)} lists")

    resolved, code_skipped = resolve_all(merged)
    if code_skipped:
        print(f"[skipped] {len(code_skipped)} ticker(s) had no KLSEScreener code match: {', '.join(code_skipped)}")

    resolved = annotate_checks(resolved)

    dated_path = save_html(resolved)

    print(f"\n[done] {len(resolved)} stock(s) -> {dated_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="TradingView Malaysia market movers -> KLSEScreener chart links (HTML)")
    p.add_argument("--top", type=int, default=100,
                   help="How many tickers to take per list before merging (default 100)")
    args = p.parse_args()

    try:
        run(args.top)
    except (ValueError, RuntimeError, requests.RequestException) as e:
        print(f"[error] {e}")
        raise SystemExit(1)
