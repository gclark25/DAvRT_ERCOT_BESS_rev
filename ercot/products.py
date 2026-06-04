"""ERCOT data products used by the backtest.

Four inputs drive the DA-vs-RT settlement calc:

  1. DAM Settlement Point Prices (hourly DA LMP at each settlement point)
  2. RTM Settlement Point Prices (15-min RT LMP at each settlement point)
  3. 60-Day DAM Disclosure  -> Gen Resource energy AWARDS (DA MW per hour)
  4. 60-Day SCED Disclosure -> Gen Resource BASE POINTS (RT dispatch MW, 5-min)

Products 1-2 are row-based JSON APIs (clean paging). Products 3-4 are the
60-day disclosure ARCHIVE bundles (zipped CSVs), so they are fetched via the
archive listing + download path and parsed from the relevant CSV inside the zip.

The string constants below are the best-known EMIL paths; they are verified
against https://apiexplorer.ercot.com on the first live run and adjusted if a
product path differs. Each constant is isolated here so a fix is one line.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date

import pandas as pd
import requests

from .client import ErcotClient, BASE

# ---- Row-based price products -------------------------------------------------
DAM_SPP = "np4-190-cd/dam_stlmnt_pnt_prices"        # DAM Settlement Point Prices
RTM_SPP = "np6-905-cd/spp_node_zone_hub"            # RTM 15-min SPP (node/zone/hub)

# ---- 60-day disclosure archives (zipped CSV bundles) --------------------------
DAM_DISCLOSURE = "np3-966-er"   # 60-Day DAM Disclosure Reports (awards)
SCED_DISCLOSURE = "np3-965-er"  # 60-Day SCED Disclosure Reports (base points)

# CSV member-name fragments inside each disclosure zip:
DAM_GEN_CSV_FRAGMENT = "60d_DAM_Gen_Resource_Data"
SCED_GEN_CSV_FRAGMENT = "60d_SCED_Gen_Resource_Data"


def dam_prices(client: ErcotClient, d0: date, d1: date,
               settlement_points: list[str] | None = None) -> pd.DataFrame:
    """Hourly DA LMP. Returns columns incl. deliveryDate, hourEnding,
    settlementPoint, settlementPointPrice."""
    params = {
        "deliveryDateFrom": d0.isoformat(),
        "deliveryDateTo": d1.isoformat(),
    }
    df = client.get_report(DAM_SPP, params)
    if settlement_points and "settlementPoint" in df.columns:
        df = df[df["settlementPoint"].isin(settlement_points)]
    return df.reset_index(drop=True)


def rtm_prices(client: ErcotClient, d0: date, d1: date,
               settlement_points: list[str] | None = None) -> pd.DataFrame:
    """15-min RT SPP. Returns columns incl. deliveryDate, deliveryInterval,
    settlementPoint, settlementPointPrice."""
    params = {
        "deliveryDateFrom": d0.isoformat(),
        "deliveryDateTo": d1.isoformat(),
    }
    df = client.get_report(RTM_SPP, params)
    if settlement_points and "settlementPoint" in df.columns:
        df = df[df["settlementPoint"].isin(settlement_points)]
    return df.reset_index(drop=True)


# ---- Disclosure archive helpers ----------------------------------------------

def _list_archive(client: ErcotClient, emil: str, d0: date, d1: date) -> pd.DataFrame:
    """List archive documents for a disclosure product in a posted-date window."""
    url = f"{BASE}/archive/{emil}"
    params = {
        "postDatetimeFrom": f"{d0.isoformat()}T00:00:00",
        "postDatetimeTo": f"{d1.isoformat()}T23:59:59",
    }
    return client.get_report(url.replace(f"{BASE}/", ""), params)


def _download_zip(client: ErcotClient, emil: str, doc_id: str) -> bytes:
    url = f"{BASE}/archive/{emil}"
    headers = client._auth.headers(client._key)  # reuse client auth
    resp = requests.get(url, headers=headers, params={"download": doc_id}, timeout=120)
    resp.raise_for_status()
    return resp.content


def _read_member(zbytes: bytes, fragment: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        names = [n for n in zf.namelist() if fragment in n]
        if not names:
            return pd.DataFrame()
        frames = [pd.read_csv(zf.open(n)) for n in names]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def dam_awards(client: ErcotClient, d0: date, d1: date) -> pd.DataFrame:
    """DA Gen Resource energy awards from the 60-day DAM disclosure.

    Returns the raw 60d_DAM_Gen_Resource_Data rows; the calc layer maps the
    'Resource Name' / 'Awarded Quantity' (or 'Energy Settlement Point Price')
    columns. Posting lag is ~60 days, so d0/d1 are POSTED dates.
    """
    docs = _list_archive(client, DAM_DISCLOSURE, d0, d1)
    frames = []
    id_col = next((c for c in docs.columns if c.lower() in ("docid", "documentid", "id")), None)
    for doc_id in (docs[id_col] if id_col else []):
        frames.append(_read_member(_download_zip(client, DAM_DISCLOSURE, str(doc_id)),
                                    DAM_GEN_CSV_FRAGMENT))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def sced_dispatch(client: ErcotClient, d0: date, d1: date) -> pd.DataFrame:
    """RT Gen Resource base points (dispatch) from the 60-day SCED disclosure."""
    docs = _list_archive(client, SCED_DISCLOSURE, d0, d1)
    frames = []
    id_col = next((c for c in docs.columns if c.lower() in ("docid", "documentid", "id")), None)
    for doc_id in (docs[id_col] if id_col else []):
        frames.append(_read_member(_download_zip(client, SCED_DISCLOSURE, str(doc_id)),
                                    SCED_GEN_CSV_FRAGMENT))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
