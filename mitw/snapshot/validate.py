"""Pre-publication checks. Any problem => the new snapshot is NOT published and the previous latest stays."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ..contracts.version import SNAPSHOT_SCHEMA_VERSION


@dataclass(frozen=True)
class PublishPolicy:
    min_companies: int = 1500
    min_twse: int = 800
    min_tpex: int = 600
    min_price_coverage: float = 0.90
    min_official_pe_coverage: float = 0.50
    max_company_drop: float = 0.05  # vs previous latest
    max_price_coverage_drop: float = 0.05


POLICY = PublishPolicy()


def _finite(o, path="") -> list[str]:
    bad = []
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        bad.append(path)
    elif isinstance(o, dict):
        for k, v in o.items():
            bad += _finite(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            bad += _finite(v, f"{path}[{i}]")
    return bad


def validate_documents(market: dict, industry: dict, previous: Optional[dict] = None, policy: PublishPolicy = POLICY) -> list[str]:
    p: list[str] = []
    for name, doc in (("market", market), ("industry", industry)):
        if doc.get("schemaVersion") != SNAPSHOT_SCHEMA_VERSION:
            p.append(f"{name}: schemaVersion {doc.get('schemaVersion')!r} != {SNAPSHOT_SCHEMA_VERSION!r}")
        for k in ("generatedAt", "marketAsOf", "financialAsOf", "sourceVersions"):
            if k not in doc:
                p.append(f"{name}: missing {k}")
        bad = _finite(doc)
        if bad:
            p.append(f"{name}: non-finite numbers at {bad[:3]}")

    comps = market.get("companies", [])
    n = len(comps)
    twse = sum(1 for c in comps if c["market"] == "TWSE")
    tpex = n - twse
    if n < policy.min_companies:
        p.append(f"only {n} companies (< {policy.min_companies})")
    if twse < policy.min_twse:
        p.append(f"only {twse} TWSE companies (< {policy.min_twse})")
    if tpex < policy.min_tpex:
        p.append(f"only {tpex} TPEx companies (< {policy.min_tpex})")
    if n:
        px = sum(1 for c in comps if c["price"]["close"] is not None) / n
        pe = sum(1 for c in comps if c["officialPE"]["value"] is not None) / n
        if px < policy.min_price_coverage:
            p.append(f"price coverage {px:.1%} < {policy.min_price_coverage:.0%}")
        if pe < policy.min_official_pe_coverage:
            p.append(f"official P/E coverage {pe:.1%} < {policy.min_official_pe_coverage:.0%}")
        if previous and previous.get("companies"):
            pn = len(previous["companies"])
            if n < pn * (1 - policy.max_company_drop):
                p.append(f"company count fell from {pn} to {n}")
            ppx = sum(1 for c in previous["companies"] if c["price"]["close"] is not None) / pn
            if px < ppx - policy.max_price_coverage_drop:
                p.append(f"price coverage fell from {ppx:.1%} to {px:.1%}")

    for s in industry.get("snapshots", []):
        tag = f"{s['metricSeries']} {s['industryCode']}/{s['subIndustryCode']}"
        if s["validCount"] != len(s["includedCompanies"]):
            p.append(f"{tag}: validCount != includedCompanies")
        if s["companyCount"] != s["validCount"] + s["excludedCount"]:
            p.append(f"{tag}: companyCount != valid + excluded")
        for key in ("raw", "robust"):
            st = s[key]
            if st and not (st["p25"] <= st["median"] <= st["p75"]):
                p.append(f"{tag}: {key} percentiles out of order")
            if st and not (st["min"] <= st["p25"] and st["p75"] <= st["max"]):
                p.append(f"{tag}: {key} min/max inconsistent")
    return p
