"""Read a publication back from its PUBLIC address, the way a browser would.

A release being committed is not the same as it being readable: GitHub Pages deploys after a push, and a manifest that points at a
release the site cannot serve yet would break every visitor. So the workflow pushes the immutable release FIRST, waits here until
the public site serves each of its files with exactly the sha256 recorded in the manifest, and only then publishes the manifest.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable, Optional

import requests

FILES = ("market-snapshot.json", "industry-snapshots.json")


def _url(base: str, path: str, bust: str) -> str:
    return f"{base.rstrip('/')}/{path}?cb={bust}"


def verify_release(base: str, out_dir: Path, release: str, timeout_s: int = 900, interval_s: int = 20,
                   get: Optional[Callable] = None, sleep: Callable = time.sleep, clock: Callable = time.monotonic) -> tuple[bool, str]:
    """True once EVERY file of `release` is served with the manifest's sha256. Never raises: a network problem is just 'not yet'."""
    get = get or (lambda u: requests.get(u, timeout=30, headers={"Cache-Control": "no-cache"}))
    manifest = json.loads((Path(out_dir) / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("release") != release:
        return False, f"manifest points at {manifest.get('release')}, not {release}"
    wanted = {n: manifest["files"][n]["sha256"] for n in FILES}
    deadline, last = clock() + timeout_s, "not tried"
    while True:
        bad = []
        for name, sha in wanted.items():
            try:
                r = get(_url(base, f"releases/{release}/{name}", str(int(time.time()))))
                if r.status_code != 200:
                    bad.append(f"{name}: HTTP {r.status_code}")
                elif hashlib.sha256(r.content).hexdigest() != sha:
                    bad.append(f"{name}: served bytes do not match the manifest sha256")
            except requests.RequestException as e:
                bad.append(f"{name}: {type(e).__name__}")
        if not bad:
            return True, f"release {release} is readable from {base}"
        last = "; ".join(bad)
        if clock() >= deadline:
            return False, f"release {release} not readable after {timeout_s}s: {last}"
        sleep(interval_s)
