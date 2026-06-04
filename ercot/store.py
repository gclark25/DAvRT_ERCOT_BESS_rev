"""History store: append-only parquet of per-battery per-day revenue.

Idempotent: re-running a date overwrites that date's rows rather than
duplicating, so the daily Action can safely backfill the 60-day disclosure
window every run without creating duplicates.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import DATA_DIR

HISTORY = DATA_DIR / "history.parquet"
KEYS = ["resource_name", "date"]


def load_history() -> pd.DataFrame:
    if HISTORY.exists():
        return pd.read_parquet(HISTORY)
    return pd.DataFrame()


def upsert(daily: pd.DataFrame) -> pd.DataFrame:
    """Merge new daily rows into history, replacing any matching (resource,date)."""
    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"]).dt.date
    existing = load_history()
    if not existing.empty:
        existing["date"] = pd.to_datetime(existing["date"]).dt.date
        mask = existing.set_index(KEYS).index.isin(daily.set_index(KEYS).index)
        existing = existing[~mask]
        combined = pd.concat([existing, daily], ignore_index=True)
    else:
        combined = daily
    combined = combined.sort_values(KEYS).reset_index(drop=True)
    HISTORY.parent.mkdir(exist_ok=True)
    combined.to_parquet(HISTORY, index=False)
    return combined
