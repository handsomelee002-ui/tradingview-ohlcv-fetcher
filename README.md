# tradingview-ohlcv-fetcher

The active script in this repo is **`klse_chart_links.py`** — a Bursa Malaysia
screener that filters the market down to the stocks passing seven
bullish/momentum conditions and writes a static, clickable HTML shortlist with
links to KLSEScreener or TradingView charts.

The repo name comes from its origin as a generic TradingView OHLCV fetcher.
Those original scripts still work but are no longer maintained and now live in
[`legacy/`](#legacy) — see the bottom of this file.

- **Data sources:** TradingView's public scanner API and KLSEScreener's public
  search + chart-data endpoints. All JSON, no headless browser, no tokens.
- **Runtime:** ~7 seconds for a full market screen.
- **Output:** one dated HTML file per run in `data/klse_links/`.

> Both endpoints are public but **undocumented and unofficial** — they can
> change without notice. For individual, non-commercial use.

---

## Install

Requires Python 3.10+.

```powershell
pip install pandas requests
```

That's it — no TradingView login, no API key, no browser automation.

---

## Usage

```powershell
python klse_chart_links.py                        # default: 1 screen request, ~7s
python klse_chart_links.py --source klsescreener  # backup: slower, chart-exact
python klse_chart_links.py --top 200              # cap the candidate list
python klse_chart_links.py -h                     # built-in help
```

| Flag | Default | Description |
|------|---------|-------------|
| `--top N` | `500` | Cap on candidates — the whole screen under `--source tradingview`, or per-list before merging under `--source klsescreener`. |
| `--source` | `tradingview` | `tradingview` (one screen request, plus one code lookup per qualifier) or `klsescreener` (slow, chart-exact backup). |
| `-h`, `--help` | — | Show help and exit. |

There is no `--out` flag — output always goes to `data/klse_links/`, one dated
file per run:

```
data/klse_links/klse_chart_links_<YYYY-MM-DD>.html   overwritten if rerun same day
```

---

## The seven conditions

For every candidate the script computes seven checks, listed here under the
exact label the page's legend card uses:

| Check | Meaning |
|-------|---------|
| **Bullish Bar (Close > Open)** | Today's candle closed up on the day. |
| **Close > 四线上扬** | The full four-line SMA stack: `close > SMA10 > SMA20 > SMA60 > SMA200`, in that order. Needs 200 bars, so anything younger cannot pass. |
| **Volume > 5,000,000** | Shares traded today. |
| **MACD golden cross** | Standard MACD(12,26,9), MACD line currently above its signal line — a *state*, **not** "crossed on this exact bar". |
| **Close > previous close** | Up versus yesterday. |
| **Price >= RM 0.20** | Twenty sen exactly passes; only *below* is excluded. |
| **Listed at least 1 year** | From `first_bar_time`, the epoch timestamp of the oldest daily bar. |

A column TradingView returns as null — young listings have no SMA200 — makes
that check `None`, which counts as not passing.

**Only stocks that pass all seven make it into the output file.** Filtering
happens in Python before the HTML is written, so the page you get is already
the shortlist, not a "tick a box to filter" browse-everything page.

---

## How the screen works

**One request does the whole screen.** TradingView's scanner will return
`SMA10/20/60/200`, `MACD.macd`, `MACD.signal`, `change` and `first_bar_time` as
columns, so a single call returns both the candidate list *and* every input the
seven checks need — no per-ticker fetch loop. The call filters on
`volume > 5,000,000` server-side, which on a normal day returns ~105 of Bursa's
~1,125 primary listings.

The server-side filter uses exactly `CHECK_MIN_VOLUME` with the same strict `>`
operator as the `volume_ok` check, and is derived from that same constant so the
two cannot drift apart. There is no buffer between them: the filter and the
check read the same TradingView `volume` field from the same response, so a
looser filter returns an identical result set and merely fetches rows it is
about to discard (verified — filtering at 4M and at 5M produced the same
qualifiers, with 18 rows wasted).

This replaced an earlier design that merged 9 market-movers lists (unusual
volume, active, most volatile, high beta, best performing, top gainers, top
volume, all-time high, at 52-week high). Those 9 lists are 9 *orderings of one
universe*, so once a hard volume floor exists, every stock that could survive it
is already inside the single screen — the extra 8 lists contributed no
additional qualifiers, only per-ticker HTTP work. That path is still available
as `--source klsescreener` (see below); the preset and column-filter list
definitions it depends on are still in the file.

**Runtime: ~7 seconds**, versus several minutes before. KLSEScreener code
lookups now run *only on the handful of stocks that already qualified* (8 on the
day this was written) rather than on every candidate.

### Data source, and the tradeoff it carries

The default (`--source tradingview`) computes the checks from TradingView's
scanner columns. The backup (`--source klsescreener`) walks the old 9-list path
and recomputes every check from **KLSEScreener's own daily bars**
(`klsescreener.com/v2/trading_view/history`).

The backup exists because it is the only path whose numbers are guaranteed to
match a KLSEScreener chart exactly. KLSEScreener and TradingView are different
data vendors, and small price-series differences compound into different
SMA/MACD values — so under the default, a stock's SMA200 may differ slightly
from what the KLSEScreener chart shows when you click through. If a borderline
result looks wrong against the chart, rerun with `--source klsescreener` to
check it. It costs two rate-limited HTTP calls per candidate instead of one call
in total.

### How it resolves tickers to KLSE codes

KLSEScreener's own live-search endpoint
(`klsescreener.com/v2/screener/search/<query>`), taking the entry whose name
exactly matches the ticker symbol (to skip warrants/call-warrants sharing the
same prefix, e.g. searching `MAYBANK` also returns `MAYBANKC2H`). This runs
**after** filtering, on qualifiers only. A ticker with no exact match is not
dropped — its KLSEScreener link falls back to TradingView, and the affected
tickers are logged as a warning.

**Runtime:** the default path is one scanner request plus ~0.3s per qualifier
for code lookup — around **7 seconds** end to end for a typical 8-stock result.
`--source klsescreener` merges 9 lists into several hundred unique tickers and
spends two rate-limited KLSEScreener calls on each (~0.3s rate limit, ~0.25s
measured latency), which runs to several minutes.

---

## The HTML page

- The header carries a two-line run summary:

  ```
  Generated 2026-08-08 15:32.
  8 passed out of 105 from TradingView.
  ```

  It names the vendor whose data computed the checks, which is the detail that
  matters if a result looks wrong against the chart. It deliberately omits the
  condition count (the legend card directly below already states it) and any
  HTTP request count (implementation detail, nothing to act on).
- A static legend card lists all 7 conditions (informational only — every row
  already passes them, so there's nothing to toggle).
- Each row: a review checkbox (state persisted in the browser's `localStorage`,
  survives a reload), a note button (pop-up per-stock free-text notes, also
  `localStorage`-backed, with a dot indicator when a note exists), the stock
  name/link, and a small provenance line beneath it:

  ```
  Genetec Technology Bhd.
  #2 by volume
  ```

  That rank is **market-wide**, not a position within the filtered set. The
  scanner sorts by volume descending and filters on that same field, so the
  response is a prefix of the full Bursa volume ranking — `#2 by volume` means
  the second-highest-volume stock on Bursa that day. Under
  `--source klsescreener` the line instead reads `From: top_volume,
  unusual_volume`, naming the market-movers lists that surfaced the stock.
- **A per-stock decision calendar.** Each row has a `Set decision` button
  opening a month calendar: pick a date, mark it **Buy / Watch / Ignore** (one
  decision per date), and the row button then shows the most recent decision and
  its date. A row whose latest decision is *Ignore* is dimmed. History is
  per-ticker under `localStorage['klse_decision_history_<TICKER>']`, so a stock's
  decisions accumulate across days and survive regenerating the page. Edits are
  a **draft until you press Save** — Save stays disabled until something
  actually changes, `Delete` removes the selected date, and `Clear all` wipes
  that stock's history (also only on Save). Closing with unsaved changes
  discards them.
- A search box filters by ticker/company name; Select all / Deselect all
  bulk-toggle the review checkboxes; a counter shows `X / N reviewed`.
- A theme toggle (System/Light/Dark) and a chart-source toggle switch every
  row's link between KLSEScreener (`klsescreener.com/v2/charting/chart/<code>`)
  and TradingView (`tradingview.com/chart/?symbol=<TICKER>`) — both choices
  persist across reloads.

---

## Scheduled runs and logging

`dailyRun.bat` runs the screen with default flags and appends every run to
`data/task.log` (`>> data\task.log 2>&1`), so each run delimits itself:

```
######### START #########
2026-08-08 15:41:54,540 INFO Scanner screen: 50 stocks above the volume floor
2026-08-08 15:42:14,825 INFO [DONE] Saved 4 stock(s) -> data\klse_links\klse_chart_links_2026-08-08.html
######### END ###########


######### START #########
2026-08-08 15:42:17,930 INFO Scanner screen: 50 stocks above the volume floor
2026-08-08 15:42:21,163 INFO [DONE] Saved 4 stock(s) -> data\klse_links\klse_chart_links_2026-08-08.html
######### END ###########
```

Each run ends with two blank lines, so consecutive appends stay visually
separated. The END banner is written from a `finally` block, so a run that fails
partway still closes its block instead of swallowing every later run into it.

Everything goes through `logging` on **stderr** — there are no `print()` calls.
Mixing the two put lines in the log out of chronological order, because stdout
is block-buffered when redirected to a file while stderr is not, so a `print()`
issued first could land after a later log line. Keep it that way — don't
reintroduce a bare `print()`.

---

## Files

| File | Purpose |
|------|---------|
| `klse_chart_links.py` | The screener: Bursa Malaysia 7-condition screen → chart links (HTML). |
| `dailyRun.bat` | Scheduled-task launcher; appends output to `data/task.log`. |
| `data/klse_links/` | Output: chart-links HTML, one dated file per day. |
| `data/task.log` | Append-only run log from `dailyRun.bat`. |
| `legacy/` | Retired scripts — see below. |

---

## Legacy

These are the repo's original TradingView OHLCV scripts. They are **no longer
maintained** and are not used by `klse_chart_links.py`, which is fully
standalone (stdlib + `pandas` + `requests` only). They are kept for reference.

| File | What it did |
|------|-------------|
| `legacy/ohlcv_fetcher.py` | Fetched EOD OHLCV for any TradingView-listed stock from a `tickers.txt` list into per-stock CSVs under `data/ohlcv/`. |
| `legacy/market_movers.py` | Pulled TradingView's top gainers/losers/volume lists per market and fetched OHLCV for them into `data/topx/`. |
| `legacy/bursa_validate.py` | Cross-checked fetched TradingView data against Bursa Malaysia's own website (Playwright), writing reports to `data/compare/`. |
| `legacy/tickers.txt.example` | Example ticker list for the above. |

They import each other (`market_movers` and `bursa_validate` both import
`ohlcv_fetcher`), so to run one, run it from inside `legacy/`. They also need
extra dependencies the active script does not: `tvdatafeed` for the fetcher and
market movers, `playwright` for the validator.
