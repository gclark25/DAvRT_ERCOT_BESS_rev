"""Battery fleet config: HEN assets + ERCOT peers, with node/MW mapping."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import CONFIG_DIR

REQUIRED_COLS = ["name", "owner", "resource_name", "settlement_point", "nameplate_mw"]


def load_batteries(path: Path | str | None = None) -> pd.DataFrame:
    """Load the battery config CSV. Comment lines (starting with #) are ignored.

    Looks for config/batteries.csv by default; falls back to the template.
    """
    if path is None:
        candidate = CONFIG_DIR / "batteries.csv"
        path = candidate if candidate.exists() else CONFIG_DIR / "batteries_template.csv"
    df = pd.read_csv(path, comment="#")
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"battery config missing columns: {missing}")
    df["owner"] = df["owner"].str.upper().str.strip()
    df["nameplate_mw"] = pd.to_numeric(df["nameplate_mw"], errors="coerce")
    return df.reset_index(drop=True)


def settlement_points(df: pd.DataFrame) -> list[str]:
    return sorted(df["settlement_point"].dropna().unique().tolist())
