"""Daily release retention (Phase 3I.2). KEEP_RELEASES=14 meant 14 WEEKS under the weekly policy; read as 14 daily releases it would silently shrink the
rollback window to two weeks, so the daily policy has its own explicit rule:

  keep EVERY release published within the last 30 calendar days
  PLUS the newest release of each earlier ISO week, for 12 further weeks
  ALWAYS keep: the release the manifest points at, every release named in `protect` (the previous one = rollback target, anything a caller needs)
  NEVER delete a release whose publication time cannot be read (what cannot be classified is not deleted)

Deleting a release removes its files from the branch's working tree only (git history keeps them); the manifest target can never be selected.
Expected growth with this rule: <= ~30 + 12 releases on disk (main release ~4 MB raw each => ~170 MB working tree worst case; git history grows
~0.6 MB compressed per release per day, i.e. about 150 MB a year for the main snapshot, ~9 MB a year for 興櫃).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

RECENT_DAYS = 30
WEEKLY_ANCHOR_WEEKS = 12
TAIPEI = timezone(timedelta(hours=8))


def _published(rel_dir: Path) -> Optional[datetime]:
    try:
        v = json.loads((rel_dir / "release.json").read_text(encoding="utf-8")).get("publishedAt")
        t = datetime.fromisoformat(v)
        return t if t.tzinfo else t.replace(tzinfo=TAIPEI)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def plan(releases: dict, now: datetime, protect: Iterable[str] = ()) -> tuple[set, set]:
    """releases: {name: published datetime | None}. Returns (keep, delete)."""
    now = now if now.tzinfo else now.replace(tzinfo=TAIPEI)
    keep = set(protect)
    cutoff = now - timedelta(days=RECENT_DAYS)
    anchor_floor = cutoff - timedelta(weeks=WEEKLY_ANCHOR_WEEKS)
    newest_in_week: dict = {}
    for name, t in releases.items():
        if t is None:
            keep.add(name)
            continue
        if t >= cutoff:
            keep.add(name)
        elif t >= anchor_floor:
            wk = t.astimezone(TAIPEI).isocalendar()[:2]
            if wk not in newest_in_week or t > releases[newest_in_week[wk]]:
                newest_in_week[wk] = name
    keep |= set(newest_in_week.values())
    return keep & set(releases), set(releases) - keep


def prune_daily(releases_dir: Path, now: datetime, protect: Iterable[str] = ()) -> list[str]:
    """Delete the releases the rule drops. Returns their names. Never touches a protected release or one without a readable publication time."""
    d = Path(releases_dir)
    if not d.exists():
        return []
    rels = {p.name: _published(p) for p in d.iterdir() if p.is_dir()}
    _, delete = plan(rels, now, protect)
    for name in sorted(delete):
        for f in (d / name).iterdir():
            f.unlink()
        (d / name).rmdir()
    return sorted(delete)
