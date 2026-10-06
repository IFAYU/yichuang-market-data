"""Official industry code -> name, built from the exchanges' own data (company feed has the code, EPS feed prints the name).

Nothing is typed in by hand except codes the feeds cannot name (see FALLBACK_NAMES, flagged in the output).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace

from ..contracts.models import Company

# Only for codes that never appear with a name in any feed. Reported as MANUAL_FALLBACK, never silently.
FALLBACK_NAMES: dict[str, str] = {}


def build_industry_names(companies: list[Company], ticker_to_name: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Returns (code -> name, warnings). The most common name per code wins; conflicts are reported."""
    votes: dict[str, Counter] = defaultdict(Counter)
    for c in companies:
        name = ticker_to_name.get(c.ticker)
        if name:
            votes[c.officialIndustryCode][name] += 1
    names, warnings = {}, []
    for code, cnt in votes.items():
        names[code] = cnt.most_common(1)[0][0]
        if len(cnt) > 1:
            warnings.append(f"industry code {code} has several names {dict(cnt)}; using {names[code]!r}")
    for c in companies:
        code = c.officialIndustryCode
        if code and code not in names:
            if code in FALLBACK_NAMES:
                names[code] = FALLBACK_NAMES[code]
                warnings.append(f"industry code {code} named from MANUAL_FALLBACK: {names[code]}")
            else:
                names[code] = f"產業代碼{code}"
                warnings.append(f"industry code {code} has no official name in any feed; labelled {names[code]!r}")
    return names, sorted(set(warnings))


def attach_industry_names(companies: list[Company], names: dict[str, str]) -> list[Company]:
    return [replace(c, officialIndustryName=names.get(c.officialIndustryCode, "")) for c in companies]
