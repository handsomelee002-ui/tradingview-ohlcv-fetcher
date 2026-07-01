"""
Bursa official validator — manual, on-demand data check (any KLSE stock)

Independently confirms the TradingView data (from ohlcv_fetcher.py) is correct by
grabbing the SAME series straight from Bursa Malaysia's own website and
comparing. FULLY AUTOMATIC — a headless browser loads the Bursa page, lets the
site mint its own signed request, and we capture the JSON response.
No manual tokens, ever.

It reads the SAME ticker list as the fetcher (tickers.txt) and validates every
KLSE stock, or a single stock via --ticker. Non-Malaysian tickers (e.g. US) are
skipped — Bursa only covers Bursa Malaysia.

For individual / non-commercial use. Not part of the automated pipeline.

Install:
    pip install playwright pandas
    python -m playwright install chromium

Run:
    python ohlcv_fetcher.py                          # fetch TradingView data first
    python bursa_validate.py                      # validate every stock in tickers.txt
    python bursa_validate.py --ticker MYX:1155    # validate one stock
    python bursa_validate.py --file sample_tickers.csv --headful
"""

import sys
import time
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# reuse the fetcher's ticker parsing + CSV naming so paths always match
from ohlcv_fetcher import parse_ticker, _data_path, load_tickers, TICKERS_FILE

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger().setLevel(logging.WARNING)   # override INFO set by imported modules
log = logging.getLogger(__name__)

COMPARE_DIR = Path("data/compare")


def _bursa_code(ticker: str) -> str | None:
    """Return the Bursa stock code if this ticker is a Bursa (MYX) stock, else None."""
    exchange, symbol = parse_ticker(ticker)
    return symbol if exchange == "MYX" else None


def _page_url(stock_code: str) -> str:
    return ("https://www.bursamalaysia.com/trade/trading_resources/"
            f"listing_directory/company-profile?stock_code={stock_code}")


# ---------------------------------------------------------------------------
# Capture official OHLCV from Bursa via headless browser
# ---------------------------------------------------------------------------

def _capture_one(page, stock_code: str, attempts: int = 3) -> dict[str, dict]:
    """Load one stock's Bursa page and capture the historical stock_price_data XHR."""
    captured, seen, hist_urls = [], [], []

    def on_request(req):
        if "stock_price_data" in req.url and "historical" in req.url:
            hist_urls.append(req.url)
            log.info("%s: found API -> %s", stock_code, req.url)

    def on_response(resp):
        if "stock_price_data" in resp.url:
            seen.append(resp.url)
            try:
                body = resp.json()
            except Exception:
                return
            recs = _extract_records(body)
            if recs:
                captured.append(recs)

    page.on("request", on_request)
    page.on("response", on_response)
    log.info("Opening Bursa page for %s ...", stock_code)

    for attempt in range(1, attempts + 1):
        try:
            # domcontentloaded is fast; Bursa never reaches networkidle
            page.goto(_page_url(stock_code), wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log.debug("%s: navigation note: %s", stock_code, e)
        # actively wait for the historical XHR to fire (has the most records)
        try:
            page.wait_for_response(
                lambda r: "stock_price_data" in r.url and "historical" in r.url,
                timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(3000)     # let the response body settle
        if captured:
            break
        if attempt < attempts:
            log.info("%s: nothing yet, reloading (attempt %d)...", stock_code, attempt + 1)
            page.wait_for_timeout(2000)

    page.remove_listener("request", on_request)
    page.remove_listener("response", on_response)

    # Fallback: response body not caught, but the signed request fired -> replay it
    if not captured and hist_urls:
        log.info("%s: replaying captured signed URL ...", stock_code)
        recs = _replay(page, hist_urls[-1])
        if recs:
            captured.append(recs)

    if not captured:
        log.error("%s: no stock_price_data captured. URLs fired:", stock_code)
        for u in seen or ["(none)"]:
            log.error("  %s", u)
        return {}
    best = max(captured, key=len)
    log.info("%s: captured %d official records", stock_code, len(best))
    return _records_to_map(best)


def _replay(page, url: str) -> list:
    """Re-fetch a captured signed URL via the browser's own session (shares cookies)."""
    try:
        resp = page.request.get(url, timeout=30000)
        if resp.ok:
            return _extract_records(resp.json())
        log.warning("replay HTTP %s", resp.status)
    except Exception as e:
        log.warning("replay failed: %s", e)
    return []


def _extract_records(payload) -> list:
    if not isinstance(payload, dict):
        return []
    node = payload.get("historical_data", payload)
    data = node.get("data", node) if isinstance(node, dict) else node
    return data if isinstance(data, list) else []


def _parse_date(value) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError):
        return str(value)[:10]


def _pick(row, *keys):
    for k in keys:
        if isinstance(row, dict) and row.get(k) is not None:
            return row[k]
    return None


def _records_to_map(records: list) -> dict[str, dict]:
    out = {}
    for r in records:
        out[_parse_date(_pick(r, "date", "timestamp"))] = {"C": _pick(r, "close", "Close")}
    return out


# ---------------------------------------------------------------------------
# Compare against the stock's TradingView CSV
# ---------------------------------------------------------------------------

def _blank(ticker: str, verdict: str) -> dict:
    return {"ticker": ticker, "dates": 0, "match": 0, "diff": 0, "agree": 0.0, "verdict": verdict}


def _compare(ticker: str, bursa: dict[str, dict], threshold: float) -> dict:
    tv_csv = _data_path(ticker)
    if not tv_csv.exists():
        log.warning("%s: TradingView CSV missing (%s) — run ohlcv_fetcher.py first", ticker, tv_csv)
        return _blank(ticker, "no_csv")

    tv = pd.read_csv(tv_csv, dtype={"date": str})
    tv_map = {r["date"]: r for _, r in tv.iterrows()}
    common = sorted(set(bursa) & set(tv_map))
    if not common:
        return _blank(ticker, "no_overlap")

    rows, diffs, big = [], 0, 0
    for d in common:
        try:
            bc, tc = float(bursa[d]["C"]), float(tv_map[d]["C"])
        except (TypeError, ValueError):
            continue
        pct = abs(bc - tc) / bc if bc else 0.0
        status = "MATCH" if pct <= threshold else "DIFF"
        if status == "DIFF":
            diffs += 1
            if pct > 0.10:          # large gap => likely corporate-action adjustment
                big += 1
        rows.append({"date": d, "close_bursa": round(bc, 4),
                     "close_tradingview": round(tc, 4),
                     "pct_diff": round(pct, 6), "status": status})

    rep = pd.DataFrame(rows)
    COMPARE_DIR.mkdir(parents=True, exist_ok=True)
    _, code = parse_ticker(ticker)
    rep.to_csv(COMPARE_DIR / f"{code}_bursa_vs_tv.csv", index=False)

    n = len(rep)
    match = n - diffs
    if diffs == 0:
        verdict = "perfect"
    elif big > 0:
        verdict = "corp-action"     # adjusted (TV) vs raw (Bursa) around a split/bonus
    else:
        verdict = "ok (noise)"
    return {"ticker": ticker, "dates": n, "match": match, "diff": diffs,
            "agree": match / n * 100 if n else 0.0, "verdict": verdict}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(tickers: list[str], threshold: float, headful: bool) -> None:
    myx, skipped = [], []
    for t in tickers:
        (myx if _bursa_code(t) else skipped).append(t)
    if skipped:
        print(f"Skipped (not Bursa/MYX): {', '.join(skipped)}")
    if not myx:
        log.error("No Bursa (MYX) tickers to validate.")
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError("Playwright missing. Run:\n"
                          "  pip install playwright\n  python -m playwright install chromium")

    print(f"\nValidating {len(myx)} Bursa stock(s) vs official Bursa website ...")

    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        for i, t in enumerate(myx):
            print(f"  [{i + 1}/{len(myx)}] {t} ...", end="", flush=True)
            # fresh context per stock: the site only fires the API on a first visit
            ctx = browser.new_context(user_agent=ua, locale="en-US")
            page = ctx.new_page()
            data = _capture_one(page, _bursa_code(t))
            ctx.close()
            if data:
                r = _compare(t, data, threshold)
            else:
                r = _blank(t, "capture_failed")
            results.append(r)
            print(f" {r['verdict']} ({r['agree']:.2f}%)" if r["dates"] else f" {r['verdict']}")
            if i < len(myx) - 1:
                time.sleep(2)                # pace requests
        browser.close()

    _print_summary(results)


def _print_summary(results: list[dict]) -> None:
    summary = pd.DataFrame(results)
    COMPARE_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(COMPARE_DIR / "validation_summary.csv", index=False)

    print("\n" + "=" * 62)
    print("VALIDATION SUMMARY - TradingView vs official Bursa")
    print("=" * 62)
    print(f"{'ticker':14s}{'dates':>7}{'match':>7}{'diff':>6}{'agree':>9}  verdict")
    print("-" * 62)
    for r in results:
        print(f"{r['ticker']:14s}{r['dates']:>7}{r['match']:>7}{r['diff']:>6}"
              f"{r['agree']:>8.2f}%  {r['verdict']}")
    print("-" * 62)
    ok = sum(1 for r in results if r["verdict"] in ("perfect", "ok (noise)"))
    ca = sum(1 for r in results if r["verdict"] == "corp-action")
    bad = [r["ticker"] for r in results if r["verdict"] in ("capture_failed", "no_csv", "no_overlap")]
    print(f"accurate: {ok}/{len(results)}   corp-action (adjusted vs raw): {ca}"
          + (f"   needs attention: {', '.join(bad)}" if bad else ""))
    print(f"reports -> {COMPARE_DIR}\\<code>_bursa_vs_tv.csv  +  validation_summary.csv")
    print("=" * 62)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Validate TradingView data vs official Bursa")
    p.add_argument("--ticker", default=None, help="Validate one stock, e.g. MYX:1155")
    p.add_argument("--file", type=Path, default=TICKERS_FILE,
                   help=f"Ticker list to validate (default: {TICKERS_FILE})")
    p.add_argument("--threshold", type=float, default=0.005,
                   help="Flag close diff above this fraction (default 0.005 = 0.5%%)")
    p.add_argument("--headful", action="store_true", help="Show the browser window")
    args = p.parse_args()

    tickers = [args.ticker] if args.ticker else load_tickers(args.file)
    run(tickers, threshold=args.threshold, headful=args.headful)
