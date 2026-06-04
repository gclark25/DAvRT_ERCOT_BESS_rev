"""Daily orchestrator: fetch -> normalize -> settle -> store -> report.

Run locally:   python run_daily.py
In GitHub Actions: invoked by .github/workflows/daily.yml on a cron, with
credentials supplied as repo Secrets (env vars).

The 60-day disclosure lag means peer awards/dispatch for an operating day only
post ~60 days later. Each run therefore (re)processes a window of operating days
that have just become fully available, and the store upserts idempotently.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from ercot.auth import ErcotAuth
from ercot.client import ErcotClient
from ercot import config, products, normalize, calc, store, reports
from ercot.batteries import load_batteries, settlement_points

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backtest")

# Operating-day window to (re)process each run. Default catches days whose
# 60-day disclosure has just posted, with slack on both sides.
DEFAULT_LAG_START = 70
DEFAULT_LAG_END = 58


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--start", help="operating day start YYYY-MM-DD (overrides lag window)")
    p.add_argument("--end", help="operating day end YYYY-MM-DD")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    today = date.today()
    if args.start and args.end:
        d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
    else:
        d0 = today - timedelta(days=DEFAULT_LAG_START)
        d1 = today - timedelta(days=DEFAULT_LAG_END)
    log.info("Processing operating days %s .. %s", d0, d1)

    batteries = load_batteries()
    sps = settlement_points(batteries)
    log.info("Loaded %d batteries (%d settlement points)", len(batteries), len(sps))

    auth = ErcotAuth(config.require("ERCOT_USERNAME"), config.require("ERCOT_PASSWORD"))
    client = ErcotClient(auth, config.require("ERCOT_SUBSCRIPTION_KEY"))

    # --- fetch ---
    log.info("Fetching DA prices ...")
    da_px_raw = products.dam_prices(client, d0, d1, sps)
    log.info("Fetching RT prices ...")
    rt_px_raw = products.rtm_prices(client, d0, d1, sps)
    log.info("Fetching DA awards (60-day disclosure) ...")
    da_aw_raw = products.dam_awards(client, d0, d1)
    log.info("Fetching RT dispatch (60-day disclosure) ...")
    rt_disp_raw = products.sced_dispatch(client, d0, d1)

    for name, frame in [("DA prices", da_px_raw), ("RT prices", rt_px_raw),
                        ("DA awards", da_aw_raw), ("RT dispatch", rt_disp_raw)]:
        log.info("  %s: %d rows, cols=%s", name, len(frame), list(frame.columns)[:12])

    # --- normalize ---
    da_px = normalize.normalize_da_prices(da_px_raw)
    rt_px = normalize.normalize_rt_prices(rt_px_raw)
    da_aw = normalize.normalize_da_awards(da_aw_raw, batteries)
    rt_disp = normalize.normalize_rt_dispatch(rt_disp_raw, batteries)

    prices = normalize.build_prices(da_px, rt_px)
    positions = normalize.build_positions(da_aw, rt_disp)

    # --- settle + aggregate ---
    settled = calc.settle(positions, prices)
    daily = calc.daily_by_battery(settled, batteries)
    log.info("Computed %d battery-day revenue rows", len(daily))

    # --- persist + report ---
    history = store.upsert(daily)
    xlsx = reports.write_excel(history)
    feed = reports.write_dashboard_feed(history)
    log.info("Wrote %s and %s (history now %d rows)", xlsx, feed, len(history))
    return 0


if __name__ == "__main__":
    sys.exit(main())
