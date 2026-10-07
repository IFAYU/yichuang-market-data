"""Read back a published 興櫃 release from the PUBLIC site and compare its bytes with the manifest (Phase 3I.1, STAGED helper).

    python scripts/verify_emerging_public.py --base https://ifayu.github.io/yichuang-market-data --manifest out/emerging/manifest.json

Exit 0 only when the public file answers 200, is JSON, has the exact sha256 and the exact company count the manifest promises.
Used between "release pushed" and "manifest pushed": the pointer is moved only after the public site really serves the release.
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
    f = m["files"]["emerging-snapshot.json"]
    url = f"{a.base.rstrip('/')}/emerging/{f['path']}"
    deadline = time.time() + a.timeout
    last = "never answered"
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(urllib.request.Request(f"{url}?cb={time.time()}", headers={"Cache-Control": "no-cache"}), timeout=30)
            body = r.read()
            if r.status == 200 and hashlib.sha256(body).hexdigest() == f["sha256"]:
                doc = json.loads(body.decode("utf-8"))
                if doc.get("kind") == "emerging-snapshot" and len(doc.get("companies", [])) == m["companyCount"] and doc.get("marketAsOf") == m["marketAsOf"]:
                    print(f"OK {m['release']}: {m['companyCount']} companies, {m['marketAsOf']}, sha256 {f['sha256'][:12]}")
                    return 0
                last = "bytes match but the document does not agree with the manifest"
            else:
                last = f"HTTP {r.status}, sha256 {hashlib.sha256(body).hexdigest()[:12]} != {f['sha256'][:12]}"
        except Exception as e:  # not deployed yet (404) or a transient error: keep waiting
            last = str(e)[:100]
        time.sleep(15)
    print(f"NOT READABLE: {last}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
