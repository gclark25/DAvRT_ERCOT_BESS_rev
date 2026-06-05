"""Generate Excel rollups + a JSON feed for the dashboard."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .config import REPORTS_DIR, DATA_DIR, DOCS_DIR
from . import calc


def _rank(df: pd.DataFrame, value: str) -> pd.DataFrame:
    df = df.sort_values(value, ascending=False).reset_index(drop=True)
    df.insert(0, "rank", df.index + 1)
    return df


def write_excel(history: pd.DataFrame) -> Path:
    """Write a single workbook with daily, monthly, yearly sheets + peer ranks."""
    out = REPORTS_DIR / "da_vs_rt_backtest.xlsx"
    monthly = calc.rollup(history, "month")
    yearly = calc.rollup(history, "year")

    with pd.ExcelWriter(out, engine="openpyxl") as xl:
        history.sort_values(["date", "total_rev"], ascending=[True, False]).to_excel(
            xl, sheet_name="Daily", index=False
        )
        _rank(monthly, "total_rev_per_mw").to_excel(xl, sheet_name="Monthly", index=False)
        _rank(yearly, "total_rev_per_mw").to_excel(xl, sheet_name="Yearly", index=False)

        # HEN vs peer summary (latest year, avg $/MW by owner)
        if not yearly.empty:
            summary = (
                yearly.groupby("owner", as_index=False)
                .agg(avg_total_rev_per_mw=("total_rev_per_mw", "mean"),
                     dart_energy=("dart_energy", "sum"),
                     rt_energy=("rt_energy", "sum"),
                     dart_as=("dart_as", "sum"),
                     rt_as=("rt_as", "sum"))
            )
            summary.to_excel(xl, sheet_name="HEN_vs_Peers", index=False)
    return out


def _duration_groups(monthly: pd.DataFrame) -> list[dict]:
    """Average $/MW per month by group = '<owner> <duration_class>'
    (e.g. 'HEN 1hr', 'PEER 2hr'), the four series the dashboard plots."""
    if monthly.empty:
        return []
    m = monthly.copy()
    if "duration_class" not in m.columns:
        m["duration_class"] = "1hr"
    m["group"] = m["owner"].str.title() + " " + m["duration_class"]
    for c in ("dart_energy", "rt_energy", "dart_as", "rt_as"):
        if c not in m.columns:
            m[c] = 0.0
    g = m.groupby(["period", "group"], as_index=False).agg(
        avg_rev_per_mw=("total_rev_per_mw", "mean"),
        dart_energy=("dart_energy", "sum"),
        rt_energy=("rt_energy", "sum"),
        dart_as=("dart_as", "sum"),
        rt_as=("rt_as", "sum"),
        n_units=("resource_name", "nunique"),
    )
    return json.loads(g.to_json(orient="records"))


def _fleet_mw(history: pd.DataFrame) -> dict:
    """Total nameplate MW per group ('<Owner> <duration>'), counting each
    battery once, so the dashboard can express stream revenue as $/MW."""
    if history.empty or "duration_class" not in history.columns:
        return {}
    b = history.drop_duplicates("resource_name").copy()
    b["group"] = b["owner"].str.title() + " " + b["duration_class"]
    return b.groupby("group")["nameplate_mw"].sum().round(1).to_dict()


def write_dashboard_feed(history: pd.DataFrame) -> Path:
    """Compact JSON the static dashboard reads (committed to repo / Pages)."""
    monthly = calc.rollup(history, "month")
    latest = sorted(monthly["period"].unique())[-1] if not monthly.empty else None
    leaderboard = (monthly[monthly["period"] == latest]
                   if latest else monthly.head(0))
    payload = {
        "updated": latest,
        "fleet_mw": _fleet_mw(history),
        "groups_monthly": _duration_groups(monthly),
        "leaderboard": json.loads(
            leaderboard.sort_values("total_rev_per_mw", ascending=False)
            .to_json(orient="records")),
    }
    text = json.dumps(payload)
    out = DOCS_DIR / "dashboard.json"   # served by GitHub Pages
    out.write_text(text)
    (DATA_DIR / "dashboard.json").write_text(text)  # also keep with data
    return out
