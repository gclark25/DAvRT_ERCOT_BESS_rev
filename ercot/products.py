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
# NP6-332-CD = Real-Time Clearing Prices for Capacity by SCED Interval (RT AS MCPC).
# Endpoint sub-path confirmed by discover_rt_as_endpoint() on first run.
RT_AS_MCPC = "np6-332-cd"


def discover_rt_as_endpoint(client) -> None:
    """One-time probe of the confirmed NP6-332-CD row endpoint: capture the
    date-filter parameter name, the column list, and a couple of sample rows."""
    import json as _json
    import requests
    url = f"{BASE}/{RT_AS_MCPC}/rt_clear_price_cap_sced"
    headers = client._auth.headers(client._key)
    param_sets = [
        {"SCEDTimestampFrom": "2026-04-02T00:00:00", "SCEDTimestampTo": "2026-04-02T00:30:00"},
        {"SCEDTimeStampFrom": "2026-04-02T00:00:00", "SCEDTimeStampTo": "2026-04-02T00:30:00"},
        {"deliveryDateFrom": "2026-04-02", "deliveryDateTo": "2026-04-02"},
    ]
    for p in param_sets:
        p["size"] = 3
        try:
            client._throttle()
            r = requests.get(url, headers=headers, params=p, timeout=60)
            if r.status_code == 200:
                b = r.json()
                fields = [f.get("name") for f in b.get("fields", [])]
                rows = b.get("data", [])
                log.info("DISCOVER endpoint OK params=%s fields=%s", list(p)[:-1], fields)
                log.info("DISCOVER sample rows=%s", _json.dumps(rows[:3])[:800])
                return
            log.info("DISCOVER endpoint params=%s status=%s body=%s",
                     list(p)[:-1], r.status_code, r.text[:200])
        except Exception as e:  # noqa
            log.info("DISCOVER endpoint params=%s error %s", list(p)[:-1], e)

# ---- 60-day disclosure archives (zipped CSV bundles) --------------------------
DAM_DISCLOSURE = "NP3-966-ER"   # 60-Day DAM Disclosure Reports (awards)
SCED_DISCLOSURE = "NP3-965-ER"  # 60-Day SCED Disclosure Reports (base points)

# CSV member-name fragments inside each disclosure zip.
# Post-RTC+B (operating days from 2025-12-05 on) ESRs report in dedicated ESR
# files, not the Gen_Resource_Data files: 60d_DAM_ESR_Data / 60d_ESR_Data_in_SCED.
DAM_GEN_CSV_FRAGMENT = "DAM_ESR_Data"
SCED_GEN_CSV_FRAGMENT = "ESR_Data_in_SCED"

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


# Only these columns are needed downstream; the ESR SCED file has ~190 columns,
# so restricting the parse keeps memory bounded over a full-year backfill.
AWARD_COLS = {"Delivery Date", "Hour Ending", "Resource Name",
              "Awarded Quantity", "Settlement Point Name",
              # DA ancillary-service awards (MW) and their clearing prices (MCPC):
              "RegUp Awarded", "RegUp MCPC", "RegDown Awarded", "RegDown MCPC",
              "RRSPFR Awarded", "RRSFFR Awarded", "RRSUFR Awarded", "RRS MCPC",
              "ECRSSD Awarded", "ECRS MCPC", "NonSpin Awarded", "NonSpin MCPC"}
DISPATCH_COLS = {"SCED Time Stamp", "Resource Name", "Base Point"}


def _read_members(zbytes: bytes, fragment: str, usecols: set | None = None) -> pd.DataFrame:
    keep = (lambda c: c in usecols) if usecols else None
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        all_names = zf.namelist()
        names = [n for n in all_names if fragment in n]
        if not names:
            for n in all_names:
                if n.lower().endswith(".zip"):
                    inner = _read_members(zf.read(n), fragment, usecols)
                    if not inner.empty:
                        return inner
            return pd.DataFrame()
        frames = [pd.read_csv(zf.open(n), usecols=keep) for n in names]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _fetch_disclosure(client: ErcotClient, emil: str, fragment: str,
                      op_d0: date, op_d1: date, usecols: set | None = None) -> pd.DataFrame:
    docs = _archive_docs(client, emil, op_d0, op_d1)
    frames = []
    for doc in docs:
        doc_id = _doc_id(doc)
        if doc_id is None:
            continue
        df = _read_members(_download_zip(client, emil, doc_id), fragment, usecols)
        if not df.empty:
            frames.append(df)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    log.info("  %s: %d docs, %d rows", emil, len(docs), len(out))
    return out


def dam_awards(client: ErcotClient, op_d0: date, op_d1: date) -> pd.DataFrame:
    """DA ESR energy awards from the 60-Day DAM disclosure."""
    return _fetch_disclosure(client, DAM_DISCLOSURE, DAM_GEN_CSV_FRAGMENT,
                             op_d0, op_d1, AWARD_COLS)


def sced_dispatch(client: ErcotClient, op_d0: date, op_d1: date) -> pd.DataFrame:
    """ESR base points (dispatch) from the 60-Day SCED disclosure."""
    return _fetch_disclosure(client, SCED_DISCLOSURE, SCED_GEN_CSV_FRAGMENT,
                             op_d0, op_d1, DISPATCH_COLS)
