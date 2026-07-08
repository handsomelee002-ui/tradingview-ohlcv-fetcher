# TradingView OHLCV Fetcher

Fetches end-of-day (EOD) OHLCV data for **any TradingView-listed stock** (Bursa
Malaysia / KLSE, US, and more) and saves one clean, machine-readable CSV per
stock. Fully automatic — reads a ticker list, loops every stock, no manual steps.

- **Data source:** TradingView, via the `tvDatafeed` library.
- **Why TradingView:** for KLSE (`MYX`), its feed is licensed directly from Bursa
  Malaysia, so it is **official-grade** data — reachable automatically with no
  tokens. Other exchanges (US etc.) work the same way.
- **Validated:** cross-checked against Bursa Malaysia's own website — **99.75–100%
  exact match** across 10 KLSE stocks. The only large gaps are stocks with a
  split/bonus, where TradingView is split-adjusted and Bursa is raw (expected —
  see *Adjusted vs raw prices* below). Run `bursa_validate.py` to check yourself.
- **Don't know which stocks to track?** `market_movers.py` pulls TradingView's
  own top gainers/losers/volume lists per market and feeds them straight into
  the same OHLCV fetch — see *Market movers* below.
- **Want a quick-scan HTML watchlist for Malaysia?** `klse_chart_links.py`
  pulls TradingView's Malaysia market-movers lists and turns them into a
  checkbox-able HTML page of KLSEScreener chart links — see *KLSE chart
  links* below.

> `tvDatafeed` is an unofficial library (the *data* is official; the *access
> method* is not). It can break if TradingView changes its backend. For
> individual, non-commercial use.

---

## What it produces

One CSV per stock:

`data/ohlcv/<ticker>.csv`
```
date,ticker,H,L,O,C,V
2024-01-02,MYX:1155,9.85,9.70,9.75,9.80,1234500
```

| Column | Meaning |
|--------|---------|
| `date`   | Trading date, `YYYY-MM-DD` |
| `ticker` | TradingView ticker, e.g. `MYX:1155` |
| `H` | High |
| `L` | Low |
| `O` | Open |
| `C` | Close (split-**adjusted**, not dividend-adjusted — see caveats) |
| `V` | Volume |

One file per stock, so models can load each independently.

---

## Install

Requires Python 3.10+.

```powershell
pip install pandas
pip install --upgrade git+https://github.com/rongardF/tvdatafeed.git
pip install requests    # only needed for market_movers.py (see below)
```

Optional TradingView login (more stable, fewer throttles) — set env vars:

```powershell
set TV_USERNAME=youruser
set TV_PASSWORD=yourpass
```

---

## The ticker list

Stocks are read from a plain text or CSV file, one ticker per line. `#` lines
are comments; anything after a comma is ignored (so a CSV with names works).

Tickers use **TradingView's own `EXCHANGE:SYMBOL` format** — one format, no
suffix guessing, so there's no ambiguity about which market a code belongs to.

`tickers.txt`:
```
# TradingView format EXCHANGE:SYMBOL
MYX:1155, Maybank        # Bursa Malaysia
MYX:1023, CIMB
NASDAQ:AAPL, Apple       # US
NYSE:IBM, IBM
```

| Example | Market |
|---------|--------|
| `MYX:1155` | Bursa Malaysia |
| `NASDAQ:AAPL` | US (NASDAQ) |
| `NYSE:IBM` | US (NYSE) |
| `INDEX:KLSE` | FBM KLCI index |
| `TWSE:IX0001` | TAIEX index |

**Indices work too**, not just stocks — confirmed by testing the tickers
above. Take the ticker straight from the TradingView symbol URL, e.g.
`tradingview.com/symbols/INDEX-KLSE/` → `INDEX:KLSE` (dash becomes colon).
Note volume (`V`) comes back as `0` for indices, since an index itself has no
traded volume.

Any TradingView-supported exchange works. Each stock saves to a
filesystem-safe filename (`MYX:1155` → `MYX_1155.csv`). The validator converts
`MYX:xxxx` to the Bursa stock code internally — you never type a `.KL`/`.MY`
suffix anywhere.

---

## Usage

```powershell
python ohlcv_fetcher.py
```

Default run: reads `tickers.txt`, pulls up to 5000 daily bars (~20 years) per
stock from TradingView, saves a per-stock CSV.

### Examples

```powershell
# Full fetch, all defaults
python ohlcv_fetcher.py

# A single stock
python ohlcv_fetcher.py --tickers MYX:1155

# Read a different list (txt or csv)
python ohlcv_fetcher.py --file my_other_list.csv

# Fewer bars (faster)
python ohlcv_fetcher.py --bars 1000

# Fast top-up: merge only recent days into existing CSVs
python ohlcv_fetcher.py --update

# Built-in help
python ohlcv_fetcher.py -h
```

### Command-line options

| Flag | Default | Description |
|------|---------|-------------|
| `--tickers T [T ...]` | — | Space-separated tickers (e.g. `MYX:1155 NASDAQ:AAPL`). Overrides `--file`. |
| `--file PATH` | `tickers.txt` | Ticker list file (`.txt` or `.csv`). |
| `--bars N` | `5000` | Daily bars per stock on a full fetch (max 5000 ≈ 20 years). |
| `--update` | off | Merge recent days into existing CSVs (de-duplicated). |
| `-h`, `--help` | — | Show help and exit. |

### Full fetch vs. update

| Command | What it does | When |
|---------|--------------|------|
| `python ohlcv_fetcher.py` | Full history, overwrites each CSV. | First run; clean refresh. |
| `python ohlcv_fetcher.py --update` | Pulls ~30 recent bars, merges, de-dups. | Daily / routine top-ups. |

Adding new stocks to `tickers.txt` then running `--update` works: existing
stocks get topped up, new stocks get a fresh CSV automatically.

---

## Validating the data (optional)

`bursa_validate.py` independently checks your data against **Bursa Malaysia's
own website**. It is fully automatic — a headless browser loads the Bursa page,
lets the site issue its own request, and captures the official response.
**No manual tokens.** This is an on-demand spot-check, not part of the pipeline.

It reads the **same ticker list** as the fetcher and validates every Bursa
stock (non-Malaysian tickers like US are skipped — Bursa is Malaysia only).

```powershell
pip install playwright
python -m playwright install chromium

python ohlcv_fetcher.py                          # fetch first
python bursa_validate.py                      # validate every Bursa stock in tickers.txt
python bursa_validate.py --ticker MYX:1155    # or just one
```

Options: `--ticker` (one stock), `--file` (ticker list, default `tickers.txt`),
`--threshold` (default 0.005 = 0.5%), `--headful` (watch the browser).

It prints a live progress line per stock and a summary table at the end, and
saves `data/compare/<code>_bursa_vs_tv.csv` (per stock) plus
`data/compare/validation_summary.csv`:

```
VALIDATION SUMMARY - TradingView vs official Bursa
==============================================================
ticker          dates  match  diff    agree  verdict
--------------------------------------------------------------
MYX:1155         1189   1187     2   99.83%  ok (noise)
MYX:5296         1189    978   211   82.25%  corp-action
--------------------------------------------------------------
accurate: 1/2   corp-action (adjusted vs raw): 1
```

Verdict per stock:

| Verdict | Meaning |
|---------|---------|
| `perfect` | every date matches |
| `ok (noise)` | only tiny (<1%) diffs, usually a recent-date revision lag |
| `corp-action` | large exact-ratio gaps around a split/bonus — adjusted vs raw, expected |
| `capture_failed` / `no_csv` | couldn't capture Bursa data / no TradingView CSV yet — rerun |

> Each stock takes ~5–40s (the browser waits for Bursa's chart to fire its API,
> retrying up to 3×). Failed captures are listed as "needs attention" — just
> rerun. If nothing is ever captured, the script logs the URLs the page fired so
> the target page can be adjusted.

### Adjusted vs raw prices

TradingView returns **split-adjusted** prices (confirmed from the `tvDatafeed`
library itself, which requests `"adjustment":"splits"` — **not** dividends);
Bursa's site returns **raw** traded prices. For a stock with no corporate
action they match to ~100%. For a stock that had a split/bonus, all prices
*before* that event differ by the adjustment factor (e.g. an exact 1.5× on a
3-for-2 bonus) — the validator labels this `corp-action`, not an error.
**Adjusted is what you want for backtesting / AI training** (raw has
artificial jumps at split dates that corrupt indicators); raw is only for
"what was the actual traded price". Note this is split-adjusted only —
dividends are **not** backed out of the price series.

---

## Notes & caveats

- **Adjusted prices:** the fetched data is split-adjusted (continuous series,
  best for backtesting) but **not** dividend-adjusted. See *Adjusted vs raw
  prices* above.
- **Rate limits & retries:** anonymous TradingView is throttled; a free login
  (env vars above) is steadier. There's a 1s pause between stocks and each fetch
  retries up to 3× on transient drops. Any stocks that still fail are printed in
  a `[failed]` list at the end — just rerun.
- **Survivorship bias:** only currently-listed stocks are covered; delisted
  companies are missing — keep this in mind when training models.
- **`tvDatafeed` is unofficial** and may break on TradingView changes.

---

## Market movers (top gainers / losers / volume, by market)

`market_movers.py` pulls a ranked "top N" list — gainers, losers, or most
active by volume — for a given market from TradingView's public screener, then
fetches OHLCV for those tickers the same way as `ohlcv_fetcher.py`. It never
touches your `tickers.txt`-driven `data/ohlcv/` files; results land in their
own sub-folder under `data/topx/`, overwritten fresh on each run.

Needs one extra dependency on top of the main [Install](#install) steps:

```powershell
pip install requests
```

`--market` is the exact slug from a TradingView market-movers URL:

```
tradingview.com/markets/stocks-malaysia/market-movers-gainers/  -> --market malaysia
tradingview.com/markets/stocks-germany/market-movers-volume/    -> --market germany
tradingview.com/markets/stocks-usa/market-movers-gainers/       -> --market usa
```

```powershell
python market_movers.py --market malaysia --list volume  --top 30
python market_movers.py --market malaysia --list losers  --top 100
python market_movers.py --market taiwan   --list gainers --top 50
python market_movers.py --market usa      --list volume  --top 30
```

| Flag | Description |
|------|-------------|
| `--market SLUG` | TradingView URL slug, e.g. `malaysia`, `usa`, `germany`, `taiwan` |
| `--list` | `gainers`, `losers`, or `volume` |
| `--top N` | How many tickers to take from the ranking |
| `--bars N` | Bars per stock (default 5000, same as `ohlcv_fetcher.py`) |

Output:

```
data/topx/<market>_<list>_top<n>/_ranking.csv     the ranked list (rank, ticker, name, close, volume, change)
data/topx/<market>_<list>_top<n>/<ticker>.csv     one OHLCV CSV per stock, same columns as data/ohlcv/
```

**Notes:**
- Most `--market` slugs pass straight to TradingView's scanner backend
  unchanged; a couple of known exceptions are translated internally (e.g.
  `usa` → backend region `america`). An unrecognized slug fails with a clear
  error rather than silently returning nothing.
- For US stocks, OTC/pink-sheet listings are excluded by default — without
  that filter, "gainers"/"losers" gets dominated by illiquid penny stocks with
  meaningless swings (this matches what TradingView's own page shows).
- Same unofficial-API caveat as `tvDatafeed`: this uses TradingView's public
  scanner endpoint, which could change without notice.

---

## KLSE chart links (Malaysia market movers → clickable HTML)

`klse_chart_links.py` pulls 5 of TradingView's Malaysia market-movers lists
(unusual volume, active, most volatile, high beta, best performing), merges
and dedupes the tickers, resolves each to its KLSEScreener numeric stock
code, and writes a static HTML file of clickable chart links
(`klsescreener.com/v2/charting/chart/<code>`). Each row has a checkbox you
can tick off after reviewing a chart — state is saved in the browser's
`localStorage`, so it survives a page reload (per-file, since it's a local
`file://`/`http://localhost` page).

For every resolved stock it also pulls four checks — **Bullish** (close >
open), **&gt;SMA10&amp;20** (close above *both* the 10- and 20-day SMA),
**Vol&gt;2M** (volume > 2,000,000), and **MACD Cross** (standard
MACD(12,26,9): MACD line currently above its signal line right now — a
state, **not** "crossed on this exact bar") — shown as ✓/✗/— columns
(`—` = couldn't be computed: not enough history for that specific check,
or a fetch error; bullish/volume only need the latest bar so they still
compute even when SMA/MACD can't).

All four are computed from **KLSEScreener's own daily price history**
(`klsescreener.com/v2/trading_view/history`) — the same TradingView
Charting Library data feed that powers the `/v2/charting/chart/<code>`
page the link opens — fetched per resolved ticker (~1.5 years of bars each
time) and computed locally with the standard SMA/MACD formulas. This is
**deliberately not** TradingView's own scanner data: KLSEScreener and
TradingView are different data vendors, and even small differences in
historical price series compound into different SMA/MACD values (MACD
especially, since it's an EMA over the whole history). Sourcing from
KLSEScreener's own feed means the checks match the exact chart you click
through to, at the cost of one HTTP call per ticker instead of one batched
call (see Runtime below). These checks always run; there's no CLI flag to
skip them. Filtering happens **in the page itself**: four checkboxes above
the table ("Bullish only", "Close > SMA10 & SMA20 only", "Volume >
2,000,000 only", "MACD golden cross (MACD > signal) only") hide/show rows
client-side (AND logic if you tick more than one) — no rerun needed to
change your mind.

```powershell
python klse_chart_links.py                      # top 100 per list
python klse_chart_links.py --top 50
```

| Flag | Default | Description |
|------|---------|-------------|
| `--top N` | `100` | Tickers to take from each of the 5 lists before merging. |

No `--out` flag — output always goes to `data/klse_links/` (same `data/`
convention as `ohlcv_fetcher.py`/`market_movers.py`), one dated file per run:

```
data/klse_links/klse_chart_links_<YYYY-MM-DD>.html   overwritten if rerun same day
```

**How it resolves tickers to KLSE codes:** KLSEScreener's own live-search
endpoint (`klsescreener.com/v2/screener/search/<query>`), taking the entry
whose name exactly matches the ticker symbol (to skip warrants/call-warrants
sharing the same prefix, e.g. searching `MAYBANK` also returns
`MAYBANKC2H`). Any ticker with no exact match is skipped and printed at the
end, not silently dropped.

**Runtime:** merging 5 lists of `--top 100` typically yields ~250–450 unique
tickers (real overlap between volatile/active/high-beta lists). Both KLSE
code resolution *and* the four checks are one HTTP call per ticker to
KLSEScreener (~0.3s rate limit each, ~0.25s measured latency per call) — two
per-ticker passes over the same host, roughly 6–8 min total for a large
list. Slower than a batched TradingView-sourced version would be, but this
is the tradeoff for the checks matching the actual chart the link opens.

Same unofficial-API caveat as the rest of this project: the TradingView
scanner endpoint, KLSEScreener's search endpoint, and KLSEScreener's chart
data feed are all public but
undocumented, and could change without notice.

---

## Files

| File | Purpose |
|------|---------|
| `ohlcv_fetcher.py` | The fetcher (TradingView → per-stock CSV). |
| `market_movers.py` | Top gainers/losers/volume lists per market → OHLCV. |
| `klse_chart_links.py` | Malaysia market movers → KLSEScreener chart links (HTML, with checkboxes). |
| `bursa_validate.py` | On-demand validator vs official Bursa (Playwright). |
| `test_klse_chart_links.py` | Unit tests for `klse_chart_links.py` (mocked network calls). |
| `tickers.txt` | Your ticker list. |
| `data/ohlcv/` | Output: per-stock OHLCV CSVs. |
| `data/topx/` | Output: market-movers top-N lists (ranking + OHLCV). |
| `data/klse_links/` | Output: KLSE chart-links HTML, one dated file per day. |
| `data/compare/` | Output: validation reports + `validation_summary.csv`. |
