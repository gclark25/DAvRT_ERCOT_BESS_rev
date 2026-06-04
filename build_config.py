"""Regenerate config/batteries.csv from the ERCOT battery master workbook.

Run whenever 'ERCOT Batteries.xlsx' is updated. Keeps only currently-active
records (valid_to in 2050) and flags Hunt Energy Network units as HEN.
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent
MASTER = ROOT / "ERCOT Batteries.xlsx"
OUT = ROOT / "config" / "batteries.csv"
HEN_OWNER = "Hunt Energy Network"


def main():
    df = pd.read_excel(MASTER)
    active = df[df["valid_to"].astype(str).str.startswith("2050")].copy()
    out = pd.DataFrame({
        "name": active["asset"],
        "owner": active["owner"].eq(HEN_OWNER).map({True: "HEN", False: "PEER"}),
        "resource_name": active["generator_id"],
        "settlement_point": active["settlement_point_name"],
        "nameplate_mw": active["rated_power_mw"],
        "load_zone": active["load_zone"],
        "company": active["owner"],
    })
    out = out.dropna(subset=["resource_name", "settlement_point"])
    out = out.drop_duplicates("resource_name")
    out = out.sort_values(["owner", "name"]).reset_index(drop=True)
    out.to_csv(OUT, index=False)
    n_hen = int((out["owner"] == "HEN").sum())
    n_peer = int((out["owner"] == "PEER").sum())
    print("Wrote", OUT, "-", len(out), "batteries:", n_hen, "HEN,", n_peer, "PEER")


if __name__ == "__main__":
    main()
