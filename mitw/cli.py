from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .config import DATA_DIR, OUT_DIR, RUNS_DIR
from .ingest import ingest
from .providers.http import now_taipei


def _closures(args) -> frozenset:
    return frozenset(date.fromisoformat(d) for d in (getattr(args, "closure", None) or []))


def cmd_fetch(args) -> int:
    """Manual one-shot raw fetch (the scheduled path is `run`)."""
    res = ingest(args.stage, run_date=args.date, only=args.only)
    runs = DATA_DIR / "fetch-logs"
    runs.mkdir(parents=True, exist_ok=True)
    out = {"runDate": res.ctx.run_date, "requests": res.ctx.request_count, "reusedToday": res.ctx.reused_today, "notModified": res.ctx.not_modified,
           "failures": res.failures, "events": res.ctx.events}
    (runs / f"{res.ctx.run_id}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if res.ok else 2


def _print_run(rec) -> int:
    print(json.dumps({"status": rec.status, "runId": rec.runId, "partition": rec.partition, "requests": rec.requests, "reused": rec.reused,
                      "marketDates": rec.marketDates, "expectedLatestTradeDate": rec.expectedLatestTradeDate,
                      "publication": {k: v for k, v in rec.publication.items() if k != "gate"}, "gate": (rec.publication.get("gate") or {}).get("checks"),
                      "failureReason": rec.failureReason}, ensure_ascii=False, indent=2))
    return 0 if rec.status in ("SUCCESS", "NOOP", "WAITING", "SKIPPED") else 3


def cmd_run(args) -> int:
    """The scheduled entry point: at most a handful of requests, publishes only through the gate, always leaves a record."""
    from .runner import run_once
    return _print_run(run_once(trigger=args.trigger, closures=_closures(args), allow_mixed_dates=args.allow_mixed_dates, allow_republish=args.republish))


def cmd_build(args) -> int:
    """Rebuild + republish from a raw partition already on disk (no network). Same gate as `run`."""
    from .runner import publish_existing
    partition = args.date or now_taipei().date().isoformat()
    return _print_run(publish_existing(partition, closures=_closures(args), allow_mixed_dates=args.allow_mixed_dates, allow_republish=args.republish))


def cmd_sync(args) -> int:
    from .sync import SyncRefused, sync
    try:
        manifest = sync(Path(args.source), Path(args.target))
    except SyncRefused as e:
        print(json.dumps({"status": "REFUSED", "problems": e.problems, "note": "previous valid snapshot in the target was left untouched"}, ensure_ascii=False, indent=2))
        return 4
    print(json.dumps({"status": "SYNCED", "manifest": manifest}, ensure_ascii=False, indent=2))
    return 0


def cmd_health(args) -> int:
    from .runlog import load_runs, summarize
    s = summarize(load_runs(RUNS_DIR))
    print(json.dumps({k: v for k, v in s.items() if k != "runs"}, ensure_ascii=False, indent=2))
    print(f"retained runs: {len(s['runs'])}")
    return 0


def cmd_rollback(args) -> int:
    from .snapshot.build import PublishRefused, rollback
    try:
        m = rollback(Path(args.out), args.release, now_taipei().isoformat(timespec="seconds"))
    except PublishRefused as e:
        print(json.dumps({"status": "REFUSED", "problems": e.problems}, ensure_ascii=False))
        return 5
    print(json.dumps({"status": "ROLLED_BACK", "release": m["release"], "marketAsOf": m["marketAsOf"]}, ensure_ascii=False))
    return 0


def cmd_verify_remote(args) -> int:
    """Poll the public site until a release file is served with the hash the manifest records (or give up)."""
    from .remote import verify_release
    ok, detail = verify_release(args.base, Path(args.out), args.release, timeout_s=args.timeout)
    print(json.dumps({"status": "READABLE" if ok else "NOT_READABLE", "detail": detail}, ensure_ascii=False))
    return 0 if ok else 6


def cmd_mark_failed(args) -> int:
    from .runlog import mark_last_run_failed
    last = mark_last_run_failed(args.reason, now_taipei().isoformat(timespec="seconds"))
    print(json.dumps({"status": "MARKED" if last else "NO_RUN", "runId": (last or {}).get("runId")}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mitw")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="manual one-shot raw fetch (never refetches a dataset already fetched today)")
    f.add_argument("--stage", choices=["core", "full"], default="core")
    f.add_argument("--only", nargs="+", default=None)
    f.add_argument("--date", default=None)
    r = sub.add_parser("run", help="scheduled run: expected date? -> fetch -> build -> gate -> publish; always writes a run record")
    r.add_argument("--trigger", choices=["manual", "schedule"], default="manual")
    r.add_argument("--closure", nargs="*", default=None, help="extra market closure dates YYYY-MM-DD (holidays are not modelled otherwise)")
    r.add_argument("--allow-mixed-dates", action="store_true", help="explicit, recorded exception: publish although the two exchanges are on different dates")
    r.add_argument("--republish", action="store_true", help="explicit, recorded exception: publish although the market date did not move past the published one")
    b = sub.add_parser("build", help="rebuild + republish from a raw partition on disk (no network), through the same gate")
    b.add_argument("--date", default=None, help="raw partition, e.g. 2026-10-06 or 2026-10-06.2")
    b.add_argument("--closure", nargs="*", default=None)
    b.add_argument("--allow-mixed-dates", action="store_true")
    b.add_argument("--republish", action="store_true", help="explicit, recorded exception: same market date as the published release")
    sy = sub.add_parser("sync", help="validate the published snapshot and copy it into an app's static folder")
    sy.add_argument("--source", default=str(OUT_DIR))
    sy.add_argument("--target", required=True)
    sub.add_parser("health", help="print last success / last failure / consecutive failures")
    rb = sub.add_parser("rollback", help="re-point manifest.json at an earlier release")
    rb.add_argument("release")
    rb.add_argument("--out", default=str(OUT_DIR))
    vr = sub.add_parser("verify-remote", help="wait until the public site serves a release file with the recorded sha256")
    vr.add_argument("--base", required=True)
    vr.add_argument("--release", required=True)
    vr.add_argument("--out", default=str(OUT_DIR))
    vr.add_argument("--timeout", type=int, default=900)
    mf = sub.add_parser("mark-failed", help="rewrite the last run record as FAILED (release not readable from the public site)")
    mf.add_argument("--reason", required=True)
    args = ap.parse_args(argv)
    return {"fetch": cmd_fetch, "run": cmd_run, "build": cmd_build, "sync": cmd_sync, "health": cmd_health, "rollback": cmd_rollback,
            "verify-remote": cmd_verify_remote, "mark-failed": cmd_mark_failed}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
