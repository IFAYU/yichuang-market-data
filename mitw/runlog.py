"""Ingestion run records: "last successful update / last failed update / consecutive failures", visible to a person.

Every automatic or manual run writes one JSON record (data/runs/<runId>.json). The most recent MAX_RUN_RECORDS are kept. A compact
health summary (out/health.json) is rebuilt after every run, successful or not, and is what the app's Data Health page reads.
Health is INFORMATION ONLY: the app never uses it to decide whether data is fresh (that comes from the data's own market dates and
the clock), so a failed run that cannot publish a new snapshot can still be reported.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import MAX_RUN_RECORDS, OUT_DIR, RUNS_DIR

# SUCCESS  a new release was published and became latest
# NOOP     this week's scheduled publication already succeeded: zero requests
# WAITING  fetched fine but the market date did not move / an exchange is a day behind: nothing published, retry later (Sun 14:00, 20:00)
# REFUSED  data fetched but the publication gate said no: latest untouched
# FAILED   a source / schema / network problem: latest untouched
# SKIPPED  the retry limit of this week's slot was reached (3 attempts)
STATUSES = ("SUCCESS", "NOOP", "WAITING", "REFUSED", "FAILED", "SKIPPED")
HEALTHY = ("SUCCESS", "NOOP")
PROBLEM = ("FAILED", "REFUSED")


@dataclass
class RunRecord:
    runId: str
    startedAt: str
    finishedAt: str = ""
    runDate: str = ""
    partition: str = ""
    status: str = "FAILED"
    trigger: str = "manual"  # manual | schedule
    requests: int = 0
    reused: int = 0
    sources: dict = field(default_factory=dict)  # endpoint id -> {status, httpStatus, rows, error}
    schemaValidation: dict = field(default_factory=lambda: {"ok": None, "problems": []})
    marketDates: dict = field(default_factory=dict)
    expectedLatestTradeDate: Optional[str] = None
    scheduledSlot: Optional[str] = None  # the weekly slot (Sunday 10:00 Asia/Taipei) this run belongs to
    publication: dict = field(default_factory=lambda: {"result": None, "release": None, "gate": None})
    failureReason: Optional[str] = None

    def to_json(self) -> dict:
        return self.__dict__


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def record(run: RunRecord, runs_dir: Path = RUNS_DIR) -> Path:
    p = Path(runs_dir) / f"{run.runId}.json"
    write_atomic(p, json.dumps(run.to_json(), ensure_ascii=False, indent=1))
    prune(runs_dir)
    return p


def restore_from_health(out_dir: Path = OUT_DIR, runs_dir: Path = RUNS_DIR) -> int:
    """A CI runner starts with an empty data/runs. The attempts already made this week (and the history) are in the published
    health.json, so they are restored from it: otherwise every run would think it is the first attempt of the week."""
    try:
        health = json.loads((Path(out_dir) / "health.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    n = 0
    for c in health.get("runs") or []:
        if not isinstance(c, dict) or not c.get("runId") or not c.get("startedAt"):
            continue
        p = Path(runs_dir) / f"{c['runId']}.json"
        if p.exists():
            continue
        rec = {**c, "publication": {"result": c.get("publication"), "release": c.get("release"), "gate": None}, "restoredFromHealth": True}
        write_atomic(p, json.dumps(rec, ensure_ascii=False, indent=1))
        n += 1
    prune(runs_dir)
    return n


def mark_last_run_failed(reason: str, now_iso: str, runs_dir: Path = RUNS_DIR, out_dir: Path = OUT_DIR) -> Optional[dict]:
    """The pipeline said SUCCESS but the release could not be read back from the public site, so nothing went live: say so."""
    runs = load_runs(runs_dir)
    if not runs:
        return None
    last = runs[-1]
    last["status"], last["failureReason"] = "FAILED", reason[:300]
    last["publication"] = {"result": "REMOTE_NOT_READABLE", "release": (last.get("publication") or {}).get("release"), "gate": None}
    write_atomic(Path(runs_dir) / f"{last['runId']}.json", json.dumps(last, ensure_ascii=False, indent=1))
    write_health(now_iso, runs_dir, out_dir)
    return last


def load_runs(runs_dir: Path = RUNS_DIR) -> list[dict]:
    d = Path(runs_dir)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue  # a corrupt record must never break reporting
    return sorted(out, key=lambda r: r.get("startedAt", ""))


def prune(runs_dir: Path = RUNS_DIR, keep: int = MAX_RUN_RECORDS) -> None:
    files = sorted(Path(runs_dir).glob("*.json"))
    for f in files[:-keep] if len(files) > keep else []:
        f.unlink()


def _compact(r: dict) -> dict:
    pub = r.get("publication") or {}
    return {"runId": r.get("runId"), "startedAt": r.get("startedAt"), "finishedAt": r.get("finishedAt"), "status": r.get("status"),
            "trigger": r.get("trigger"), "requests": r.get("requests"), "marketDates": r.get("marketDates"),
            "expectedLatestTradeDate": r.get("expectedLatestTradeDate"), "scheduledSlot": r.get("scheduledSlot"), "publication": pub.get("result"), "release": pub.get("release"),
            "failureReason": r.get("failureReason")}


def summarize(runs: list[dict]) -> dict:
    """lastSuccess / lastFailure / consecutiveFailures over the retained records (newest last)."""
    last_success = next((r for r in reversed(runs) if r.get("status") == "SUCCESS"), None)
    last_failure = next((r for r in reversed(runs) if r.get("status") in PROBLEM), None)
    consecutive = 0
    for r in reversed(runs):
        if r.get("status") in PROBLEM:
            consecutive += 1
        elif r.get("status") in ("SUCCESS",):
            break
        # NOOP / WAITING / SKIPPED neither reset nor extend the streak
    return {
        "lastSuccess": _compact(last_success) if last_success else None,
        "lastFailure": _compact(last_failure) if last_failure else None,
        "consecutiveFailures": consecutive,
        "runs": [_compact(r) for r in runs[-MAX_RUN_RECORDS:]],
    }


def weekly_block(runs: list[dict], now_iso: str, out_dir: Path) -> dict:
    """Weekly-policy facts for the Data Health page. Informational: the app recomputes freshness itself from the manifest."""
    from .freshness import RETRY_TIMES, SCHEDULED_TIME, SCHEDULED_WEEKDAY_NAME, TIMEZONE_NAME, UPDATE_POLICY, classify
    try:
        manifest = json.loads((Path(out_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    scheduled = [r for r in runs if r.get("trigger") == "schedule"]
    last_failure = next((r for r in reversed(runs) if r.get("status") in PROBLEM), None)
    block = {"updatePolicy": UPDATE_POLICY, "timezone": TIMEZONE_NAME, "scheduledWeekday": SCHEDULED_WEEKDAY_NAME,
             "scheduledTime": SCHEDULED_TIME.strftime("%H:%M"), "retryTimes": [t.strftime("%H:%M") for t in RETRY_TIMES],
             "lastScheduledRun": _compact(scheduled[-1]) if scheduled else None,
             "lastFailureReason": (last_failure or {}).get("failureReason"), "marketAsOf": manifest.get("marketAsOf"),
             "lastSuccessfulPublication": manifest.get("lastSuccessfulPublication")}
    if manifest:
        from datetime import datetime
        v = classify(manifest, datetime.fromisoformat(now_iso))
        block.update({"nextScheduledPublication": v["nextScheduledPublication"], "freshnessWhenWritten": v["status"], "missedWeeklyUpdates": v["missedWeeklyUpdates"]})
    return block


def write_health(now_iso: str, runs_dir: Path = RUNS_DIR, out_dir: Path = OUT_DIR) -> dict:
    runs = load_runs(runs_dir)
    health = {"schemaVersion": 2, "updatedAt": now_iso, "retained": MAX_RUN_RECORDS, **summarize(runs), "weekly": weekly_block(runs, now_iso, out_dir)}
    write_atomic(Path(out_dir) / "health.json", json.dumps(health, ensure_ascii=False, indent=1, sort_keys=True))
    return health
