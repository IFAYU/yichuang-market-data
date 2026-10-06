"""Small, strict parsers for the strings the exchanges publish. Anything unparseable becomes None, never 0."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

_MISSING = {"", "-", "--", "---", "----", "n/a", "na", "nan", "null", "none", "－", "—"}


def parse_num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("　", "").replace(" ", "")
    if s.lower() in _MISSING:
        return None
    s = s.rstrip("%")
    try:
        x = float(s)
    except ValueError:
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def parse_price(v) -> Optional[float]:
    """A closing price must be strictly positive; '0.00' / '---' mean 'no trade', not a price of zero."""
    x = parse_num(v)
    return x if x is not None and x > 0 else None


def parse_int(v) -> Optional[int]:
    x = parse_num(v)
    return int(round(x)) if x is not None else None


def parse_tw_date(v) -> Optional[str]:
    """ROC '1151005' / '115/10/05' / '115-10-05', or Gregorian '20261005' / '2026-10-05' -> ISO date string."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{2,3})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 1911 if y < 1911 else y
    else:
        digits = re.sub(r"\D", "", s)
        if len(digits) == 7:  # ROC YYYMMDD
            y, mo, d = int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7])
        elif len(digits) == 8:  # YYYYMMDD
            y, mo, d = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        elif len(digits) == 6 and not s.count("/"):  # ROC YYMMDD (e.g. 990101)
            y, mo, d = int(digits[:2]) + 1911, int(digits[2:4]), int(digits[4:6])
        else:
            return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def parse_roc_year(v) -> Optional[int]:
    x = parse_int(v)
    if x is None:
        return None
    return x + 1911 if x < 1911 else x


def parse_year_month(v) -> Optional[str]:
    """'11509' (ROC) or '202609' -> '2026-09'."""
    if v is None:
        return None
    d = re.sub(r"\D", "", str(v))
    if len(d) == 5:
        y, m = int(d[:3]) + 1911, int(d[3:])
    elif len(d) == 6:
        y, m = int(d[:4]), int(d[4:])
    else:
        return None
    return f"{y:04d}-{m:02d}" if 1 <= m <= 12 else None
