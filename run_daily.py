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
import os
import sys
from datetime import date, timedelta

import pandas as pd

from ercot.auth import ErcotAuth
from ercot.client import ErcotClient
from ercot import config, products, normalize, calc, store, reports
from ercot.batteries import load_batteries, settlement_points

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backtest")

# Scope: post-RTC+B only. Default window runs from this date through the latest
# operating day whose 60-day disclosure has posted (today - DISCLOSURE_LAG).
START_DATE = date(2026, 1, 1)
DISCLOSURE_LAG = 64  # calendar days; safe margin past the 60-day posting


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--start", help="operating day start YYYY-MM-DD (overrides lag window)")
    p.add_argument("--end", help="operating day end YYYY-MM-DD")
    p.add_argument("--rebuild", action="store_true",
                   help="recompute the whole window, overwriting stored days")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    today = date.today()
    d0 = date.fromisoformat(args.start) if args.start else START_DATE
    d1 = date.fromisoformat(args.end) if args.end else today - timedelta(days=DISCLOSURE_LAG)
    d0 = max(d0, START_DATE)              # never go before the RTC+B scope start
    log.info("Target window %s .. %s", d0, d1)
    if d1 < d0:
        log.info("Window empty - nothing to process."); return 0

    batteries = load_batteries()
    sps = settlement_points(batteries)
    log.info("Loaded %d batteries (%d settlement points)", len(batteries), len(sps))

    auth = ErcotAuth(config.require("ERCOT_USERNAME"), config.require("ERCOT_PASSWORD"))
    client = ErcotClient(auth, config.require("ERCOT_SUBSCRIPTION_KEY"))

    # Incremental: process only operating days not already in history (skipped in
    # --rebuild mode, which recomputes the whole window and overwrites via upsert).
    if args.rebuild:
        log.info("REBUILD: recomputing full window %s .. %s, ignoring stored days.", d0, d1)
    else:
        done = set()
        hist = store.load_history()
        if not hist.empty:
            done = set(pd.to_datetime(hist["date"]).dt.date)
        all_days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
        missing = [d for d in all_days if d not in done]
        if not missing:
            log.info("Up to date through %s (%d operating days stored).", d1, len(done))
            return 0
        d0, d1 = missing[0], missing[-1]
        log.info("Processing %d missing operating days: %s .. %s", len(missing), d0, d1)

    # Process one calendar month at a time. This bounds each price request and
    # disclosure batch (a full-year span would time out / exhaust memory), and
    # upserts after each chunk so progress persists.
    total = 0
    for c0, c1 in _month_chunks(d0, d1):
        log.info("=== chunk %s .. %s ===", c0, c1)
        da_px_raw = products.dam_prices(client, c0, c1, sps)
        rt_px_raw = products.rtm_prices(client, c0, c1, sps)
        da_aw_raw = products.dam_awards(client, c0, c1)
        rt_disp_raw = products.sced_dispatch(client, c0, c1)
        rt_as_price_raw = products.rt_as_prices(client, c0, c1)

        resmap = normalize.esr_resource_map(da_aw_raw, batteries)
        log.info("  matched %d ESR resources (%d HEN)", len(resmap),
                 int((resmap.get("owner") == "HEN").sum()) if len(resmap) else 0)

        prices = normalize.build_prices(normalize.normalize_da_prices(da_px_raw),
                                        normalize.normalize_rt_prices(rt_px_raw))
        positions = normalize.build_positions(
            normalize.normalize_da_awards(da_aw_raw, resmap),
            normalize.normalize_rt_dispatch(rt_disp_raw, resmap))
        daily = calc.daily_by_battery(calc.settle(positions, prices), resmap)

        # AS revenue, split DART (DA award x (DA MCPC - RT MCPC)) and RT (RT award x RT MCPC)
        as_df = normalize.normalize_as_revenue(da_aw_raw, rt_disp_raw,
                                               rt_as_price_raw, resmap)
        daily = daily.merge(as_df, on=["resource_name", "date"], how="left")
        for col in ("dart_as", "rt_as"):
            daily[col] = daily[col].fillna(0.0)
        daily["total_rev"] = (daily["dart_energy"] + daily["rt_energy"]
                              + daily["dart_as"] + daily["rt_as"])
        daily["total_rev_per_mw"] = daily["total_rev"] / daily["nameplate_mw"]
        store.upsert(daily)
        total += len(daily)
        log.info("  chunk added %d battery-day rows", len(daily))

    history = store.load_history()
    xlsx = reports.write_excel(history)
    feed = reports.write_dashboard_feed(history)
    log.info("Done: +%d rows this run; history now %d rows. Wrote %s, %s",
             total, len(history), xlsx, feed)
    return 0


def _month_chunks(d0: date, d1: date):
    """Yield (start, end) date pairs, each within a single calendar month."""
    cur = d0
    while cur <= d1:
        if cur.month == 12:
            month_end = date(cur.year, 12, 31)
        else:
            month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
        yield cur, min(month_end, d1)
        cur = min(month_end, d1) + timedelta(days=1)


if __name__ == "__main__":
    sys.exit(main())
