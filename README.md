# ERCOT BESS — Day-Ahead vs Real-Time Backtest

Compares how Hunt Energy Network (HEN) batteries earn vs other ERCOT batteries,
using ERCOT-settled DA and RT revenue. Runs daily in GitHub Actions, stores a
growing history, and publishes Excel rollups + a live dashboard.

## How revenue is measured

Standard two-settlement decomposition, per battery per 15-min interval:

```
DA revenue  = DA award (MW) x DA settlement-point price
RT revenue  = (RT dispatch MW - DA award MW) x RT price x 0.25h   # the deviation
Total       = DA + RT
```

DA and RT are reported separately, plus a `$/MW` figure (revenue / nameplate)
so different-sized units compare fairly.

## The 60-day lag

Peer batteries' awards and dispatch only publish on ERCOT's ~60-day disclosure
schedule. So the peer comparison is on *settled* history: each daily run
processes the operating-day window that has just become fully available
(~58-70 days back) and upserts it into `data/history.parquet` idempotently.

## Repo layout

```
ercot/            # client, auth, products, normalize, calc, store, reports
config/batteries.csv   # fleet (regenerate with build_config.py from the xlsx)
run_daily.py      # orchestrator: fetch -> normalize -> settle -> store -> report
build_config.py   # rebuild battery config from 'ERCOT Batteries.xlsx'
.github/workflows/daily.yml   # daily cron + manual trigger
docs/index.html   # GitHub Pages dashboard (reads docs/dashboard.json)
data/history.parquet          # accumulated results (committed by the Action)
reports/da_vs_rt_backtest.xlsx
tests/            # settlement + pipeline unit tests (no network)
```

## One-time setup

1. **Push this folder to a GitHub repo.**
2. **Add repo Secrets** (Settings → Secrets and variables → Actions):
   - `ERCOT_USERNAME`
   - `ERCOT_PASSWORD`
   - `ERCOT_SUBSCRIPTION_KEY`
   (Locally these live in `.env`, which is git-ignored — never committed.)
3. **Enable GitHub Pages** (Settings → Pages → Source: *Deploy from a branch*,
   folder `/docs`). The dashboard will be at the Pages URL.
4. **First run:** Actions tab → *Daily DA vs RT backtest* → *Run workflow*.
   Use the optional `start`/`end` inputs to backtest a specific date range.

## Local run

```bash
pip install -r requirements.txt
python build_config.py            # rebuild fleet from the xlsx (optional)
python run_daily.py               # uses the default 60-day-back window
python run_daily.py --start 2025-01-01 --end 2025-01-31   # explicit range
python tests/test_calc.py && python tests/test_pipeline.py
```

## To confirm on the first live run

The price products are clean JSON APIs; the 60-day disclosure bundles are
zipped CSVs whose exact product paths and column headers are confirmed against
the live catalog on the first Actions run. The adapters in `ercot/normalize.py`
log unmatched columns, and `ercot/products.py` isolates each product path on a
single line for easy correction.
