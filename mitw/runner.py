"""One scheduled ingestion run, end to end, with a record of what happened.

    this week's slot (Sunday 10:00) ──▶ already published since it? ──▶ NOOP (0 requests)
                                   │ no
                          3 attempts since the slot? ──▶ SKIPPED
                                   │ no
        fetch (prices + ratios only; slow-moving datasets are copied from recent raw) ──▶ FAILED on any source / schema problem
                                   │
        build ──▶ both exchanges on the same, NEWER trade date? ──no──▶ WAITING (try again later; on the LAST attempt of the
                                   │ yes                                   week it becomes FAILED, "NO_NEW_MARKET_DATA")
                                   │ yes
        publication gate ──fail──▶ REFUSED (latest untouched)
                                   │ pass
        write release ▸ verify ▸ replace manifest.json last ──▶ SUCCESS

Every outcome is recorded (data/runs/, last 30) and out/health.json is rebuilt, so "last success / last failure / consecutive
failures" are always answerable. The previous latest snapshot is never touched unless a new one passes the whole gate.
"""
from __future__ import annotations

import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .config import DB_PATH, MAX_ATTEMPTS_PER_SLOT, OUT_DIR, RAW_DIR, RUNS_DIR
from .freshness import expected_latest_trade_date, parse_date, parse_instant, slot_at_or_before
from .ingest import ingest_daily, list_partitions, next_partition, partition_market_dates
from .pipeline import build, load_inputs
from .providers.http import fetch_endpoint, new_run, now_taipei
from .raw.store import RawStore
from .runlog import RunRecord, load_runs, record, restore_from_health, write_health
from .snapshot.build import PublishRefused, publish, read_manifest

COUNTED = ("SUCCESS", "WAITING", "FAILED", "REFUSED")  # NOOP and SKIPPED do not use up the day's attempts


def _iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def _finish(rec: RunRecord, now: Callable[[], datetime], runs_dir: Path, out_dir: Path) -> RunRecord:
    rec.finishedAt = _iso(now())
    record(rec, runs_dir)
    write_health(rec.finishedAt, runs_dir, out_dir)
    return rec


def _not_yet(rec: RunRecord, final_attempt: bool, reason: str, result: str) -> RunRecord:
    """Nothing publishable yet. Early in the week's window that is WAITING (retry later); on the last attempt it is this week's failure."""
    rec.status = "FAILED" if final_attempt else "WAITING"
    rec.failureReason = reason + (" (last attempt of this week's window: this week's publication has FAILED, the previous release stays)" if final_attempt else "")
    rec.publication = {"result": result, "release": None, "gate": None}
    return rec


def publish_partition(partition: str, rec: RunRecord, now: datetime, raw_dir: Path = RAW_DIR, out_dir: Path = OUT_DIR,
                      closures: frozenset = frozenset(), allow_mixed_dates: bool = False, db_path: Path | None = DB_PATH,
                      final_attempt: bool = False, allow_republish: bool = False) -> RunRecord:
    """Build from an existing raw partition and publish through the gate. Fills rec.status / marketDates / publication."""
    previous = read_manifest(out_dir)
    store = RawStore(raw_dir)
    inp = load_inputs(store, partition)
    generated_at = _iso(now)
    res = build(inp, generated_at)
    rec.marketDates = dict(res.market_as_of)
    dates = {m: parse_date(d) for m, d in res.market_as_of.items()}
    prev_dates = {m: parse_date(d) for m, d in ((previous or {}).get("marketAsOf") or {}).items()}

    if all(dates.values()) and len(set(dates.values())) > 1 and not allow_mixed_dates:
        return _not_yet(rec, final_attempt, f"the exchanges are on different trade dates {res.market_as_of}: one has not published yet; nothing was published", "WAITING")
    if previous and all(dates.values()) and any(prev_dates.get(m) and dates[m] <= prev_dates[m] for m in dates) and not allow_republish:
        # a schedule that ran is not data that moved
        return _not_yet(rec, final_attempt, f"NO_NEW_MARKET_DATA: no newer trade date than the published one (published {previous.get('marketAsOf')}, fetched {res.market_as_of})", "NO_NEW_MARKET_DATA")

    try:
        manifest, outcome = publish(res, out_dir, generated_at, _iso(now), now, closures, allow_mixed_dates, allow_republish)
    except PublishRefused as e:
        rec.status = "REFUSED"
        rec.failureReason = "; ".join(e.problems[:4])
        rec.publication = {"result": "REFUSED", "release": None, "gate": e.gate.to_json() if e.gate else None}
        return rec
    rec.status = "SUCCESS" if outcome == "PUBLISHED" else "NOOP"
    rec.publication = {"result": outcome, "release": manifest["release"], "gate": manifest.get("gate")}
    if outcome == "PUBLISHED" and db_path is not None:
        from .store.sqlite import connect, persist_build
        persist_build(connect(Path(db_path)), inp, res)
    return rec


def run_once(trigger: str = "manual", now_fn: Callable[[], datetime] = now_taipei, closures: frozenset = frozenset(),
             raw_dir: Path = RAW_DIR, out_dir: Path = OUT_DIR, runs_dir: Path = RUNS_DIR, fetch=fetch_endpoint,
             allow_mixed_dates: bool = False, db_path: Path | None = DB_PATH, allow_republish: bool = False) -> RunRecord:
    started = now_fn()
    run_date = started.date().isoformat()
    restore_from_health(out_dir, runs_dir)
    rec = RunRecord(runId=started.strftime("%Y%m%dT%H%M%S"), startedAt=_iso(started), runDate=run_date, trigger=trigger)
    try:
        expected = expected_latest_trade_date(started, closures)
        rec.expectedLatestTradeDate = expected.isoformat()
        previous = read_manifest(out_dir)
        slot = slot_at_or_before(started)
        rec.scheduledSlot = slot.isoformat(timespec="seconds")
        published = parse_instant((previous or {}).get("lastSuccessfulPublication") or (previous or {}).get("publishedAt"))
        if previous and published and published >= slot and not allow_republish:
            rec.status, rec.marketDates = "NOOP", dict(previous.get("marketAsOf") or {})
            rec.publication = {"result": "WEEK_ALREADY_PUBLISHED", "release": previous.get("release"), "gate": None}
            rec.failureReason = None
            return _finish(rec, now_fn, runs_dir, out_dir)

        attempts = [r for r in load_runs(runs_dir) if r.get("status") in COUNTED and (parse_instant(r.get("startedAt")) or slot) >= slot]
        if len(attempts) >= MAX_ATTEMPTS_PER_SLOT and trigger != "manual":  # a person pressing 'run' is not the retry loop
            rec.status = "SKIPPED"
            rec.failureReason = f"retry limit reached for this week's slot ({MAX_ATTEMPTS_PER_SLOT} attempts since {rec.scheduledSlot}); next chance is the next scheduled slot"
            return _finish(rec, now_fn, runs_dir, out_dir)
        final_attempt = trigger != "manual" and len(attempts) + 1 >= MAX_ATTEMPTS_PER_SLOT  # only the scheduled retries close a week

        partition = next_partition(raw_dir, run_date)
        rec.partition = partition
        ctx = new_run(partition)
        result = ingest_daily(partition, raw_dir, ctx, fetch)
        rec.requests, rec.reused = ctx.request_count, ctx.reused_today + ctx.reused_recent
        rec.sources = {eid: {"status": "REUSED" if (ref.reusedToday or ref.meta.reusedFrom) else "FETCHED", "httpStatus": ref.meta.httpStatus,
                             "rows": ref.meta.rows, "fetchedAt": ref.meta.fetchedAt} for eid, ref in result.fetched.items()}
        for eid, why in result.failures.items():
            rec.sources[eid] = {"status": "FAILED", "error": why[:300]}
        rec.schemaValidation = {"ok": not result.schema_problems, "problems": [f"{k}: {v[:2]}" for k, v in result.schema_problems.items()]}
        if result.failures:
            rec.status = "FAILED"
            rec.failureReason = next(iter(result.failures.values()))[:300]
            return _finish(rec, now_fn, runs_dir, out_dir)
        rec.marketDates = partition_market_dates(raw_dir, partition)
        publish_partition(partition, rec, started, raw_dir, out_dir, closures, allow_mixed_dates, db_path, final_attempt, allow_republish)
    except Exception as e:  # any unexpected problem must still leave a record and leave `latest` untouched
        rec.status = "FAILED"
        rec.failureReason = f"{type(e).__name__}: {e}"[:300]
        rec.publication = {"result": "ERROR", "release": None, "gate": {"traceback": traceback.format_exc()[-600:]}}
    return _finish(rec, now_fn, runs_dir, out_dir)


def publish_existing(partition: str, trigger: str = "manual", now_fn: Callable[[], datetime] = now_taipei, closures: frozenset = frozenset(),
                     raw_dir: Path = RAW_DIR, out_dir: Path = OUT_DIR, runs_dir: Path = RUNS_DIR, allow_mixed_dates: bool = False,
                     db_path: Path | None = DB_PATH, allow_republish: bool = False) -> RunRecord:
    """Rebuild and republish from a raw partition already on disk (no network). Same gate, same record."""
    started = now_fn()
    rec = RunRecord(runId=started.strftime("%Y%m%dT%H%M%S") + "b", startedAt=_iso(started), runDate=started.date().isoformat(), partition=partition, trigger=trigger)
    rec.expectedLatestTradeDate = expected_latest_trade_date(started, closures).isoformat()
    try:
        publish_partition(partition, rec, started, raw_dir, out_dir, closures, allow_mixed_dates, db_path, False, allow_republish)
    except Exception as e:
        rec.status, rec.failureReason = "FAILED", f"{type(e).__name__}: {e}"[:300]
    return _finish(rec, now_fn, runs_dir, out_dir)
