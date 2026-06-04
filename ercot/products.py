"""ERCOT data products used by the backtest.

Four inputs drive the DA-vs-RT settlement calc:

  1. DAM Settlement Point Prices (hourly DA LMP at each settlement point)
  2. RTM Settlement Point Prices (15-min RT LMP at each settlement point)
  3. 60-Day DAM Disclosure  -> Gen Resource energy AWARDS (DA MW per hour)
  4. 60-Day SCED Disclosure -> Gen Resource BASE POINTS (RT dispatch MW, 5-min)

Products 1-2 are row-based JSON APIs (clean paging) and are confirmed working.
Products 3-4 are the 60-Day disclosure ARCHIVE bundles (zipped CSVs): the API
returns an `archives` list of downloadable documents, which we then download and
read the relevant CSV from. Disclosures post ~60 days AFTER the operating day,
so we search the archive by POSTED date = operating day + ~60.
"""
from __future__ import annotations

import io
import logging
import time
import zipfile
from datetime import date, timedelta

import pandas as pd
import requests

from .client import ErcotClient, BASE

log = logging.getLogger("backtest.products")

# ---- Row-based price products -------------------------------------------------
DAM_SPP = "np4-190-cd/dam_stlmnt_pnt_prices"        # DAM Settlement Point Prices
RTM_SPP = "np6-905-cd/spp_node_zone_hub"            # RTM 15-min SPP (node/zone/hub)

# ---- 60-day disclosure archives (zipped CSV bundles) --------------------------
DAM_DISCLOSURE = "NP3-966-ER"   # 60-Day DAM Disclosure Reports (awards)
SCED_DISCLOSURE = "NP3-965-ER"  # 60-Day SCED Disclosure Reports (base points)

# CSV member-name fragments inside each disclosure zip:
DAM_GEN_CSV_FRAGMENT = "DAM_Gen_Resource_Data"
SCED_GEN_CSV_FRAGMENT = "SCED_Gen_Resource_Data"

# Disclosure posting lag (calendar days after the operating day), with buffer
# for weekends/holidays that push the posting date out.
LAG_MIN = 57
LAG_MAX = 65


def dam_prices(client: ErcotClient, d0: date, d1: date,
               settlement_points: list[str] | None = None) -> pd.DataFrame:
    """Hourly DA LMP. Columns: deliveryDate, hourEnding, settlementPoint,
    settlementPointPrice."""
    params = {"deliveryDateFrom": d0.isoformat(), "deliveryDateTo": d1.isoformat()}
    df = client.get_report(DAM_SPP, params)
    if settlement_points and "settlementPoint" in df.columns:
        df = df[df["settlementPoint"].isin(settlement_points)]
    return df.reset_index(drop=True)


def rtm_prices(client: ErcotClient, d0: date, d1: date,
               settlement_points: list[str] | None = None) -> pd.DataFrame:
    """15-min RT SPP. Columns: deliveryDate, deliveryHour, deliveryInterval,
    settlementPoint, settlementPointPrice."""
    params = {"deliveryDateFrom": d0.isoformat(), "deliveryDateTo": d1.isoformat()}
    df = client.get_report(RTM_SPP, params)
    if settlement_points and "settlementPoint" in df.columns:
        df = df[df["settlementPoint"].isin(settlement_points)]
    return df.reset_index(drop=True)


# ---- Disclosure archive helpers ----------------------------------------------

def _archive_docs(client: ErcotClient, emil: str, op_d0: date, op_d1: date) -> list[dict]:
    """List archive documents for a disclosure product, searching by the POSTED
    date window implied by the operating-day window + the 60-day lag."""
    posted_from = op_d0 + timedelta(days=LAG_MIN)
    posted_to = op_d1 + timedelta(days=LAG_MAX)
    url = f"{BASE}/archive/{emil}"
    params = {
        "postDatetimeFrom": f"{posted_from.isoformat()}T00:00:00",
        "postDatetimeTo": f"{posted_to.isoformat()}T23:59:59",
        "size": 1000,
    }
    body = client._get(url, params)
    docs = body.get("archives") or body.get("data") or []
    log.info("archive %s: %d docs (posted %s..%s); keys=%s",
             emil, len(docs), posted_from, posted_to,
             list(docs[0].keys()) if docs else "n/a")
    return docs


def _doc_id(doc: dict):
    for k in ("docId", "documentId", "id", "_docId"):
        if k in doc:
            return doc[k]
    return None


def _download_zip(client: ErcotClient, emil: str, doc_id) -> bytes:
    url = f"{BASE}/archive/{emil}"
    headers = client._auth.headers(client._key)
    time.sleep(2.2)  # stay under the rate limit on downloads too
    resp = requests.get(url, headers=headers, params={"download": doc_id}, timeout=180)
    resp.raise_for_status()
    return resp.content


def _read_members(zbytes: bytes, fragment: str, logged: bool = False) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        all_names = zf.namelist()
        names = [n for n in all_names if fragment in n]
        if not logged:
            log.info("zip members (%d): %s", len(all_names), all_names[:8])
        # disclosure zips sometimes nest a second zip; descend one level
        if not names:
            for n in all_names:
                if n.lower().endswith(".zip"):
                    inner = _read_members(zf.read(n), fragment, logged=True)
                    if not inner.empty:
                        return inner
            return pd.DataFrame()
        frames = [pd.read_csv(zf.open(n)) for n in names]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _fetch_disclosure(client: ErcotClient, emil: str, fragment: str,
                      op_d0: date, op_d1: date) -> pd.DataFrame:
    docs = _archive_docs(client, emil, op_d0, op_d1)
    frames = []
    for i, doc in enumerate(docs):
        doc_id = _doc_id(doc)
        if doc_id is None:
            continue
        df = _read_members(_download_zip(client, emil, doc_id), fragment,
                            logged=(i > 0))
        if not df.empty:
            log.info("  doc %s -> %d rows, cols=%s", doc_id, len(df), list(df.columns)[:14])
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def dam_awards(client: ErcotClient, op_d0: date, op_d1: date) -> pd.DataFrame:
    """DA Gen Resource energy awards from the 60-Day DAM disclosure."""
    return _fetch_disclosure(client, DAM_DISCLOSURE, DAM_GEN_CSV_FRAGMENT, op_d0, op_d1)


def sced_dispatch(client: ErcotClient, op_d0: date, op_d1: date) -> pd.DataFrame:
    """RT Gen Resource base points (dispatch) from the 60-Day SCED disclosure."""
    return _fetch_disclosure(client, SCED_DISCLOSURE, SCED_GEN_CSV_FRAGMENT, op_d0, op_d1)
