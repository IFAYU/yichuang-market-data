"""Read back a published ETF release from the PUBLIC site and compare the bytes of every file with etf/manifest.json (Phase 8P).

    python scripts/verify_etf_public.py --base https://ifayu.github.io/yichuang-market-data --manifest out/etf/manifest.json

Exit 0 only when each file the manifest lists answers 200 with the exact sha256. Used between "release pushed" and "manifest pushed":
the pointer is moved only after the public site really serves the release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()
    m = json.load(open(a.manifest, encoding="utf-8"))
    deadline = time.time() + a.timeout
    pending = dict(m["files"])
    last = "never answered"
    while pending and time.time() < deadline:
        for name, f in list(pending.items()):
            try:
                url = f"{a.base.rstrip('/')}/{f['path']}"
                r = urllib.request.urlopen(urllib.request.Request(f"{url}?cb={time.time()}", headers={"Cache-Control": "no-cache"}), timeout=30)
                body = r.read()
                if r.status == 200 and hashlib.sha256(body).hexdigest() == f["sha256"]:
                    del pending[name]
                else:
                    last = f"{name}: HTTP {r.status}, sha256 {hashlib.sha256(body).hexdigest()[:12]} != {f['sha256'][:12]}"
            except Exception as e:  # not deployed yet (404) or a transient error: keep waiting
                last = f"{name}: {str(e)[:100]}"
        if pending:
            time.sleep(15)
    if pending:
        print(f"NOT READABLE: {last}", file=sys.stderr)
        return 1
    print(f"OK {m['release']}: {len(m['files'])} files, price date {m['priceDate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
