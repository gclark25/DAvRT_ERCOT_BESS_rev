"""ERCOT Public API authentication.

ERCOT's Public API uses an Azure AD B2C Resource Owner Password Credentials
(ROPC) flow to issue a bearer token, plus an APIM subscription key sent on
every request. This module fetches and caches a token, refreshing it before
expiry.

Reference: https://apiexplorer.ercot.com  (Authorization section)
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass

import requests

# ERCOT public API B2C constants (public, not secret).
TOKEN_URL = (
    "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
    "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
)
CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"
SCOPE = f"openid {CLIENT_ID} offline_access"

# Refresh this many seconds before the token actually expires.
_REFRESH_MARGIN = 120


@dataclass
class _Token:
    value: str
    expires_at: float


class ErcotAuth:
    """Thread-safe ERCOT token provider.

    Usage:
        auth = ErcotAuth(username, password)
        headers = auth.headers(subscription_key)
    """

    def __init__(self, username: str, password: str, timeout: int = 30):
        self._username = username
        self._password = password
        self._timeout = timeout
        self._token: _Token | None = None
        self._lock = threading.Lock()

    def _fetch(self) -> _Token:
        data = {
            "grant_type": "password",
            "username": self._username,
            "password": self._password,
            "response_type": "id_token",
            "scope": SCOPE,
            "client_id": CLIENT_ID,
        }
        resp = requests.post(TOKEN_URL, data=data, timeout=self._timeout)
        if resp.status_code != 200:
            raise RuntimeError(
                f"ERCOT auth failed ({resp.status_code}): {resp.text[:300]}"
            )
        body = resp.json()
        token = body.get("access_token") or body.get("id_token")
        if not token:
            raise RuntimeError(f"No token in ERCOT auth response: {list(body)}")
        expires_in = int(body.get("expires_in", 3600))
        return _Token(value=token, expires_at=time.time() + expires_in)

    def token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token is None or now >= (self._token.expires_at - _REFRESH_MARGIN):
                self._token = self._fetch()
            return self._token.value

    def headers(self, subscription_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token()}",
            "Ocp-Apim-Subscription-Key": subscription_key,
        }
