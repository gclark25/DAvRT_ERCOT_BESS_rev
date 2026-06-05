"""Adapters mapping raw ERCOT frames -> the calc's normalized schema.

The calc works on a 15-minute grid keyed by (settlement_point, ts_hour,
ts_interval) where ts_interval is 1-4 within the operating hour:

  prices:    settlement_point, ts_hour, ts_interval, da_price, rt_price
  positions: resource_name, settlement_point, ts_hour, ts_interval,
             da_award_mw, rt_dispatch_mw

Raw cadences differ, so we resample onto the 15-min grid:
  - DA price  (hourly)  -> broadcast to the hour's 4 intervals
  - RT price  (15-min)  -> 1:1
  - DA award  (hourly)  -> broadcast to the hour's 4 intervals
  - RT base point (5-min) -> mean of the three 5-min points in each 15-min interval

Column names below use ERCOT's documented names with fallbacks. The first live
Actions run prints any unmatched columns so these lists can be corrected.
"""
from __future__ import annotations

import pandas as pd


def _col(df: pd.DataFrame, *candidates: str) -> str:
    lower = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    raise KeyError(
        f"None of {candidates} found in columns: {list(df.columns)}"
    )


def _hour_ending_to_grid(df: pd.DataFrame, date_col: str, he_col: str) -> pd.DataFrame:
    """Build ts_hour from delivery date + hourEnding.

    ERCOT reports hourEnding as "HH:00" (e.g. "01:00".."24:00") OR as a plain
    integer 1-24, so we take the portion before any ':' and coerce to a number.
    hourEnding N covers the hour starting N-1.
    """
    df = df.copy()
    d = pd.to_datetime(df[date_col], errors="coerce")
    he = pd.to_numeric(df[he_col].astype(str).str.strip().str.split(":").str[0],
                       errors="coerce")
    df["ts_hour"] = d + pd.to_timedelta(he - 1, unit="h")
    return df


def normalize_da_prices(raw: pd.DataFrame) -> pd.DataFrame:
    date_col = _col(raw, "deliveryDate", "DeliveryDate")
    he_col = _col(raw, "hourEnding", "HourEnding")
    px_col = _col(raw, "settlementPointPrice", "SettlementPointPrice", "spp")
    sp_col = _col(raw, "settlementPoint", "SettlementPoint", "settlementPointName")
    df = raw.rename(columns={sp_col: "settlement_point"})
    df = _hour_ending_to_grid(df, date_col, he_col)
    df["da_price"] = pd.to_numeric(df[px_col], errors="coerce")
    base = df[["settlement_point", "ts_hour", "da_price"]]
    # broadcast hourly DA price across 4 intervals
    out = base.loc[base.index.repeat(4)].copy()
    out["ts_interval"] = list(range(1, 5)) * len(base)
    return out.reset_index(drop=True)


def normalize_rt_prices(raw: pd.DataFrame) -> pd.DataFrame:
    date_col = _col(raw, "deliveryDate", "DeliveryDate")
    he_col = _col(raw, "deliveryHour", "hourEnding", "HourEnding")
    interval_col = _col(raw, "deliveryInterval", "DeliveryInterval", "intervalEnding")
    px_col = _col(raw, "settlementPointPrice", "SettlementPointPrice", "spp")
    sp_col = _col(raw, "settlementPoint", "SettlementPoint", "settlementPointName")
    df = raw.rename(columns={sp_col: "settlement_point"})
    df = _hour_ending_to_grid(df, date_col, he_col)
    df["ts_interval"] = pd.to_numeric(df[interval_col], errors="coerce").astype("Int64")
    df["rt_price"] = pd.to_numeric(df[px_col], errors="coerce")
    return df[["settlement_point", "ts_hour", "ts_interval", "rt_price"]].reset_index(drop=True)


_AWARD_COLS = ["resource_name", "settlement_point", "ts_hour", "ts_interval", "da_award_mw"]
_DISP_COLS = ["resource_name", "settlement_point", "ts_hour", "ts_interval", "rt_dispatch_mw"]


def normalize_da_awards(raw: pd.DataFrame, batteries: pd.DataFrame) -> pd.DataFrame:
    """60d_DAM_Gen_Resource_Data -> hourly DA award per resource, on 15-min grid."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_AWARD_COLS)
    date_col = _col(raw, "Delivery Date", "DeliveryDate", "deliveryDate")
    he_col = _col(raw, "Hour Ending", "HourEnding", "hourEnding")
    res_col = _col(raw, "Resource Name", "ResourceName", "resourceName")
    award_col = _col(raw, "Awarded Quantity", "Energy Awarded Quantity", "awardedQuantity")
    df = raw.rename(columns={res_col: "resource_name"})
    df = _hour_ending_to_grid(df, date_col, he_col)
    df["da_award_mw"] = pd.to_numeric(df[award_col], errors="coerce")
    df = df.merge(batteries[["resource_name", "settlement_point"]], on="resource_name", how="inner")
    base = df[["resource_name", "settlement_point", "ts_hour", "da_award_mw"]]
    out = base.loc[base.index.repeat(4)].copy()
    out["ts_interval"] = list(range(1, 5)) * len(base)
    return out.reset_index(drop=True)


def _numcol(df: pd.DataFrame, name: str) -> pd.Series:
    """Numeric column, or zeros if the column is absent."""
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce").fillna(0.0)
    return pd.Series(0.0, index=df.index)


def _sumcols(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    s = pd.Series(0.0, index=df.index)
    for c in cols:
        s = s + _numcol(df, c)
    return s


# Maps the five RT-priced AS services to their DA-award and RT-award columns.
# RRS clears as one product in RT but is split PFR/FFR/UFR in the disclosures.
_AS_SERVICES = {
    "REGUP": (["RegUp Awarded"], ["AS Awards REGUP"]),
    "REGDN": (["RegDown Awarded"], ["AS Awards REGDN"]),
    "RRS":   (["RRSPFR Awarded", "RRSFFR Awarded", "RRSUFR Awarded"],
              ["AS Awards RRSPFR", "AS Awards RRSFFR", "AS Awards RRSUFR"]),
    "ECRS":  (["ECRSSD Awarded"], ["AS Awards ECRS"]),
    "NSPIN": (["NonSpin Awarded"], ["AS Awards NSPIN"]),
}


def normalize_rt_as_revenue(da_raw: pd.DataFrame, sced_raw: pd.DataFrame,
                            price_raw: pd.DataFrame, resmap: pd.DataFrame) -> pd.DataFrame:
    """RT ancillary-service deviation revenue per resource per day.

    Two-settlement offset: (RT_AS_award - DA_AS_award) * RT_AS_MCPC, summed over
    services and hours. RT awards (per SCED interval) and RT MCPC (per SCED
    interval, system-wide) are averaged to the hour; DA awards are hourly.
    Returns [resource_name, date, rt_as_rev].
    """
    cols = ["resource_name", "date", "rt_as_rev"]
    if any(x is None or x.empty for x in (da_raw, sced_raw, price_raw)):
        return pd.DataFrame(columns=cols)

    # RT MCPC -> hourly mean per service
    p = price_raw.copy()
    pts = pd.to_datetime(p[_col(p, "SCEDTimestamp", "SCED Time Stamp")], errors="coerce")
    p["ts_hour"] = pts.dt.floor("h")
    p["ASType"] = p[_col(p, "ASType")].astype(str).str.upper()
    p["mcpc"] = pd.to_numeric(p[_col(p, "MCPC")], errors="coerce")
    price_hr = p.groupby(["ts_hour", "ASType"], as_index=False)["mcpc"].mean()

    # RT awards (SCED ESR) -> hourly mean per resource per service
    s = sced_raw.rename(columns={_col(sced_raw, "Resource Name"): "resource_name"}).copy()
    sts = pd.to_datetime(s[_col(s, "SCED Time Stamp", "SCEDTimestamp")], errors="coerce")
    s["ts_hour"] = sts.dt.floor("h")
    rt = pd.concat([
        s[["resource_name", "ts_hour"]].assign(ASType=svc, rt_mw=_sumcols(s, rtc))
        for svc, (_, rtc) in _AS_SERVICES.items()
    ], ignore_index=True)
    rt = rt.groupby(["resource_name", "ts_hour", "ASType"], as_index=False)["rt_mw"].mean()

    # DA awards (DAM ESR) -> hourly per resource per service
    d = da_raw.rename(columns={_col(da_raw, "Resource Name"): "resource_name"}).copy()
    d = _hour_ending_to_grid(d, _col(d, "Delivery Date"), _col(d, "Hour Ending"))
    da = pd.concat([
        d[["resource_name", "ts_hour"]].assign(ASType=svc, da_mw=_sumcols(d, dac))
        for svc, (dac, _) in _AS_SERVICES.items()
    ], ignore_index=True)
    da = da.groupby(["resource_name", "ts_hour", "ASType"], as_index=False)["da_mw"].sum()

    pos = rt.merge(da, on=["resource_name", "ts_hour", "ASType"], how="outer")
    pos["rt_mw"] = pos["rt_mw"].fillna(0.0)
    pos["da_mw"] = pos["da_mw"].fillna(0.0)
    pos = pos.merge(price_hr, on=["ts_hour", "ASType"], how="left")
    pos["mcpc"] = pos["mcpc"].fillna(0.0)
    pos["rt_as_rev"] = (pos["rt_mw"] - pos["da_mw"]) * pos["mcpc"]
    pos = pos.merge(resmap[["resource_name"]].drop_duplicates(),
                    on="resource_name", how="inner")
    pos["date"] = pd.to_datetime(pos["ts_hour"]).dt.date
    return pos.groupby(["resource_name", "date"], as_index=False)["rt_as_rev"].sum()


def normalize_da_as(raw: pd.DataFrame, resmap: pd.DataFrame) -> pd.DataFrame:
    """DA ancillary-service revenue per resource per day from 60d_DAM_ESR_Data.

    Revenue for each hour = sum over services of (awarded MW * MCPC $/MW):
      RegUp, RegDown, RRS (PFR+FFR+UFR share one MCPC), ECRS, NonSpin.
    Returns [resource_name, date, as_rev].
    """
    cols = ["resource_name", "date", "as_rev"]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    res_col = _col(raw, "Resource Name", "ResourceName")
    date_col = _col(raw, "Delivery Date", "DeliveryDate")
    df = raw.rename(columns={res_col: "resource_name"}).copy()
    rev = (_numcol(df, "RegUp Awarded") * _numcol(df, "RegUp MCPC")
           + _numcol(df, "RegDown Awarded") * _numcol(df, "RegDown MCPC")
           + (_numcol(df, "RRSPFR Awarded") + _numcol(df, "RRSFFR Awarded")
              + _numcol(df, "RRSUFR Awarded")) * _numcol(df, "RRS MCPC")
           + _numcol(df, "ECRSSD Awarded") * _numcol(df, "ECRS MCPC")
           + _numcol(df, "NonSpin Awarded") * _numcol(df, "NonSpin MCPC"))
    df["as_rev"] = rev
    df["date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date
    df = df.merge(resmap[["resource_name"]].drop_duplicates(),
                  on="resource_name", how="inner")
    return df.groupby(["resource_name", "date"], as_index=False)["as_rev"].sum()


def normalize_rt_dispatch(raw: pd.DataFrame, batteries: pd.DataFrame) -> pd.DataFrame:
    """60d_SCED_Gen_Resource_Data base points -> 15-min mean dispatch per resource."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_DISP_COLS)
    ts_col = _col(raw, "SCED Time Stamp", "SCEDTimestamp", "scedTimestamp")
    res_col = _col(raw, "Resource Name", "ResourceName", "resourceName")
    bp_col = _col(raw, "Base Point", "BasePoint", "basePoint")
    df = raw.rename(columns={res_col: "resource_name"})
    ts = pd.to_datetime(df[ts_col])
    df["ts_hour"] = ts.dt.floor("h")
    df["ts_interval"] = (ts.dt.minute // 15 + 1).astype(int)
    df["rt_dispatch_mw"] = pd.to_numeric(df[bp_col], errors="coerce")
    df = df.merge(batteries[["resource_name", "settlement_point"]], on="resource_name", how="inner")
    g = df.groupby(
        ["resource_name", "settlement_point", "ts_hour", "ts_interval"], as_index=False
    )["rt_dispatch_mw"].mean()
    return g


def esr_resource_map(da_raw: pd.DataFrame, batteries: pd.DataFrame) -> pd.DataFrame:
    """Map post-RTC+B ESR resources to our battery fleet via settlement point.

    The 60d_DAM_ESR_Data file carries both 'Resource Name' (e.g. CATARINA_ESR1)
    and 'Settlement Point Name'. We join that to the battery config on
    settlement point to learn each battery's current ESR resource name plus its
    meta (name/owner/nameplate). Matching on settlement point avoids depending on
    the old *_BESS resource names, which RTC+B retired.
    """
    meta_cols = ["settlement_point", "name", "owner", "nameplate_mw", "duration_class"]
    if da_raw is None or da_raw.empty:
        return pd.DataFrame(columns=["resource_name"] + meta_cols)
    rn = _col(da_raw, "Resource Name", "ResourceName")
    sp = _col(da_raw, "Settlement Point Name", "SettlementPointName", "Settlement Point")
    m = da_raw[[rn, sp]].drop_duplicates()
    m.columns = ["resource_name", "settlement_point"]
    have = [c for c in meta_cols if c in batteries.columns]
    meta = batteries[have].drop_duplicates("settlement_point")
    return m.merge(meta, on="settlement_point", how="inner").reset_index(drop=True)


def build_positions(da_awards: pd.DataFrame, rt_dispatch: pd.DataFrame) -> pd.DataFrame:
    """Outer-join DA award + RT dispatch onto the 15-min grid per resource."""
    keys = ["resource_name", "settlement_point", "ts_hour", "ts_interval"]
    pos = da_awards.merge(rt_dispatch, on=keys, how="outer")
    pos["da_award_mw"] = pos["da_award_mw"].fillna(0.0)
    pos["rt_dispatch_mw"] = pos["rt_dispatch_mw"].fillna(0.0)
    return pos


def build_prices(da_prices: pd.DataFrame, rt_prices: pd.DataFrame) -> pd.DataFrame:
    keys = ["settlement_point", "ts_hour", "ts_interval"]
    px = da_prices.merge(rt_prices, on=keys, how="outer")
    px = px.dropna(subset=["ts_hour"])          # drop any unparsable timestamps
    px = px.drop_duplicates(subset=keys)        # one price row per key
    return px.reset_index(drop=True)
