"""Thin client over the ERCOT Public API with paging + retry.

The Public API ("public-reports") returns JSON with a `data` array of rows and
a `fields` array describing columns, plus a `_meta` block with paging info.
This client normalizes any product into a pandas DataFrame, following pages.

NOTE ON PRODUCT IDS: the exact EMIL product paths are confirmed against the
live catalog at https://apiexplorer.ercot.com once network access is open.
Best-known paths are defined in products.py and are easy to adjust.
"""
from __future__ import annotations

import re
import time
from typing import Any

import pandas as pd
import requests

from .auth import ErcotAuth

BASE = "https://api.ercot.com/api/public-reports"


class ErcotClient:
    def __init__(self, auth: ErcotAuth, subscription_key: str, timeout: int = 120,
                 min_interval: float = 2.2, max_retries: int = 8):
        self._auth = auth
        self._key = subscription_key
        self._timeout = timeout
        # ERCOT public API allows ~30 requests/min. Spacing requests ~2.2s
        # apart keeps us safely under that ceiling.
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last_request = 0.0

    @staticmethod
    def _retry_after(resp) -> float:
        """Seconds to wait, from the Retry-After header or the 429 message."""
        hdr = resp.headers.get("Retry-After")
        if hdr:
            try:
                return float(hdr)
            except ValueError:
                pass
        m = re.search(r"in\s+(\d+)\s+second", resp.text or "")
        return float(m.group(1)) if m else 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.time()

    def _get(self, url: str, params: dict[str, Any]) -> dict:
        last_err = None
        for attempt in range(self._max_retries):
            self._throttle()
            headers = self._auth.headers(self._key)
            try:
                resp = requests.get(url, headers=headers, params=params,
                                    timeout=self._timeout)
            except requests.exceptions.RequestException as e:
                # network hiccup / read timeout on a large page -> back off & retry
                last_err = f"network: {type(e).__name__}"
                time.sleep(2 ** attempt + 1)
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                # Honor ERCOT's stated wait; fall back to exponential backoff.
                wait = self._retry_after(resp) or (2 ** attempt)
                last_err = f"{resp.status_code}: {resp.text[:200]}"
                time.sleep(wait + 1)
                continue
            raise RuntimeError(f"ERCOT API error {resp.status_code}: {resp.text[:300]}")
        raise RuntimeError(f"ERCOT API failed after retries: {last_err}")

    def get_report(self, path: str, params: dict[str, Any] | None = None,
                   page_size: int = 100_000, max_pages: int = 10_000) -> pd.DataFrame:
        """Fetch a public-reports product as a DataFrame, following all pages."""
        params = dict(params or {})
        params["size"] = page_size
        url = f"{BASE}/{path.lstrip('/')}"

        frames: list[pd.DataFrame] = []
        page = 1
        fields: list[str] | None = None
        while page <= max_pages:
            params["page"] = page
            body = self._get(url, params)
            if fields is None:
                fields = [f["name"] for f in body.get("fields", [])]
            rows = body.get("data", [])
            if not rows:
                break
            frames.append(pd.DataFrame(rows, columns=fields))
            meta = body.get("_meta", {}) or {}
            total_pages = meta.get("totalPages")
            if total_pages is not None and page >= total_pages:
                break
            if len(rows) < page_size:
                break
            page += 1

        if not frames:
            return pd.DataFrame(columns=fields or [])
        return pd.concat(frames, ignore_index=True)
