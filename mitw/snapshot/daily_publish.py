"""Atomic daily publication of the TWSE / TPEx release (Phase 3I.2). One release, one manifest; each market carries ITS OWN market date.

Same guarantees as snapshot/build.py (release written and re-read before manifest.json is replaced, manifest last, hash verified), with the
weekly date rules replaced by the daily ones: a market must meet ITS OWN target (TWSE T-1 and TPEx T is valid), never move backwards, and at least
one market must be newer than the Last Known Good. The weekly publish() / evaluate_gate() are untouched and keep working side by side.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .. import daily as D
from ..contracts.version import SNAPSHOT_SCHEMA_VERSION
from ..pipeline import BuildResult
from ..retention import prune_daily
from ..trading_calendar import TradingCalendar
from .build import FILES, PublishRefused, dumps, make_documents, read_manifest, sha, source_hash, strip_runtime, write_atomic
from .gate import GateResult, SUPPORTED_MAJOR, _add
from .validate import validate_documents


@dataclass
class DailyContext:
    decision: D.Decision
    calendar: TradingCalendar
    raw_partitions: dict = field(default_factory=dict)   # market -> raw partition the market's data came from
    carried: tuple = ()                                   # markets carried from the Last Known Good
    fallbacks: tuple = ()                                 # carried markets that had to be fetched after all
    attempt: str = ""
    trigger: str = "schedule"
    source_counts: dict = field(default_factory=dict)


def evaluate_gate_daily(market: dict, industry: dict, previous: Optional[dict], now: datetime, ctx: DailyContext, previous_market: Optional[dict] = None,
                        allow_republish: bool = False) -> GateResult:
    r = GateResult()
    versions = {market.get("schemaVersion"), industry.get("schemaVersion")}
    major_ok = all(isinstance(v, str) and v.split(".")[0].isdigit() and int(v.split(".")[0]) == SUPPORTED_MAJOR for v in versions)
    _add(r, "schema", major_ok and len(versions) == 1, f"schemaVersion {sorted(map(str, versions))}, supported {SUPPORTED_MAJOR}.x")
    problems = validate_documents(market, industry, previous_market)
    _add(r, "coverage", not problems, "; ".join(problems[:4]) if problems else "coverage and statistics consistent")
    official = [s for s in industry.get("snapshots", []) if s.get("metricSeries") == "OFFICIAL_PE" and not s.get("subIndustryCode")]
    meth_ok = bool(official) and all(isinstance(s.get("methodology", {}).get("outlierPolicy"), dict) and "robustFences" in s["methodology"] and s["methodology"].get("percentile") for s in official)
    basis_ok = (market.get("basisFinding") or {}).get("officialPEBasis") == "OFFICIAL_TTM"
    _add(r, "methodology", meth_ok and basis_ok, "outlier policy, fences and percentile definition present; official P/E basis OFFICIAL_TTM" if meth_ok and basis_ok else f"outlierPolicyPresent={meth_ok} officialPEBasisIsOfficialTTM={basis_ok}")

    mad = market.get("marketAsOf") or {}
    parsed = {m: D.parse_market_date(mad.get(m)) for m in D.MAIN_MARKETS}
    present = all(parsed.values())
    _add(r, "market-date-present", present, f"marketAsOf={mad}" if present else f"a market date is missing or unparseable: {mad}")
    prev_mad = (previous or {}).get("marketAsOf") or {}
    prev = {m: D.parse_market_date(prev_mad.get(m)) for m in D.MAIN_MARKETS}
    if previous and present:
        older = [m for m in D.MAIN_MARKETS if prev[m] and parsed[m] < prev[m]]
        _add(r, "not-older-than-latest", not older, f"{older} would move backwards (latest {prev_mad})" if older else f"no market moves backwards (latest {prev_mad})")
        newer = [m for m in D.MAIN_MARKETS if prev[m] is None or parsed[m] > prev[m]]
        if not newer and allow_republish:
            r.exceptions.append({"code": "REPUBLISH_SAME_MARKET_DATE", "detail": f"no market newer than {prev_mad}", "allowedExplicitly": True})
            _add(r, "a-market-advances", True, "EXCEPTION: re-publication of the same market dates allowed explicitly")
        else:
            _add(r, "a-market-advances", bool(newer), f"newer: {newer}" if newer else f"NO_NEW_MARKET_DATA: no market is newer than the published {prev_mad}")
    else:
        _add(r, "not-older-than-latest", True, "no previous publication to compare with")
        _add(r, "a-market-advances", True, "no previous publication to compare with")

    # every market in a release meets ITS OWN target; only an explicitly recorded PARTIAL may include a market that is behind it
    if present:
        behind_ok = set(ctx.decision.behind) if ctx.decision.state == D.SUCCESS_PARTIAL else set()
        bad = []
        for m in D.MAIN_MARKETS:
            tgt, unknown = D.target_date(m, now, ctx.calendar)
            if unknown or tgt is None:
                continue  # the calendar cannot say: the decision already said CALENDAR_UNKNOWN; freshness-determinable below reports it
            if parsed[m] < tgt and m not in behind_ok:
                bad.append(f"{m} {parsed[m]} < its target {tgt}")
        if behind_ok:
            r.exceptions.append({"code": "PARTIAL_MARKET_TARGET", "detail": f"{sorted(behind_ok)} did not reach their own target (final attempt)", "allowedExplicitly": True})
        _add(r, "each-market-meets-its-own-target", not bad, "; ".join(bad) if bad else "every market meets its own target date (dates may differ between markets)")
    else:
        _add(r, "each-market-meets-its-own-target", False, "market dates missing")

    verdicts = {m: D.classify_market(m, parsed[m], now, ctx.calendar) for m in D.MAIN_MARKETS} if present else {}
    unknown = [m for m, v in verdicts.items() if v["status"] == D.UNKNOWN]
    _add(r, "freshness-determinable", present and not unknown, "; ".join(f"{m}: {verdicts[m]['status']}" for m in verdicts) if not unknown else f"UNKNOWN: {unknown}")
    return r


def _market_block(m: str, res: BuildResult, ctx: DailyContext, now: datetime, previous: Optional[dict]) -> dict:
    actual = D.parse_market_date(res.market_as_of.get(m))
    tgt, _ = D.target_date(m, now, ctx.calendar)
    prev = D.parse_market_date(((previous or {}).get("marketAsOf") or {}).get(m))
    a = D.AVAILABILITY[m]
    return {"marketDate": actual.isoformat() if actual else None, "expectedMarketDate": tgt.isoformat() if tgt else None,
            "targetReached": bool(actual and tgt and actual >= tgt), "aheadOfTarget": bool(actual and tgt and actual > tgt),
            "previousPublishedMarketDate": prev.isoformat() if prev else None, "carried": m in ctx.carried and m not in ctx.fallbacks,
            "rawPartition": ctx.raw_partitions.get(m), "availabilityBasis": a.basis, "evidenceLevel": D.EVIDENCE_LEVEL,
            "expectedBy": a.expected_by.strftime("%H:%M"), "offsetDays": a.offset_days}


def publish_daily(res: BuildResult, out_root: Path, generated_at: str, published_at: str, now: datetime, ctx: DailyContext, allow_republish: bool = False) -> tuple[dict, str]:
    """Gate -> write the whole release -> re-read and verify it -> only then replace manifest.json. Returns (manifest, outcome)."""
    out_root = Path(out_root)
    previous = read_manifest(out_root)
    previous_market = previous_industry = None
    if previous:
        try:
            prev_dir = (out_root / previous["files"]["market-snapshot.json"]["path"]).parent
            previous_market = json.loads((prev_dir / "market-snapshot.json").read_text(encoding="utf-8"))
            previous_industry = json.loads((prev_dir / "industry-snapshots.json").read_text(encoding="utf-8"))
        except (KeyError, OSError, json.JSONDecodeError):
            previous_market = previous_industry = None
    market, industry = make_documents(res, generated_at)
    if previous_market and previous_industry and dumps(strip_runtime(market)) == dumps(strip_runtime(previous_market)) and dumps(strip_runtime(industry)) == dumps(strip_runtime(previous_industry)):
        return previous, "UNCHANGED"
    gate = evaluate_gate_daily(market, industry, previous, now, ctx, previous_market, allow_republish)
    if not gate.passed:
        raise PublishRefused(gate.failures, gate)

    blobs = {"market-snapshot.json": dumps(market), "industry-snapshots.json": dumps(industry)}
    hashes = {n: sha(b) for n, b in blobs.items()}
    full_hash = source_hash(hashes)
    release = full_hash[:12]
    rel_dir = out_root / "releases" / release
    for n, b in blobs.items():
        write_atomic(rel_dir / n, b)
    for n in FILES:  # verify what is actually on disk, not what we meant to write
        if sha((rel_dir / n).read_bytes()) != hashes[n]:
            raise PublishRefused([f"hash: {n} on disk does not match what was written"], gate)

    parsed = {m: D.parse_market_date(res.market_as_of.get(m)) for m in D.MAIN_MARKETS}
    blocks = {m: _market_block(m, res, ctx, now, previous) for m in D.MAIN_MARKETS}
    verdicts = {m: D.classify_market(m, parsed[m], now, ctx.calendar) for m in D.MAIN_MARKETS}
    manifest = {
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION, "release": release, "runDate": res.run_date, "generatedAt": generated_at,
        "sourceFetchedAt": market["sourceFetchedAt"], "publishedAt": published_at,
        "updatePolicy": D.POLICY, "timezone": D.TIMEZONE_NAME, "lastSuccessfulPublication": published_at,
        "publicationPolicy": D.publication_policy(ctx.calendar),
        "marketAsOf": market["marketAsOf"], "financialAsOf": market["financialAsOf"], "markets": blocks,
        "decision": {"state": ctx.decision.state, "advanced": list(ctx.decision.advanced), "behind": list(ctx.decision.behind), "carried": list(ctx.carried),
                     "fallbacks": list(ctx.fallbacks), "reason": ctx.decision.reason, "attempt": ctx.attempt, "trigger": ctx.trigger},
        "sourceHash": full_hash, "companyCount": len(market["companies"]),
        "files": {n: {"sha256": hashes[n], "bytes": len(blobs[n]), "path": f"releases/{release}/{n}"} for n in FILES},
        "freshnessAtPublish": {m: {k: v[k] for k in ("status", "expectedMarketDate", "lagTradingDays")} for m, v in verdicts.items()},
        "gate": gate.to_json(),
    }
    write_atomic(rel_dir / "release.json", dumps(manifest))
    write_atomic(out_root / "manifest.json", dumps(manifest))  # LAST: the commit point
    prune_daily(out_root / "releases", now, protect={release, (previous or {}).get("release") or release})
    return manifest, "PUBLISHED"
