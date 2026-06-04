"""Load credentials and paths from the project .env / environment."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()


def require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name} (set it in .env)")
    return val


USERNAME = os.environ.get("ERCOT_USERNAME", "")
PASSWORD = os.environ.get("ERCOT_PASSWORD", "")
SUBSCRIPTION_KEY = os.environ.get("ERCOT_SUBSCRIPTION_KEY", "")

DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
CONFIG_DIR = ROOT / "config"
DOCS_DIR = ROOT / "docs"          # GitHub Pages dashboard
for _d in (DATA_DIR, REPORTS_DIR, CONFIG_DIR, DOCS_DIR):
    _d.mkdir(exist_ok=True)
