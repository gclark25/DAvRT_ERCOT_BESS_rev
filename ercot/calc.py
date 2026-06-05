"""DA-vs-RT settlement revenue calc.

Settlement model (standard ERCOT two-settlement decomposition):

  DA revenue (hourly)  = DA_award_MW * DA_SPP
  RT revenue (per 15m) = (RT_dispatch_MW - DA_award_MW) * RT_SPP * (interval_hours)
  Total                = DA revenue + RT revenue   (RT is the imbalance/deviation)

The RT leg settles only the deviation from the day-ahead position, which is why
a battery that simply delivers its DA award nets ~0 in RT. Both legs are kept
separate so reports can show DA vs RT contribution per battery.

This module works on a NORMALIZED long-format schema so the math is independent
of ERCOT's raw column names:

  prices : [settlement_point, ts_hour, ts_interval, da_price, rt_price]
  positions: [resource_name, settlement_point, ts_hour, ts_interval,
              da_award_mw, rt_dispatch_mw]

`normalize_*` adapters map raw API/disclosure columns into this schema; they are
confirmed against live data on first run. `settle()` is fully unit-tested.
"""
from __future__ import annotations

import pandas as pd

RT_INTERVAL_HOURS = 0.25  # 15-minute RT settlement intervals


def settle(positions: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Compute DA, RT, and total revenue per resource per 15-min interval.

    positions: resource_name, settlement_point, ts_hour, ts_interval,
               da_award_mw, rt_dispatch_mw
    prices:    settlement_point, ts_hour, ts_interval, da_price, rt_price

    Returns one row per resource x interval with da_rev, rt_rev, total_rev.
    """
    df = positions.merge(
        prices,
        on=["settlement_point", "ts_hour", "ts_interval"],
        how="left",
    )
    # Prices only cover the requested operating window, so dropping rows with no
    # DA price restricts positions to in-window days. Missing RT price -> 0.
    df = df.dropna(subset=["da_price"]).copy()
    df["rt_price"] = df["rt_price"].fillna(0.0)

    # DART energy = DA award marked DA-vs-RT spread:  DA_award * (DA_price - RT_price)
    # RT energy   = real-time delivered energy:        RT_dispatch * RT_price
    # (sum equals the classic DA + RT-deviation total.)
    df["dart_energy"] = df["da_award_mw"] * (df["da_price"] - df["rt_price"]) * RT_INTERVAL_HOURS
    df["rt_energy"] = df["rt_dispatch_mw"] * df["rt_price"] * RT_INTERVAL_HOURS
    df["total_rev"] = df["dart_energy"] + df["rt_energy"]
    return df


def daily_by_battery(settled: pd.DataFrame, batteries: pd.DataFrame) -> pd.DataFrame:
    """Aggregate interval-level revenue to per-battery per-day, with $/MW."""
    settled = settled.copy()
    settled["date"] = pd.to_datetime(settled["ts_hour"]).dt.date
    agg = (
        settled.groupby(["resource_name", "date"], as_index=False)[
            ["dart_energy", "rt_energy", "total_rev"]
        ].sum()
    )
    meta_cols = [c for c in ["resource_name", "name", "owner", "nameplate_mw",
                             "duration_class"] if c in batteries.columns]
    agg = agg.merge(batteries[meta_cols], on="resource_name", how="left")
    agg["total_rev_per_mw"] = agg["total_rev"] / agg["nameplate_mw"]
    return agg


def rollup(daily: pd.DataFrame, period: str) -> pd.DataFrame:
    """Roll daily per-battery revenue up to 'month' or 'year' averages/totals."""
    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"])
    key = daily["date"].dt.to_period("M" if period == "month" else "Y").astype(str)
    daily["period"] = key
    if "duration_class" not in daily.columns:
        daily["duration_class"] = "1hr"
    for col in ("dart_energy", "rt_energy", "dart_as", "rt_as"):
        if col not in daily.columns:
            daily[col] = 0.0
    g = daily.groupby(["resource_name", "name", "owner", "duration_class", "period"],
                      as_index=False).agg(
        dart_energy=("dart_energy", "sum"),
        rt_energy=("rt_energy", "sum"),
        dart_as=("dart_as", "sum"),
        rt_as=("rt_as", "sum"),
        total_rev=("total_rev", "sum"),
        avg_daily_total=("total_rev", "mean"),
        nameplate_mw=("nameplate_mw", "first"),
    )
    g["total_rev_per_mw"] = g["total_rev"] / g["nameplate_mw"]
    return g
