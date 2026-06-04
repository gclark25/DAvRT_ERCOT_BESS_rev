"""ERCOT data products used by the backtest.

Four inputs drive the DA-vs-RT settlement calc:

  1. DAM Settlement Point Prices (hourly DA LMP at each settlement point)
  2. RTM Settlement Point Prices (15-min RT LMP at each settlement point)
  3. 60-Day DAM Disclosure  -> ESR energy AWARDS (DA MW per hour)
  4. 60-Day SCED Disclosure -> ESR BASE POINTS (RT dispatch MW)

Products 1-2 are row-based JSON APIs. Products 3-4 are the 60-Day disclosure
ARCHIVE bundles (zipped CSVs). Disclosures post ~60 days AFTER the operating
day, so we search the archive by POSTED date = operating day + ~60.
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

DAM_SPP = "np4-190-cd/dam_stlmnt_pnt_prices"
RTM_SPP = "np6-905-cd/spp_node_zone_hub"

DAM_DISCLOSURE = "NP3-966-ER"
SCED_DISCLOSURE = "NP3-965-ER"

# Post-RTC+B (operating days from 2025-12-05 on) ESRs report in dedicated ESR
# files, not the Gen_Resource_Data files: 60d_DAM_ESR_Data / 60d_ESR_Data_in_SCED.
DAM_GEN_CSV_FRAGMENT = "DAM_ESR_Data"
SCED_GEN_CSV_FRAGMENT = "ESR_Data_in_SCED"

LAG_MIN = 57
LAG_MAX = 65


def dam_prices(client: ErcotClient, d0: date, d1: date,
               settlement_points: list[str] | None = None) -> pd.DataFrame:
    params = {"deliveryDateFrom": d0.isoformat(), "deliveryDateTo": d1.isoformat()}
    df = client.get_report(DAM_SPP, params)
    if settlement_points and "settlementPoint" in df.columns:
        df = df[df["settlementPoint"].isin(settlement_points)]
    return df.reset_index(drop=True)


def rtm_prices(client: ErcotClient, d0: date, d1: date,
               settlement_points: list[str] | None = None) -> pd.DataFrame:
    params = {"deliveryDateFrom": d0.isoformat(), "deliveryDateTo": d1.isoformat()}
    df = client.get_report(RTM_SPP, params)
    if settlement_points and "settlementPoint" in df.columns:
        df = df[df["settlementPoint"].isin(settlement_points)]
    return df.reset_index(drop=True)


def _archive_docs(client: ErcotClient, emil: str, op_d0: date, op_d1: date) -> list[dict]:
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
    time.sleep(2.2)
    resp = requests.get(url, headers=headers, params={"download": doc_id}, timeout=180)
    resp.raise_for_status()
    return resp.content


def _diagnose_zip(zbytes: bytes) -> None:
    """One-time dump of a disclosure zip: every member, plus columns and sample
    Resource Names for ESR/Gen/Load members, so we can confirm RTC+B layout."""
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        names = zf.namelist()
        log.info("DIAG full member list (%d): %s", len(names), names)
        for n in names:
            low = n.lower()
            if any(k in low for k in ("esr", "gen_resource", "load_resource")):
                try:
                    d = pd.read_csv(zf.open(n), nrows=2000)
                except Exception as e:  # noqa
                    log.info("DIAG %s: unreadable (%s)", n, e); continue
                rn = "Resource Name" if "Resource Name" in d.columns else None
                samp = sorted(d[rn].dropna().unique())[:12] if rn else "n/a"
                log.info("DIAG %s cols=%s", n, list(d.columns))
                log.info("DIAG %s sample resources=%s", n, samp)


def _read_members(zbytes: bytes, fragment: str, logged: bool = False) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        all_names = zf.namelist()
        names = [n for n in all_names if fragment in n]
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
        zbytes = _download_zip(client, emil, doc_id)
        if i == 0:
            _diagnose_zip(zbytes)   # one-time structure dump for the first doc
        df = _read_members(zbytes, fragment)
        if not df.empty:
            log.info("  doc %s -> %d rows, cols=%s", doc_id, len(df), list(df.columns)[:14])
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def dam_awards(client: ErcotClient, op_d0: date, op_d1: date) -> pd.DataFrame:
    """DA ESR energy awards from the 60-Day DAM disclosure."""
    return _fetch_disclosure(client, DAM_DISCLOSURE, DAM_GEN_CSV_FRAGMENT, op_d0, op_d1)


def sced_dispatch(client: ErcotClient, op_d0: date, op_d1: date) -> pd.DataFrame:
    """ESR base points (dispatch) from the 60-Day SCED disclosure."""
    return _fetch_disclosure(client, SCED_DISCLOSURE, SCED_GEN_CSV_FRAGMENT, op_d0, op_d1)
