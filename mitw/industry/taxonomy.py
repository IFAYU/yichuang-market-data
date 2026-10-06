"""L1 (official industry) -> L2 (internal sub-industry) schema, plus a proof of concept for 半導體業 only.

Schema (one row per assignment, so history and review are possible):
    ticker, level ("L2"), code, name, assignedBy, note, effectiveFrom

The POC assignments in taxonomy/semiconductor_l2_poc.json were written from general knowledge of each company's business model,
NOT from an official source. They exist only to prove the L1 -> L2 mechanism end to end and MUST be reviewed by a person before
anyone relies on them. A ticker that is not actually in the official L1 industry is reported and skipped, never forced in.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..contracts.models import Company

POC_FILE = Path(__file__).with_name("taxonomy_data") / "semiconductor_l2_poc.json"
ASSIGNED_BY_POC = "POC_ASSISTANT_UNREVIEWED"


@dataclass(frozen=True)
class L2Assignment:
    ticker: str
    l1Code: str
    code: str
    name: str
    assignedBy: str
    note: str


def load_poc(path: Path = POC_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def apply_l2(companies: list[Company], poc: dict) -> tuple[dict[str, L2Assignment], list[str]]:
    """ticker -> L2 assignment, for companies whose official L1 code equals the POC's l1Code. Returns (assignments, warnings)."""
    l1 = poc["l1Code"]
    names = {c["code"]: c["name"] for c in poc["categories"]}
    by_ticker = {c.ticker: c for c in companies}
    out: dict[str, L2Assignment] = {}
    warnings: list[str] = []
    for a in poc["assignments"]:
        t, code = a["ticker"], a["l2"]
        comp = by_ticker.get(t)
        if comp is None:
            warnings.append(f"POC ticker {t} is not in the company universe; skipped")
        elif comp.officialIndustryCode != l1:
            warnings.append(f"POC ticker {t} ({comp.companyName}) is in official industry {comp.officialIndustryCode}, not {l1}; skipped, not forced")
        elif code not in names:
            warnings.append(f"POC ticker {t} uses unknown L2 code {code}; skipped")
        else:
            out[t] = L2Assignment(t, l1, code, names[code], ASSIGNED_BY_POC, a.get("note", ""))
    return out, warnings
