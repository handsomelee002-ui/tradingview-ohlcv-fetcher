<div align="center">
  <h1>📈 KLSE Chart Links</h1>
  <p><strong>A one-file Bursa Malaysia stock screener that outputs a clickable HTML shortlist.</strong></p>

  <p>
    <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white">
    <img alt="Dependencies" src="https://img.shields.io/badge/deps-pandas%20%7C%20requests-orange">
    <img alt="Full screen" src="https://img.shields.io/badge/full%20screen-~7s-brightgreen">
    <img alt="Demo" src="https://img.shields.io/badge/demo-Vercel-black?logo=vercel&logoColor=white">
    <img alt="License" src="https://img.shields.io/badge/license-MIT-red">
  </p>
</div>

---

## 📖 Overview

**KLSE Chart Links** scans every Bursa Malaysia primary listing, keeps only the
stocks that pass **seven bullish / momentum conditions**, and writes a
self-contained HTML page of clickable chart links — one dated file per run.

One HTTP request does the entire screen. A full run takes about **7 seconds**.
No TradingView login, no API key, no headless browser — just `pandas` and
`requests`.

**[▶ Live demo](https://tradingview-ohlcv-fetcher.vercel.app/)** — a static snapshot from one
run on 2026-08-18. It does not update; run the script for fresh data.

> ⚠️ **Not investment advice.** This is a personal, educational tool. It only
> reports that a stock met a set of arithmetic conditions on a given day. Do
> your own research. The upstream data endpoints are public but **undocumented
> and unofficial** and can change or break without notice.

---

## ✨ Features

### 🔎 The screen

| Area | What it does |
| --- | --- |
| **Whole-market scan** | One request to TradingView's public scanner returns every Bursa primary listing above the volume floor, plus every indicator column the checks need. No per-ticker fetch loop. |
| **Seven-condition filter** | Bullish candle, full SMA stack, volume, MACD golden cross, up on the day, minimum price, minimum listing age. Only stocks passing **all seven** are written out. See [The seven conditions](#-the-seven-conditions). |
| **Fast** | ~7 seconds end to end for a typical run. KLSEScreener code lookups run only on the handful of stocks that already qualified. |
| **Chart-exact backup** | `--source klsescreener` recomputes every check from KLSEScreener's own daily bars, so the numbers match the chart a link opens exactly. Slower (minutes), for double-checking borderline results. |
| **Graceful ticker resolution** | A ticker with no exact KLSEScreener match is not dropped — its link falls back to TradingView and it is logged as a warning. |

### 🖥️ The HTML page

| Feature | What it does |
| --- | --- |
| **Self-contained** | No external assets. All interactive state lives in the browser's `localStorage`. |
| **Run summary + legend** | Header shows when it ran, how many passed, and which data vendor computed the checks. A card lists all seven conditions. |
| **Review checkboxes** | Tick stocks off; state survives reload. Select all / Deselect all, plus an `X / N reviewed` counter. |
| **Per-stock notes** | Free-text notes per stock, with a dot indicator when a note exists. |
| **Decision calendar** | Mark any date **Buy / Watch / Ignore** per stock. History accumulates across days and survives regenerating the page. Rows last marked *Ignore* are dimmed. |
| **Search** | Filter by ticker or company name. |
| **Theme + chart-source toggles** | System / Light / Dark, and KLSEScreener ⇄ TradingView chart links. Both choices persist. |

---

## 🚀 Getting Started

**Requirements:** Python **3.10+**.

```bash
git clone https://github.com/handsomelee002-ui/tradingview-ohlcv-fetcher.git
cd tradingview-ohlcv-fetcher
pip install pandas requests
python klse_chart_links.py
```

The output is written to `data/klse_links/klse_chart_links_<YYYY-MM-DD>.html`
(one file per calendar day, overwritten if you rerun the same day). Open it in
any browser.

### Options

```bash
python klse_chart_links.py                        # default: 1 screen request, ~7s
python klse_chart_links.py --source klsescreener  # slower, chart-exact backup
python klse_chart_links.py --top 200              # cap the candidate list
python klse_chart_links.py -h                     # built-in help
```

| Flag | Default | Description |
| --- | --- | --- |
| `--top N` | `500` | Cap on candidates — the whole screen under `--source tradingview`, or per source list before merging under `--source klsescreener`. |
| `--source` | `tradingview` | `tradingview` = one scanner request plus one code lookup per qualifier. `klsescreener` = the slow, chart-exact backup. |
| `-h`, `--help` | — | Show help and exit. |

There is no `--out` flag — output always goes to `data/klse_links/`.

---

## 🧮 The seven conditions

A stock is written to the HTML only if it passes **all seven**. Labels match
the page's legend card.

| Check | Meaning |
| --- | --- |
| **Bullish Bar (Close > Open)** | Today's candle closed up on the day. |
| **Close > 四线上扬** | The full SMA stack: `close > SMA10 > SMA20 > SMA60 > SMA200`, in that order. Needs 200 daily bars, so younger listings cannot pass. |
| **Volume > 5,000,000** | Shares traded today. |
| **MACD golden cross** | MACD(12, 26, 9); the MACD line is currently above its signal line. A *state*, not "crossed on this exact bar". |
| **Close > previous close** | Up versus yesterday. |
| **Price >= RM 0.20** | Exactly 20 sen passes; only *below* is excluded. |
| **Listed at least 1 year** | Measured from `first_bar_time`, the oldest daily bar. |

A null from TradingView (young listings have no SMA200) counts as **not
passing**. Filtering happens in Python before the HTML is written, so the page
is already the shortlist.

---

## ⏰ Scheduled runs

`dailyRun.bat` is a Windows Task Scheduler launcher. It runs the screen with
default flags and appends every run to `data/task.log` with a `START` / `END`
banner around each — the `END` banner is written from a `finally` block, so a
run that fails partway still closes its own block.

The committed `dailyRun.bat` contains machine-specific absolute paths — edit
them for your machine, or copy it to `dailyRun.local.bat` (git-ignored). All
logging goes to **stderr**; there are no `print()` calls.

---

## 🛠️ Tech Stack

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="pandas" src="https://img.shields.io/badge/pandas-150458?logo=pandas&logoColor=white">
  <img alt="Requests" src="https://img.shields.io/badge/Requests-2CA5E0?logo=python&logoColor=white">
  <img alt="HTML5" src="https://img.shields.io/badge/HTML5-E34F26?logo=html5&logoColor=white">
  <img alt="CSS" src="https://img.shields.io/badge/CSS-1572B6?logo=css3&logoColor=white">
  <img alt="JavaScript" src="https://img.shields.io/badge/Vanilla%20JS-F7DF1E?logo=javascript&logoColor=black">
  <img alt="Vercel" src="https://img.shields.io/badge/Vercel-000000?logo=vercel&logoColor=white">
</p>

- **Screener:** a single Python 3 file — stdlib + `pandas` + `requests`. No
  framework, no ORM, no build step.
- **Output:** one static HTML file with inline CSS and vanilla JavaScript;
  all state in `localStorage`.
- **Hosting:** the demo is served as a static file on Vercel — no server, no
  build.

---

## 🗂️ Project layout

| Path | Purpose |
| --- | --- |
| `klse_chart_links.py` | The screener. Everything is in this one file. |
| `dailyRun.bat` | Windows scheduled-task launcher; appends to `data/task.log`. |
| `demo/index.html` | Static snapshot published as the live demo. |
| `vercel.json` | Serves `demo/` at the site root on Vercel. |
| `data/` | Generated output and logs. **Git-ignored** — created on first run. |
| `legacy/` | The repo's retired original TradingView OHLCV scripts. Unmaintained, kept for reference. |
| `README.dev.md` | Detailed engineering notes and design rationale. |

The repo is named `tradingview-ohlcv-fetcher` for historical reasons — it began
as a generic OHLCV fetcher, and those original scripts now live in `legacy/`.

---

## 🔌 Data sources

All public, unofficial JSON APIs. No authentication, no browser.

| Endpoint | Used for |
| --- | --- |
| `scanner.tradingview.com/malaysia/scan` | The default screen: candidate list + indicator columns in one request. |
| `klsescreener.com/v2/screener/search/<query>` | Resolving a ticker to its KLSEScreener numeric stock code. |
| `klsescreener.com/v2/trading_view/history` | `--source klsescreener` only: raw daily bars for recomputing every check. |

TradingView and KLSEScreener are different vendors, so their price series
differ slightly and compound into different SMA / MACD values. The default path
is fast; the `klsescreener` backup is chart-exact. Use the backup to verify a
borderline result.

---

## ▲ Vercel demo

The Python script does not run on Vercel — there is nothing to build. Only the
static `demo/index.html` snapshot is deployed.

1. Import the repository in Vercel. Framework preset **Other**, empty build
   command.
2. `vercel.json` rewrites `/` to `/demo/index.html`, so the site root serves
   the demo.
3. To refresh it, run the script and copy a newer output file over
   `demo/index.html`:

   ```bash
   python klse_chart_links.py
   cp data/klse_links/klse_chart_links_<YYYY-MM-DD>.html demo/index.html
   ```

Then replace the demo URL near the top of this file with your Vercel URL.

---

## 🧭 Scope

**In scope:** screening Bursa Malaysia on a fixed set of daily technical
conditions, and presenting the result as a static, offline-usable HTML page for
one person to review.

**Out of scope:** intraday data, backtesting, order execution, portfolio
tracking, alerting, multi-market support, and any server-side or database
component. The tool computes conditions — it does not decide anything.

---

## 📜 License

Licensed under the **MIT License** — see [`LICENSE`](LICENSE).

© 2026 handsomelee002-ui
