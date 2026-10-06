"""Static JSON contract + atomic publication.

Layout (identical in out/ here and in the consuming app's public/market-data/):

    manifest.json                       the ONLY "latest" pointer: names a release and carries every file's sha256
    releases/<sourceHash[:12]>/         one complete, immutable release: market-snapshot.json, industry-snapshots.json, release.json
    health.json                         ingestion run summary (information only; never used to decide freshness)

A release is written and re-read (hash-verified) in full BEFORE manifest.json is replaced, and manifest.json is replaced last and
atomically. Readers always read the manifest first and then exactly the two files it names, so "new manifest + old snapshot + new
industry file" cannot happen. Rolling back is re-pointing the manifest at an earlier release.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..contracts.version import PIPELINE_VERSION, SNAPSHOT_SCHEMA_VERSION
from ..freshness import classify, weekly_metadata
from ..pipeline import BuildResult
from .gate import evaluate_gate

FILES = ("market-snapshot.json", "industry-snapshots.json")
KEEP_RELEASES = 14

METRIC_DEFINITIONS = {
    "marketCap": {"formula": "close * sharesOutstanding", "unit": "TWD", "basis": "SPOT"},
    "officialPE": {"formula": "as published by TWSE/TPEx", "unit": "multiple", "basis": "OFFICIAL_TTM", "note": "exchange definition: closing price / EPS of the latest four quarters (EPS <= 0: not published); see basisFinding.officialPEDefinition"},
    "calculatedPE": {"formula": "close / (eps_ytd * 4 / quarter)", "unit": "multiple", "basis": "ANNUALIZED_YTD", "note": "run-rate, NOT trailing-twelve-month"},
    "ps": {"formula": "marketCap / (revenue_ytd_twd * 4 / quarter)", "unit": "multiple", "basis": "ANNUALIZED_YTD", "note": "run-rate; not applicable to financial companies"},
    "eps": {"unit": "TWD per share", "basis": "CUMULATIVE_YTD (confirmed by evidence, see basisFinding)"},
    "revenueGrowthYtdYoyPct": {"unit": "percent", "basis": "cumulative revenue through the monthly feed's month vs the same months last year"},
    "netMarginYtdPct": {"formula": "net_income_ytd / revenue_ytd * 100", "unit": "percent", "basis": "same cumulative period for both"},
}


def _round(o):
    if isinstance(o, float):
        return round(o, 6)
    if isinstance(o, dict):
        return {k: _round(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v) for v in o]
    return o


def source_fetched_at(res: BuildResult) -> Optional[str]:
    """Latest moment any official source file behind this snapshot was fetched (derived from the raw files, so it is deterministic)."""
    times = [v["fetchedAt"] for v in res.source_versions["raw"].values() if v.get("fetchedAt")]
    return max(times) if times else None


def make_documents(res: BuildResult, generated_at: str) -> tuple[dict, dict]:
    versions = {"pipeline": PIPELINE_VERSION, "snapshotSchema": SNAPSHOT_SCHEMA_VERSION,
                "raw": {k: {"sha256": v["sha256"], "fetchedAt": v["fetchedAt"], "rows": v["rows"]} for k, v in sorted(res.source_versions["raw"].items())}}
    common = {"schemaVersion": SNAPSHOT_SCHEMA_VERSION, "generatedAt": generated_at, "sourceFetchedAt": source_fetched_at(res),
              "marketAsOf": res.market_as_of, "financialAsOf": res.financial_as_of, "sourceVersions": versions}
    market = {**common, "kind": "market-snapshot", "basisFinding": res.findings, "metricDefinitions": METRIC_DEFINITIONS,
              "coverage": res.coverage, "warnings": res.warnings, "companies": res.companies}
    industry = {**common, "kind": "industry-snapshots",
                "industryIndex": {s.industryCode: s.industryName for s in res.industry_snapshots},
                "series": {"OFFICIAL_PE": "exchange-published P/E (primary)", "CALCULATED_PE_ANNUALIZED_YTD": "run-rate P/E from cumulative YTD EPS (comparison)"},
                "snapshots": [asdict(s) for s in res.industry_snapshots]}
    return _round(market), _round(industry)


def dumps(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False).encode("utf-8")


def strip_runtime(doc):
    """Remove runtime-only metadata (generatedAt) so two builds of the same raw data can be compared."""
    if isinstance(doc, dict):
        return {k: strip_runtime(v) for k, v in doc.items() if k != "generatedAt"}
    if isinstance(doc, list):
        return [strip_runtime(v) for v in doc]
    return doc


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def source_hash(file_hashes: dict) -> str:
    """One identity for the pair of files: sha256 of the two file hashes concatenated in a fixed order."""
    return sha("".join(file_hashes[n] for n in FILES).encode("ascii"))


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class PublishRefused(Exception):
    def __init__(self, problems, gate=None):
        self.problems = problems
        self.gate = gate
        super().__init__("; ".join(problems[:5]))


def read_manifest(out_root: Path) -> Optional[dict]:
    p = Path(out_root) / "manifest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def publish(res: BuildResult, out_root: Path, generated_at: str, published_at: str, now: datetime,
            closures: frozenset = frozenset(), allow_mixed_dates: bool = False, allow_republish: bool = False) -> tuple[dict, str]:
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
    # Same DATA again (only the build time differs) is not a new release: nothing is written and `latest` does not move.
    if previous_market and previous_industry and dumps(strip_runtime(market)) == dumps(strip_runtime(previous_market)) \
            and dumps(strip_runtime(industry)) == dumps(strip_runtime(previous_industry)):
        return previous, "UNCHANGED"
    gate = evaluate_gate(market, industry, previous, now, closures, allow_mixed_dates, previous_market, allow_republish)
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

    weekly = weekly_metadata(published_at)
    verdict = classify({**weekly, "marketAsOf": market["marketAsOf"], "financialAsOf": market["financialAsOf"]}, now, closures)
    manifest = {
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION, "release": release, "runDate": res.run_date, "generatedAt": generated_at,
        "sourceFetchedAt": market["sourceFetchedAt"], "publishedAt": published_at, **weekly,
        "marketAsOf": market["marketAsOf"], "financialAsOf": market["financialAsOf"], "sourceHash": full_hash,
        "companyCount": len(market["companies"]),
        "files": {n: {"sha256": hashes[n], "bytes": len(blobs[n]), "path": f"releases/{release}/{n}"} for n in FILES},
        "freshnessAtPublish": {k: verdict[k] for k in ("status", "missedWeeklyUpdates", "marketAgeTradingDays", "expectedLatestTradeDate", "calendarNote")},
        "gate": gate.to_json(),
    }
    write_atomic(rel_dir / "release.json", dumps(manifest))
    write_atomic(out_root / "manifest.json", dumps(manifest))  # LAST: the commit point
    prune_releases(out_root, keep=KEEP_RELEASES, current=release, protect=(previous or {}).get("release"))
    return manifest, "PUBLISHED"



def _published_key(p: Path):
    try:
        return json.loads((p / "release.json").read_text(encoding="utf-8")).get("publishedAt") or ""
    except (OSError, json.JSONDecodeError):
        return ""


def prune_releases(out_root: Path, keep: int, current: str, protect: Optional[str] = None) -> None:
    d = Path(out_root) / "releases"
    if not d.exists():
        return
    dirs = sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: (_published_key(p), p.stat().st_mtime), reverse=True)
    for p in dirs[keep:]:
        if p.name not in (current, protect):
            for f in p.iterdir():
                f.unlink()
            p.rmdir()


def rollback(out_root: Path, release: str, rolled_back_at: str) -> dict:
    """Re-point manifest.json at an earlier release after re-verifying its files. Nothing else changes."""
    out_root = Path(out_root)
    rel_dir = out_root / "releases" / release
    meta = json.loads((rel_dir / "release.json").read_text(encoding="utf-8"))
    for n in FILES:
        if sha((rel_dir / n).read_bytes()) != meta["files"][n]["sha256"]:
            raise PublishRefused([f"hash: release {release} file {n} does not match its recorded hash; rollback refused"])
    meta = {**meta, "rolledBackAt": rolled_back_at}
    write_atomic(out_root / "manifest.json", dumps(meta))
    return meta
