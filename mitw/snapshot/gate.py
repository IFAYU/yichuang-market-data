"""Production publication gate. A candidate becomes `latest` only if EVERY check passes; otherwise latest is left exactly as it was.

  schema        both documents carry the supported schemaVersion and agree with each other
  coverage      the existing structural / coverage / statistics checks (snapshot/validate.py)
  methodology   industry statistics carry an explicit outlier policy + fences; the official P/E basis is the exchanges' TTM definition
  market-date   both markets have a parseable trade date, and the dates are the SAME day (no mixed-date "latest")
  not-older     neither market's date is older than the currently published one (a publication can never move backwards in time)
  advances      (weekly policy) the market dates are NEWER than the published ones: a run that only re-fetched old data is
                NO_NEW_MARKET_DATA, not a publication. Explicit recorded exception: REPUBLISH_SAME_MARKET_DATE (maintainer re-build)
  freshness     the freshness of the candidate can be determined (not UNKNOWN)

(The sha256 check is made by the publisher after the files are written: it re-reads them and compares with the manifest.)
Mixed dates can be allowed only by an explicit, recorded exception (used once, to migrate the snapshot that was published before
this gate existed); the exception is written into the manifest and shown in the app.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from ..contracts.version import SNAPSHOT_SCHEMA_VERSION
from ..freshness import UNKNOWN, classify, parse_date, weekly_metadata
from .validate import validate_documents

SUPPORTED_MAJOR = int(SNAPSHOT_SCHEMA_VERSION.split(".")[0])


@dataclass
class GateResult:
    checks: list = field(default_factory=list)  # [{name, ok, detail}]
    exceptions: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c["ok"] for c in self.checks)

    @property
    def failures(self) -> list:
        return [f'{c["name"]}: {c["detail"]}' for c in self.checks if not c["ok"]]

    def to_json(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "exceptions": self.exceptions}


def _add(r: GateResult, name: str, ok: bool, detail: str = "") -> None:
    r.checks.append({"name": name, "ok": bool(ok), "detail": detail or ("ok" if ok else "failed")})


def evaluate_gate(market: dict, industry: dict, previous_manifest: Optional[dict], now: datetime, closures: frozenset = frozenset(),
                  allow_mixed_dates: bool = False, previous_market: Optional[dict] = None, allow_republish: bool = False) -> GateResult:
    r = GateResult()

    # schema
    versions = {market.get("schemaVersion"), industry.get("schemaVersion")}
    major_ok = all(isinstance(v, str) and v.split(".")[0].isdigit() and int(v.split(".")[0]) == SUPPORTED_MAJOR for v in versions)
    _add(r, "schema", major_ok and len(versions) == 1, f"schemaVersion {sorted(map(str, versions))}, supported {SUPPORTED_MAJOR}.x")

    # coverage / structure / statistics consistency
    problems = validate_documents(market, industry, previous_market)
    _add(r, "coverage", not problems, "; ".join(problems[:4]) if problems else "coverage and statistics consistent")

    # methodology
    official = [s for s in industry.get("snapshots", []) if s.get("metricSeries") == "OFFICIAL_PE" and not s.get("subIndustryCode")]
    meth_ok = bool(official) and all(
        isinstance(s.get("methodology", {}).get("outlierPolicy"), dict) and "robustFences" in s["methodology"] and s["methodology"].get("percentile")
        for s in official)
    basis_ok = (market.get("basisFinding") or {}).get("officialPEBasis") == "OFFICIAL_TTM"
    _add(r, "methodology", meth_ok and basis_ok,
         "outlier policy, fences and percentile definition present; official P/E basis OFFICIAL_TTM" if meth_ok and basis_ok else
         f"outlierPolicyPresent={meth_ok} officialPEBasisIsOfficialTTM={basis_ok}")

    # market dates
    mad = market.get("marketAsOf") or {}
    parsed = {m: parse_date(mad.get(m)) for m in ("TWSE", "TPEX")}
    present = all(parsed.values())
    _add(r, "market-date-present", present, f"marketAsOf={mad}" if present else f"a market date is missing or unparseable: {mad}")
    same = present and len({d for d in parsed.values()}) == 1
    if present and not same:
        if allow_mixed_dates:
            r.exceptions.append({"code": "MIXED_MARKET_DATES", "detail": f"TWSE {mad.get('TWSE')} vs TPEx {mad.get('TPEX')}", "allowedExplicitly": True})
            _add(r, "market-date-same", True, f"EXCEPTION: mixed dates allowed explicitly ({mad})")
        else:
            _add(r, "market-date-same", False, f"TWSE {mad.get('TWSE')} and TPEx {mad.get('TPEX')} are different days: one exchange has not published yet, so this is not published as latest")
    else:
        _add(r, "market-date-same", same, "both markets are on the same trade date" if same else "cannot compare")

    # never move backwards
    prev_mad = (previous_manifest or {}).get("marketAsOf") or {}
    if previous_manifest and present:
        older = [m for m in ("TWSE", "TPEX") if parse_date(prev_mad.get(m)) and parsed[m] < parse_date(prev_mad[m])]
        _add(r, "not-older-than-latest", not older, f"{older} would move backwards (latest {prev_mad})" if older else f"not older than latest {prev_mad}")
    else:
        _add(r, "not-older-than-latest", True, "no previous publication to compare with")

    # "the schedule ran" is not "the data moved": a weekly publication must carry a NEWER market date than the one it replaces
    if previous_manifest and present:
        stuck = [m for m in ("TWSE", "TPEX") if parse_date(prev_mad.get(m)) and parsed[m] <= parse_date(prev_mad[m])]
        if stuck and allow_republish:
            r.exceptions.append({"code": "REPUBLISH_SAME_MARKET_DATE", "detail": f"{stuck} not newer than {prev_mad}", "allowedExplicitly": True})
            _add(r, "market-date-advances", True, f"EXCEPTION: re-publication of the same market date allowed explicitly ({mad})")
        else:
            _add(r, "market-date-advances", not stuck,
                 f"NO_NEW_MARKET_DATA: {stuck} did not move past the published {prev_mad}" if stuck else f"newer than the published {prev_mad}")
    else:
        _add(r, "market-date-advances", True, "no previous publication to compare with")

    # freshness determinable (weekly, schedule-based; judged as of the moment of publication)
    verdict = classify({**weekly_metadata(now.isoformat(timespec="seconds")), "marketAsOf": mad, "financialAsOf": market.get("financialAsOf") or {}}, now, closures)
    _add(r, "freshness-determinable", verdict["status"] != UNKNOWN,
         f"candidate would be {verdict['status']} (market age {verdict.get('marketAgeTradingDays')} trading days, informational)" if verdict["status"] != UNKNOWN else f"UNKNOWN: {verdict.get('reason')}")
    return r
