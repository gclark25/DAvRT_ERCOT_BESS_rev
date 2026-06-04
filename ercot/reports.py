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
                     total_da_rev=("da_rev", "sum"),
                     total_rt_rev=("rt_rev", "sum"))
            )
            summary.to_excel(xl, sheet_name="HEN_vs_Peers", index=False)
    return out


def write_dashboard_feed(history: pd.DataFrame) -> Path:
    """Compact JSON the static dashboard reads (committed to repo / Pages)."""
    monthly = calc.rollup(history, "month")
    payload = {
        "generated_keys": list(history.columns),
        "daily": json.loads(history.tail(2000).to_json(orient="records")),
        "monthly": json.loads(monthly.to_json(orient="records")),
    }
    text = json.dumps(payload)
    out = DOCS_DIR / "dashboard.json"   # served by GitHub Pages
    out.write_text(text)
    (DATA_DIR / "dashboard.json").write_text(text)  # also keep with data
    return out
