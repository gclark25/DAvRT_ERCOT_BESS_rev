"""Validate settlement math on a hand-computed synthetic case."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ercot import calc  # noqa: E402


def build_case():
    # One resource, one hour (4 x 15-min intervals).
    # DA award = 10 MW, DA price = $50/MWh.
    # RT dispatch = 12 MW (over-delivered 2 MW) at RT price $80/MWh.
    positions = pd.DataFrame(
        {
            "resource_name": ["R1"] * 4,
            "settlement_point": ["RN1"] * 4,
            "ts_hour": ["2025-01-01 00:00"] * 4,
            "ts_interval": [1, 2, 3, 4],
            "da_award_mw": [10.0] * 4,
            "rt_dispatch_mw": [12.0] * 4,
        }
    )
    prices = pd.DataFrame(
        {
            "settlement_point": ["RN1"] * 4,
            "ts_hour": ["2025-01-01 00:00"] * 4,
            "ts_interval": [1, 2, 3, 4],
            "da_price": [50.0] * 4,
            "rt_price": [80.0] * 4,
        }
    )
    batteries = pd.DataFrame(
        {
            "resource_name": ["R1"],
            "name": ["Battery One"],
            "owner": ["HEN"],
            "nameplate_mw": [20.0],
        }
    )
    return positions, prices, batteries


def test_settlement():
    positions, prices, batteries = build_case()
    settled = calc.settle(positions, prices)

    # DA: 10 MW * $50 * 1 hr = $500 across the hour.
    assert abs(settled["da_rev"].sum() - 500.0) < 1e-6, settled["da_rev"].sum()
    # RT deviation: (12-10) MW * $80 * 1 hr = $160 across the hour.
    assert abs(settled["rt_rev"].sum() - 160.0) < 1e-6, settled["rt_rev"].sum()
    # Total = $660.
    assert abs(settled["total_rev"].sum() - 660.0) < 1e-6

    daily = calc.daily_by_battery(settled, batteries)
    row = daily.iloc[0]
    assert abs(row["total_rev"] - 660.0) < 1e-6
    # $/MW on 20 MW nameplate = $33.
    assert abs(row["total_rev_per_mw"] - 33.0) < 1e-6
    print("PASS: DA=$500, RT=$160, total=$660, $/MW=$33")


if __name__ == "__main__":
    test_settlement()
