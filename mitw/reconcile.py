"""Phase 3I.2C: close a stuck PENDING_PUBLIC_READBACK without ever touching a release or the manifest.

Lifecycle of a main publication (each step is recorded; nothing is claimed before it is true):
  1. release written to the data branch                 -> publicVerification = PENDING_PUBLIC_READBACK
  2. every release file read back from the public site  -> VERIFIED_PUBLIC_READBACK   (published in the SAME commit as manifest.json)
  3. manifest.json pushed last                          -> the release is live
If the process died / an older build left a PENDING record for a release that the live manifest already points at, `reconcile_health`
re-checks the public bytes and only then upgrades the record. A failed re-check keeps PENDING and records the attempt: it never downgrades
or removes the Last Known Good (the manifest and the release files are not read for writing here).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

PENDING = "PENDING_PUBLIC_READBACK"
VERIFIED = "VERIFIED_PUBLIC_READBACK"


def reconcile_health(health: dict, manifest: Optional[dict], verify: Callable[[str], tuple], now_iso: str) -> tuple[dict, str]:
    """Return (health, outcome). outcome: NOTHING_PENDING | NOT_LIVE_RELEASE | VERIFIED | STILL_PENDING. Pure apart from calling `verify(release)`."""
    daily = (health or {}).get("daily") or {}
    last = daily.get("lastDecision") or {}
    if last.get("publicVerification") != PENDING:
        return health, "NOTHING_PENDING"
    release = last.get("releaseId")
    # a pending record only means something while the live manifest still points at that release; otherwise the claim is about a release nobody sees
    if not release or not manifest or manifest.get("release") != release:
        return health, "NOT_LIVE_RELEASE"
    ok, detail = verify(release)
    last = dict(last)
    if ok:
        last["publicVerification"] = VERIFIED
        last["publicVerifiedAt"] = now_iso
        last["publicVerificationDetail"] = "re-checked after publication (reconcile)"
        outcome = "VERIFIED"
    else:
        last["publicVerificationCheckedAt"] = now_iso
        last["publicVerificationDetail"] = str(detail)[:300]
        outcome = "STILL_PENDING"
    out = {**health, "updatedAt": now_iso, "daily": {**daily, "lastDecision": last}}
    if ok:
        runs = []
        for r in health.get("runs") or []:
            d = r.get("daily") if isinstance(r, dict) else None
            if isinstance(d, dict) and d.get("releaseId") == release and d.get("publicVerification") == PENDING:
                r = {**r, "daily": {**d, "publicVerification": VERIFIED}}
            runs.append(r)
        out["runs"] = runs
    return out, outcome


def reconcile_files(out_dir: Path, verify: Callable[[str], tuple], now_iso: str) -> str:
    """Read health.json + manifest.json from `out_dir`, write health.json back only when something was proven. Never raises on unreadable input."""
    out_dir = Path(out_dir)
    try:
        health = json.loads((out_dir / "health.json").read_text(encoding="utf-8"))
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "UNREADABLE"
    new, outcome = reconcile_health(health, manifest, verify, now_iso)
    if new is not health:
        tmp = out_dir / "health.json.tmp"
        tmp.write_text(json.dumps(new, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(out_dir / "health.json")
    return outcome
