"""One DAILY run of the TWSE / TPEx publication (Phase 3I.2). Coexists with the weekly runner.py (untouched); only the CLI entry differs.

    precheck (0 requests when no market owes anything: NOOP_ALREADY_PUBLISHED / NO_TRADING_DAY_EXPECTED)
      -> fetch ONLY the markets that owe a newer date (the others are carried from the Last Known Good's raw partition)
      -> each market's actual source date vs its own target (mitw.daily.evaluate)
      -> pure build() -> daily gate -> atomic release, manifest last
Every outcome is recorded (run record + health.json) with the observability fields of the brief. A failed attempt never touches the Last Known Good.
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import daily as D
from .config import OUT_DIR, RAW_DIR, ROOT, RUNS_DIR
from .daily_ingest import failures_by_market, ingest_markets, prune_raw
from .ingest import next_partition, partition_market_dates
from .pipeline import build, load_inputs
from .providers.http import fetch_endpoint, new_run, now_taipei
from .raw.store import RawStore
from .runlog import RunRecord, load_runs, record, restore_from_health, write_health
from .snapshot.build import PublishRefused, read_manifest
from .snapshot.daily_publish import DailyContext, publish_daily
from .trading_calendar import TradingCalendar, from_twse_holiday_schedule, load_manual_closures

MANUAL_CLOSURES = ROOT / "config" / "manual_closures.json"
HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
NO_HEALTH_CHURN = (D.NOOP_ALREADY_PUBLISHED, D.NO_TRADING_DAY)  # zero-request outcomes do not rewrite health.json (no Pages churn)


def _iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def load_calendar(rows: Optional[list], previous: Optional[dict], manual_path: Path = MANUAL_CLOSURES, fetched_at: str = "") -> tuple:
    """Official holiday list (rows) + the explicit manual closures. If the official list could not be read, the calendar the Last Known Good published
    is used; if there is none either, the calendar is UNKNOWN (never 'everything trades'). Returns (calendar, source)."""
    manual = load_manual_closures(manual_path)
    if rows:
        return from_twse_holiday_schedule(rows, fetched_at, manual=manual), "OFFICIAL_FETCHED"
    cal_json = (((previous or {}).get("publicationPolicy") or {}).get("calendar"))
    if cal_json:
        base = TradingCalendar.from_json(cal_json)
        return TradingCalendar(base.closed, base.covered_from, base.covered_to, tuple(manual) or base.manual, base.source, base.fetched_at), "LAST_KNOWN_GOOD_CALENDAR"
    return from_twse_holiday_schedule([], fetched_at, manual=manual), "UNKNOWN"


def fetch_holiday_rows(get=None) -> Optional[list]:
    try:
        import requests
        r = (get or (lambda u: requests.get(u, timeout=30, headers={"User-Agent": "yichuang-market-data calendar (read-only)"})))(HOLIDAY_URL)
        rows = json.loads(r.content.decode("utf-8-sig"))
        return rows if isinstance(rows, list) and rows else None
    except Exception:
        return None


def _published_dates(previous: Optional[dict]) -> dict:
    mad = (previous or {}).get("marketAsOf") or {}
    return {m: D.parse_market_date(mad.get(m)) for m in D.MAIN_MARKETS}


def _market_obs(m: str, fail: dict, dates: dict) -> D.Obs:
    if m in fail:
        return D.Obs("ERROR", None, fail[m][:300])
    d = D.parse_market_date(dates.get(m))
    return D.Obs("OK", d) if d else D.Obs("MALFORMED", None, f"no readable trade date for {m}")


def observability(started: datetime, attempt: str, trigger: str, published: dict, targets: dict, observed: dict, queried: tuple, carried: tuple, fallbacks: tuple,
                  decision: str, reason: str, release: Optional[str], company_count: Optional[int], source_hash: Optional[str], requests: int, reused: int,
                  sources: dict, manifest_updated: bool, previous_release: Optional[str], dates_now: dict) -> dict:
    return {
        "businessTimeTaipei": _iso(started), "attemptType": attempt, "trigger": trigger, "decision": decision, "reason": reason,
        "markets": {m: {"queried": m in queried, "carried": m in carried and m not in fallbacks, "carryFallbackFetched": m in fallbacks,
                        "expectedMarketDate": targets.get(m), "actualSourceMarketDate": (observed.get(m).date.isoformat() if observed.get(m) and observed[m].date else
                                                                                           (dates_now.get(m).isoformat() if dates_now.get(m) else None)),
                        "previousPublishedMarketDate": published[m].isoformat() if published.get(m) else None,
                        "availabilityBasis": D.AVAILABILITY[m].basis, "evidenceLevel": D.EVIDENCE_LEVEL} for m in D.MAIN_MARKETS},
        "releaseId": release, "companyCount": company_count, "sourceHash": source_hash,
        "sourceCounts": {"requests": requests, "reusedOrCarriedFiles": reused, "rows": {eid: (v or {}).get("rows") for eid, v in sources.items() if v.get("status") != "FAILED"}},
        "publicVerification": "PENDING_PUBLIC_READBACK" if manifest_updated else "NOT_APPLICABLE",
        "manifestUpdated": manifest_updated,
        "lastKnownGoodPreserved": True if not manifest_updated else bool(previous_release),
        "lastKnownGoodRelease": previous_release,
    }


def daily_health_block(runs: list, manifest: Optional[dict], now: datetime) -> dict:
    cal = TradingCalendar.from_json(((manifest or {}).get("publicationPolicy") or {}).get("calendar") or {}) if manifest else None
    verdicts = {}
    if manifest and cal:
        for m in D.MAIN_MARKETS:
            v = D.classify_market(m, D.parse_market_date(((manifest.get("marketAsOf") or {}).get(m))), now, cal)
            verdicts[m] = {k: v[k] for k in ("status", "expectedMarketDate", "lagTradingDays", "updatePending", "reason")}
    return {"updatePolicy": D.POLICY, "timezone": D.TIMEZONE_NAME, "attempts": {"TWSE": ["06:30"], "TPEX": [t.strftime("%H:%M") for t in D.ATTEMPT_TIMES]},
            "lastDecision": ((runs[-1].get("publication") or {}).get("daily") if runs else None), "lastSuccessfulPublication": (manifest or {}).get("lastSuccessfulPublication"),
            "marketAsOf": (manifest or {}).get("marketAsOf"), "freshnessWhenWritten": verdicts}


def run_daily(trigger: str = "manual", now_fn: Callable[[], datetime] = now_taipei, calendar_rows: Optional[list] = None, manual_closures_path: Path = MANUAL_CLOSURES,
              raw_dir: Path = RAW_DIR, out_dir: Path = OUT_DIR, runs_dir: Path = RUNS_DIR, fetch=fetch_endpoint, allow_republish: bool = False,
              force_markets: tuple = ()) -> RunRecord:
    started = D.to_taipei(now_fn())
    restore_from_health(out_dir, runs_dir)
    rec = RunRecord(runId=started.strftime("%Y%m%dT%H%M%S"), startedAt=_iso(started), runDate=started.date().isoformat(), trigger=trigger)
    previous = read_manifest(out_dir)
    previous_release = (previous or {}).get("release")
    attempt = D.attempt_label(started)
    try:
        cal, cal_source = load_calendar(calendar_rows, previous, manual_closures_path, _iso(started))
        published = _published_dates(previous)
        pre = D.precheck(started, published, cal, D.MAIN_MARKETS)
        rec.marketDates = {m: (d.isoformat() if d else None) for m, d in published.items()}
        rec.expectedLatestTradeDate = max((t for t in pre.targets.values() if t), default=None)
        owed = tuple(pre.owed) or ()
        if force_markets:  # a person asked for a specific market (manual dispatch): still judged against its own target
            owed = tuple(sorted(set(owed) | set(force_markets), key=D.MAIN_MARKETS.index))
        if not pre.fetch and not owed:
            rec.status = pre.state
            rec.publication = {"result": pre.state, "release": previous_release, "gate": None,
                               "daily": observability(started, attempt, trigger, published, pre.targets, {}, (), D.MAIN_MARKETS, (), pre.state, pre.reason, previous_release,
                                                      (previous or {}).get("companyCount"), (previous or {}).get("sourceHash"), 0, 0, {}, False, previous_release, {})}
            rec.failureReason = None
            return _finish(rec, started, runs_dir, out_dir, previous, write_health_file=False)

        partition = next_partition(raw_dir, rec.runDate)
        rec.partition = partition
        ctx = new_run(partition)
        carry = {m: partition_of(previous, m) for m in D.MAIN_MARKETS if m not in owed}
        result, fallbacks = ingest_markets(partition, raw_dir, ctx, owed, carry, fetch)
        rec.requests, rec.reused = ctx.request_count, ctx.reused_today + ctx.reused_recent
        rec.sources = {eid: {"status": "REUSED" if (ref.reusedToday or ref.meta.reusedFrom) else "FETCHED", "httpStatus": ref.meta.httpStatus, "rows": ref.meta.rows,
                             "fetchedAt": ref.meta.fetchedAt, "reusedFrom": ref.meta.reusedFrom} for eid, ref in result.fetched.items()}
        for eid, why in result.failures.items():
            rec.sources[eid] = {"status": "FAILED", "error": why[:300]}
        rec.schemaValidation = {"ok": not result.schema_problems, "problems": [f"{k}: {v[:2]}" for k, v in result.schema_problems.items()]}
        fail = failures_by_market(result)
        try:
            dates = partition_market_dates(raw_dir, partition)
        except Exception:  # a prices file that could not be read: that market's observation becomes ERROR / MALFORMED below
            dates = {m: None for m in D.MAIN_MARKETS}
        rec.marketDates = {m: dates.get(m) for m in D.MAIN_MARKETS}
        queried = tuple(sorted(set(owed) | set(fallbacks), key=D.MAIN_MARKETS.index))
        carried = tuple(m for m in D.MAIN_MARKETS if m not in owed)
        observed = {m: _market_obs(m, fail, dates) for m in queried}
        final = D.is_final_attempt(started, trigger)
        dec = D.evaluate(started, published, observed, cal, D.MAIN_MARKETS, final)
        obs_args = dict(started=started, attempt=attempt, trigger=trigger, published=published, targets=dec.targets, observed=observed, queried=queried, carried=carried,
                        fallbacks=fallbacks, decision=dec.state, reason=dec.reason, requests=rec.requests, reused=rec.reused, sources=rec.sources,
                        previous_release=previous_release, dates_now={m: D.parse_market_date(dates.get(m)) for m in D.MAIN_MARKETS})
        rec.status, rec.failureReason = dec.state, (dec.reason if dec.failed or dec.state in (D.WAITING, D.SUCCESS_PARTIAL) else None)
        rec.publication = {"result": dec.state, "release": None, "gate": None,
                           "daily": observability(**obs_args, release=None, company_count=None, source_hash=None, manifest_updated=False)}
        if not dec.publish:
            return _finish(rec, started, runs_dir, out_dir, previous, write_health_file=True, raw_dir=raw_dir)

        inp = load_inputs(RawStore(raw_dir), partition)
        generated_at = _iso(started)
        res = build(inp, generated_at)
        dctx = DailyContext(dec, cal, {m: partition for m in D.MAIN_MARKETS}, carried, fallbacks, attempt, trigger)
        try:
            manifest, outcome = publish_daily(res, out_dir, generated_at, _iso(started), started, dctx, allow_republish)
        except PublishRefused as e:
            rec.status, rec.failureReason = D.FAILED_VALIDATION, "; ".join(e.problems[:4])
            rec.publication = {"result": "REFUSED", "release": None, "gate": e.gate.to_json() if e.gate else None,
                               "daily": observability(**{**obs_args, "decision": D.FAILED_VALIDATION, "reason": rec.failureReason}, release=None, company_count=None, source_hash=None, manifest_updated=False)}
            return _finish(rec, started, runs_dir, out_dir, previous, write_health_file=True, raw_dir=raw_dir)
        if outcome == "UNCHANGED":
            rec.status, rec.failureReason = D.NOOP_ALREADY_PUBLISHED, None
            rec.publication = {"result": D.NOOP_ALREADY_PUBLISHED, "release": previous_release, "gate": None,
                               "daily": observability(**{**obs_args, "decision": D.NOOP_ALREADY_PUBLISHED, "reason": "identical data to the Last Known Good"}, release=previous_release,
                                                      company_count=(previous or {}).get("companyCount"), source_hash=(previous or {}).get("sourceHash"), manifest_updated=False)}
            return _finish(rec, started, runs_dir, out_dir, previous, write_health_file=False)
        rec.publication = {"result": "PUBLISHED", "release": manifest["release"], "gate": manifest.get("gate"),
                           "daily": observability(**obs_args, release=manifest["release"], company_count=manifest["companyCount"], source_hash=manifest["sourceHash"], manifest_updated=True)}
    except Exception as e:  # any unexpected problem must still leave a record and leave the Last Known Good untouched
        rec.status = D.FAILED_SOURCE if "ERROR" in type(e).__name__.upper() or isinstance(e, OSError) else D.FAILED_VALIDATION
        rec.failureReason = f"{type(e).__name__}: {e}"[:300]
        rec.publication = {"result": "ERROR", "release": None, "gate": {"traceback": traceback.format_exc()[-600:]}, "daily": {"decision": rec.status, "reason": rec.failureReason, "manifestUpdated": False, "lastKnownGoodPreserved": True}}
    return _finish(rec, started, runs_dir, out_dir, previous, write_health_file=True, raw_dir=raw_dir)


def partition_of(previous: Optional[dict], market: str) -> Optional[str]:
    return (((previous or {}).get("markets") or {}).get(market) or {}).get("rawPartition")


def _finish(rec: RunRecord, started: datetime, runs_dir: Path, out_dir: Path, previous: Optional[dict], write_health_file: bool, raw_dir: Optional[Path] = None) -> RunRecord:
    rec.finishedAt = _iso(now_taipei())
    if raw_dir is not None:
        named = {v for v in (partition_of(read_manifest(out_dir), m) for m in D.MAIN_MARKETS) if v}
        prune_raw(raw_dir, 4, named | {rec.partition})
    record(rec, runs_dir)
    if write_health_file:
        manifest = read_manifest(out_dir)
        write_health(rec.finishedAt, runs_dir, out_dir, daily=daily_health_block(load_runs(runs_dir), manifest, started))
    return rec
