"""Immutable raw snapshots.

    data/raw/<YYYY-MM-DD>/<market>/<dataset>.json        the bytes exactly as received
    data/raw/<YYYY-MM-DD>/<market>/<dataset>.meta.json   url, fetchedAt, HTTP status, ETag, Last-Modified, sha256, rows ...

A file that exists is never rewritten. Anything derived is computed FROM these files, so every number can be traced back.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


class ImmutableError(Exception):
    pass


@dataclass
class RawMeta:
    endpointId: str
    url: str
    runDate: str
    fetchedAt: str
    httpStatus: int
    etag: Optional[str]
    lastModified: Optional[str]
    sha256: str
    bytes: int
    rows: int
    runId: str
    reusedFrom: Optional[str] = None  # set when a 304 Not Modified re-used an earlier snapshot's bytes
    newFields: Optional[list] = None


@dataclass
class RawRef:
    meta: RawMeta
    path: Path
    reusedToday: bool = False  # True: today's snapshot already existed, no request was made

    def load(self):
        return json.loads(self.path.read_bytes().decode("utf-8"))


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class RawStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, run_date: str, market: str) -> Path:
        return self.root / run_date / market.lower()

    def body_path(self, run_date: str, market: str, dataset: str) -> Path:
        return self._dir(run_date, market) / f"{dataset}.json"

    def meta_path(self, run_date: str, market: str, dataset: str) -> Path:
        return self._dir(run_date, market) / f"{dataset}.meta.json"

    # ---- read -----------------------------------------------------------------------------------------------
    def get(self, run_date: str, market: str, dataset: str) -> Optional[RawRef]:
        bp, mp = self.body_path(run_date, market, dataset), self.meta_path(run_date, market, dataset)
        if not (bp.exists() and mp.exists()):
            return None
        meta = RawMeta(**json.loads(mp.read_text(encoding="utf-8")))
        if sha256_hex(bp.read_bytes()) != meta.sha256:
            raise ImmutableError(f"raw file {bp} no longer matches its recorded hash")
        return RawRef(meta=meta, path=bp)

    def latest_before(self, run_date: str, market: str, dataset: str) -> Optional[RawRef]:
        if not self.root.exists():
            return None
        days = sorted((d.name for d in self.root.iterdir() if d.is_dir() and d.name < run_date), reverse=True)
        for d in days:
            ref = self.get(d, market, dataset)
            if ref:
                return ref
        return None

    # ---- write (once) ---------------------------------------------------------------------------------------
    def write(self, meta: RawMeta, body: bytes, market: str, dataset: str) -> RawRef:
        bp, mp = self.body_path(meta.runDate, market, dataset), self.meta_path(meta.runDate, market, dataset)
        if bp.exists() or mp.exists():
            raise ImmutableError(f"raw snapshot already exists and is immutable: {bp}")
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_bytes(body)
        mp.write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=2), encoding="utf-8")
        return RawRef(meta=meta, path=bp)

    def write_rejected(self, run_date: str, market: str, dataset: str, body: bytes, note: str) -> Path:
        """Evidence for a failed ingestion. Kept next to the raw files but clearly marked; never read by the pipeline."""
        p = self._dir(run_date, market) / f"{dataset}.rejected.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_bytes(body)
            p.with_suffix(".note.txt").write_text(note, encoding="utf-8")
        return p
