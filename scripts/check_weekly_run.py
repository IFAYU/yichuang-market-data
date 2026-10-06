"""Observation gate for a weekly publication slot: did Sunday 10:00 / 14:00 / 20:00 behave as designed?

    python scripts/check_weekly_run.py                      # the most recent Sunday slot that has started
    python scripts/check_weekly_run.py --slot 2026-10-11    # a specific Sunday

Reads ONLY the public site (manifest.json + health.json), the way any visitor would. Exit code 0 = behaved as designed,
1 = unexpected, 2 = the slot has not started yet (PENDING). "Behaved as designed" includes the honest failure path
(three failed attempts, Last Known Good untouched, STALE from Monday): that is correct behaviour, but it is reported as
NO_NEW_DATA so a person looks at why the exchanges gave nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mitw.freshness import TAIPEI, classify, parse_instant, slot_at_or_before  # noqa: E402

BASE = "https://ifayu.github.io/yichuang-market-data/"


def get(name: str):
    with urllib.request.urlopen(f"{BASE}{name}?cb={int(datetime.now().timestamp())}", timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", help="Sunday date YYYY-MM-DD (default: most recent slot that has started)")
    args = ap.parse_args()
    now = datetime.now(TAIPEI)
    slot = datetime.fromisoformat(f"{args.slot}T10:00:00").replace(tzinfo=TAIPEI) if args.slot else slot_at_or_before(now)
    if now < slot:
        print(f"PENDING: slot {slot.isoformat()} has not started (now {now.isoformat(timespec='seconds')})")
        return 2
    manifest, health = get("manifest.json"), get("health.json")
    end = slot + timedelta(hours=14)  # through the 20:00 attempt and its pipeline time
    runs = [r for r in health.get("runs", []) if (t := parse_instant(r.get("startedAt"))) and slot - timedelta(minutes=5) <= t <= end + timedelta(hours=4)]
    print(f"slot {slot.isoformat()}  release {manifest.get('release')}  marketAsOf {manifest.get('marketAsOf')}")
    for r in runs:
        print(f"  {r['startedAt']}  {r.get('trigger')}  {r.get('status')}  {r.get('publication')}  {r.get('failureReason') or ''}"[:200])
    published = parse_instant(manifest.get("lastSuccessfulPublication"))
    verdict = classify(manifest, now)
    print(f"freshness now: {verdict['status']} (missed weekly updates: {verdict['missedWeeklyUpdates']})  next: {verdict['nextScheduledPublication']}")
    sched = [r for r in runs if r.get("trigger") == "schedule"]
    if not sched and published and published >= slot:
        print("RESULT: NOT_APPLICABLE (no scheduled run for this slot: the current release was published manually / seeded at "
              f"{manifest.get('lastSuccessfulPublication')}); the first scheduled slot to observe is the next Sunday)")
        return 2
    ok_idx = next((i for i, r in enumerate(sched) if r.get("status") == "SUCCESS"), None)
    if published and published >= slot and ok_idx is not None:
        before = sched[:ok_idx]
        after = sched[ok_idx + 1:]
        good = all(r.get("status") == "WAITING" for r in before) and all(r.get("status") == "NOOP" for r in after)
        print("RESULT:", "PASS (publication succeeded; later attempts were NOOP)" if good else "UNEXPECTED attempt sequence")
        return 0 if good else 1
    if len(sched) == 3 and sched[-1].get("status") == "FAILED" and all(r.get("status") in ("WAITING", "FAILED") for r in sched):
        print("RESULT: NO_NEW_DATA (honest failure path: Last Known Good unchanged, STALE from Monday) - find out why the exchanges gave nothing new")
        return 0
    if now < end:
        print("RESULT: IN_PROGRESS (more attempts may still run today)")
        return 2
    print("RESULT: UNEXPECTED (no publication and the attempt sequence is not the designed failure path)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
