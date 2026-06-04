"""End-to-end smoke test on synthetic ERCOT-shaped raw frames (no network)."""
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ercot import normalize, calc, store, reports  # noqa


def synth():
    batteries = pd.DataFrame({
        "resource_name": ["HEN_BES1", "PEER_BES1"],
        "name": ["HEN One", "Peer One"],
        "owner": ["HEN", "PEER"],
        "settlement_point": ["HEN_RN", "PEER_RN"],
        "nameplate_mw": [100.0, 200.0],
    })
    dd = "2025-03-01"
    hours = list(range(1, 25))
    # DA prices (hourly) for both nodes
    da_px = pd.DataFrame([
        {"deliveryDate": dd, "hourEnding": h, "settlementPoint": sp, "settlementPointPrice": 40+h}
        for sp in ["HEN_RN", "PEER_RN"] for h in hours])
    # RT prices (15-min)
    rt_px = pd.DataFrame([
        {"deliveryDate": dd, "deliveryHour": h, "deliveryInterval": iv,
         "settlementPoint": sp, "settlementPointPrice": 60+h}
        for sp in ["HEN_RN", "PEER_RN"] for h in hours for iv in range(1, 5)])
    # DA awards (hourly, disclosure CSV column names)
    da_aw = pd.DataFrame([
        {"Delivery Date": dd, "Hour Ending": h, "Resource Name": rn, "Awarded Quantity": 50}
        for rn in ["HEN_BES1", "PEER_BES1"] for h in hours])
    # RT dispatch base points (5-min)
    rows = []
    for rn in ["HEN_BES1", "PEER_BES1"]:
        for h in hours:
            for mnt in range(0, 60, 5):
                rows.append({"SCED Time Stamp": f"{dd} {h-1:02d}:{mnt:02d}:00",
                             "Resource Name": rn, "Base Point": 55})
    rt_disp = pd.DataFrame(rows)
    return batteries, da_px, rt_px, da_aw, rt_disp


def main():
    batteries, da_px, rt_px, da_aw, rt_disp = synth()
    prices = normalize.build_prices(normalize.normalize_da_prices(da_px),
                                    normalize.normalize_rt_prices(rt_px))
    positions = normalize.build_positions(normalize.normalize_da_awards(da_aw, batteries),
                                          normalize.normalize_rt_dispatch(rt_disp, batteries))
    settled = calc.settle(positions, prices)
    daily = calc.daily_by_battery(settled, batteries)
    assert len(daily) == 2, daily
    # Sanity: DA rev = 50 MW * avg DA price * 24h ; positive numbers
    assert (daily["da_rev"] > 0).all()
    hist = store.upsert(daily)
    xlsx = reports.write_excel(hist)
    feed = reports.write_dashboard_feed(hist)
    assert Path(xlsx).exists() and Path(feed).exists()
    print("PASS pipeline:")
    print(daily[["name", "owner", "da_rev", "rt_rev", "total_rev", "total_rev_per_mw"]]
          .to_string(index=False))
    print("xlsx:", xlsx)
    print("feed:", feed)


if __name__ == "__main__":
    main()
