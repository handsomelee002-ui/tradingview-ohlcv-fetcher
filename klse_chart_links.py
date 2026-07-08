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
checks match the exact chart the link opens) and computes four checks —
bullish candle (close > open), close above BOTH SMA10 and SMA20, volume >
2,000,000, and a MACD(12,26,9) golden cross (MACD line currently above its
signal line right now — a state, not "crossed on this exact bar"). Written
as columns in the HTML with a checkbox per column so you can filter the
page itself (client-side, no rerun needed) rather than via a CLI flag.

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

EMPTY_CHECKS = {"bullish": None, "above_sma": None, "volume_ok": None, "macd_cross": None}


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
    above its signal line right now — not "crossed on this exact bar"."""
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

    return {"bullish": bullish, "above_sma": above_sma, "volume_ok": volume_ok, "macd_cross": macd_cross}


def annotate_checks(resolved: list[dict]) -> list[dict]:
    """Adds bullish/above_sma/volume_ok/macd_cross to every resolved row."""
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


def _check_cell(value: bool | None) -> str:
    if value is None:
        return '<td class="chk unknown">—</td>'
    return '<td class="chk ok">&#10003;</td>' if value else '<td class="chk no">&#10007;</td>'


def build_html(rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda r: r["ticker"])
    body_rows = []
    for row in rows:
        symbol = row["ticker"].split(":", 1)[1] if ":" in row["ticker"] else row["ticker"]
        url = KLSE_CHART_URL.format(row["code"])
        label = html.escape(f"{symbol} — {row['description']}")
        sources = html.escape(", ".join(row["sources"]))
        data_attrs = (
            f'data-ticker="{html.escape(row["ticker"])}" '
            f'data-bullish="{_check_attr(row.get("bullish"))}" '
            f'data-sma="{_check_attr(row.get("above_sma"))}" '
            f'data-volume="{_check_attr(row.get("volume_ok"))}" '
            f'data-macd="{_check_attr(row.get("macd_cross"))}"'
        )
        body_rows.append(f"""    <tr {data_attrs}>
      <td><input type="checkbox" class="review" data-ticker="{html.escape(row['ticker'])}"></td>
      <td><a href="{html.escape(url)}" target="_blank" rel="noopener">{label}</a></td>
      {_check_cell(row.get("bullish"))}
      {_check_cell(row.get("above_sma"))}
      {_check_cell(row.get("volume_ok"))}
      {_check_cell(row.get("macd_cross"))}
      <td class="sources">{sources}</td>
    </tr>""")

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>KLSE Market Movers — Chart Links</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  .meta {{ color: #666; font-size: 0.9rem; margin-bottom: 1rem; }}
  .filters {{ display: flex; gap: 1.2rem; flex-wrap: wrap; align-items: center; margin-bottom: 0.8rem;
    font-size: 0.9rem; background: #f5f5f5; padding: 0.6rem 0.8rem; border-radius: 6px; }}
  .filters label {{ display: flex; align-items: center; gap: 0.35rem; cursor: pointer; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid #e0e0e0; }}
  th {{ color: #444; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.02em; }}
  td.chk {{ text-align: center; font-weight: bold; }}
  td.chk.ok {{ color: #1a8a3d; }}
  td.chk.no {{ color: #c0392b; }}
  td.chk.unknown {{ color: #aaa; font-weight: normal; }}
  td.sources {{ color: #777; font-size: 0.85rem; white-space: nowrap; }}
  a {{ color: #0b5fff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  tr.checked td {{ color: #999; }}
  tr.checked a {{ color: #999; text-decoration: line-through; }}
  tr.hidden {{ display: none; }}
  input[type="checkbox"] {{ width: 1.1rem; height: 1.1rem; cursor: pointer; }}
  #counter {{ font-size: 0.9rem; color: #444; margin-bottom: 0.5rem; }}
</style>
</head>
<body>
  <h1>KLSE Market Movers — Chart Links</h1>
  <p class="meta">Generated {generated}. {len(rows)} stocks. Bullish / &gt;SMA10&amp;20 / Vol&gt;2M / MACD
    golden cross (MACD line currently above its signal line) are computed from each stock's daily
    bars — use the filters below to narrow the page (no rerun needed). "—" means the check couldn't
    be computed (insufficient history or a fetch error).</p>
  <div class="filters">
    <label><input type="checkbox" id="filterBullish"> Bullish only</label>
    <label><input type="checkbox" id="filterSma"> Close &gt; SMA10 &amp; SMA20 only</label>
    <label><input type="checkbox" id="filterVolume"> Volume &gt; 2,000,000 only</label>
    <label><input type="checkbox" id="filterMacd"> MACD golden cross (MACD &gt; signal) only</label>
  </div>
  <p id="counter"></p>
  <table id="movers">
    <thead><tr><th></th><th>Stock</th><th>Bullish</th><th>&gt;SMA10&amp;20</th><th>Vol&gt;2M</th><th>MACD Cross</th><th>From</th></tr></thead>
    <tbody>
{chr(10).join(body_rows)}
    </tbody>
  </table>
  <script>
    const rows = Array.from(document.querySelectorAll('#movers tbody tr'));
    const boxes = Array.from(document.querySelectorAll('input.review'));
    const counter = document.getElementById('counter');
    const filterBullish = document.getElementById('filterBullish');
    const filterSma = document.getElementById('filterSma');
    const filterVolume = document.getElementById('filterVolume');
    const filterMacd = document.getElementById('filterMacd');

    function keyFor(cb) {{ return 'klse_check_' + cb.dataset.ticker; }}

    function updateCounter() {{
      const visible = rows.filter(tr => !tr.classList.contains('hidden'));
      const checked = boxes.filter(cb => cb.checked).length;
      counter.textContent = checked + ' / ' + boxes.length + ' checked (' + visible.length + ' / ' + rows.length + ' shown)';
    }}

    function applyFilters() {{
      rows.forEach(tr => {{
        let show = true;
        if (filterBullish.checked && tr.dataset.bullish !== '1') show = false;
        if (filterSma.checked && tr.dataset.sma !== '1') show = false;
        if (filterVolume.checked && tr.dataset.volume !== '1') show = false;
        if (filterMacd.checked && tr.dataset.macd !== '1') show = false;
        tr.classList.toggle('hidden', !show);
      }});
      updateCounter();
    }}

    [filterBullish, filterSma, filterVolume, filterMacd].forEach(cb => cb.addEventListener('change', applyFilters));

    boxes.forEach(cb => {{
      if (localStorage.getItem(keyFor(cb)) === '1') {{
        cb.checked = true;
        cb.closest('tr').classList.add('checked');
      }}
      cb.addEventListener('change', () => {{
        localStorage.setItem(keyFor(cb), cb.checked ? '1' : '0');
        cb.closest('tr').classList.toggle('checked', cb.checked);
        updateCounter();
      }});
    }});
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
