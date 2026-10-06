"""Policies. Everything tunable lives here, nothing is hidden in the logic."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "db" / "market.sqlite"
OUT_DIR = ROOT / "out"  # publication root: manifest.json (the "latest" pointer), releases/<hash>/, health.json
RUNS_DIR = DATA_DIR / "runs"
MAX_RUN_RECORDS = 30
MAX_ATTEMPTS_PER_SLOT = 3  # weekly policy: Sunday 10:00 / 14:00 / 20:00 Asia/Taipei, then stop; never a polling loop
MAX_ATTEMPTS_PER_DAY = MAX_ATTEMPTS_PER_SLOT  # old name


@dataclass(frozen=True)
class RequestPolicy:
    """Polite batch fetching. There is deliberately no knob for rotating identities, proxies or browser automation."""

    user_agent: str = "market-intelligence-tw/0.1 (official open-data batch client; one request per dataset per day)"
    timeout_s: float = 40.0
    min_delay_s: float = 2.5  # minimum gap between two requests to the same host
    max_attempts: int = 2  # network error / 5xx only. 403, 429, HTML, schema drift never retry.
    backoff_s: tuple = (6.0, 18.0)


@dataclass(frozen=True)
class ReconciliationPolicy:
    """Official P/E vs calculated P/E. Relative difference = |calc - official| / official."""

    match_max: float = 0.01
    minor_max: float = 0.10  # above this => MATERIAL_DIFFERENCE


@dataclass(frozen=True)
class OutlierPolicy:
    """ROBUST distribution. Explicit and configurable; the legacy rules (P/E >= 200 dropped, IQR x 1.5) are NOT reused."""

    name: str = "TUKEY_K3_ON_POSITIVE_PE"
    iqr_k: float = 3.0
    min_sample: int = 8  # below this nothing is trimmed: too few points to call anything an outlier
    min_stat_sample: int = 3  # below this no statistics are published for the group


REQUEST_POLICY = RequestPolicy()
RECONCILIATION_POLICY = ReconciliationPolicy()
OUTLIER_POLICY = OutlierPolicy()
