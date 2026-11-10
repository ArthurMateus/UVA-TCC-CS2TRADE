"""Paths, constants and settings shared by the CLI and the web app.

Every path is anchored at the project root (or TRADEUP_HOME), never at the
current working directory, so `tradeup scan` works from any folder.
Secrets come from environment variables, optionally loaded from a `.env`
file at the project root (never committed: see .gitignore).
"""
from __future__ import annotations

import os
from pathlib import Path

from .models import Rarity

ROOT = Path(os.getenv("TRADEUP_HOME") or Path(__file__).resolve().parents[1])
DATA_DIR = ROOT / "data"


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides real env vars."""
    p = path or ROOT / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


load_dotenv()


def _path(env: str, default: Path) -> Path:
    v = os.getenv(env)
    if not v:
        return default
    p = Path(v)
    return p if p.is_absolute() else ROOT / p


SNAPSHOT = _path("TRADEUP_PRICE_SNAPSHOT", DATA_DIR / "price_snapshot.json")
CACHE_DB = _path("TRADEUP_CACHE", DATA_DIR / "prices.sqlite")
CHECKPOINTS = DATA_DIR / "sweep_checkpoints.json"

# Postgres/Supabase (optional). When set, prices are also written there.
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
if DATABASE_URL and "[" in DATABASE_URL:          # placeholder still in .env
    DATABASE_URL = None
CSFLOAT_API_KEY = os.getenv("CSFLOAT_API_KEY")
DEFAULT_CURRENCY = (os.getenv("TRADEUP_CURRENCY") or "BRL").upper()

RARITIES: dict[str, Rarity] = {
    "consumer": Rarity.CONSUMER, "industrial": Rarity.INDUSTRIAL,
    "milspec": Rarity.MILSPEC, "mil-spec": Rarity.MILSPEC,
    "restricted": Rarity.RESTRICTED, "classified": Rarity.CLASSIFIED,
    "covert": Rarity.COVERT,
}
TRADEABLE = (Rarity.CONSUMER, Rarity.INDUSTRIAL, Rarity.MILSPEC,
             Rarity.RESTRICTED, Rarity.CLASSIFIED, Rarity.COVERT)


def contract_size(r: Rarity) -> int:
    """Covert -> knife/glove contracts take 5 inputs; every other tier 10."""
    return 5 if r == Rarity.COVERT else 10
