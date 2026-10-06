"""sync-market-data: copy the published snapshot (manifest + the ONE release it points at + health.json) into a consuming app's static
folder (risk-profiler/public/market-data), keeping the same layout:

    manifest.json   releases/<hash>/market-snapshot.json   releases/<hash>/industry-snapshots.json   health.json

Never a manual copy. The app only ever sees files that passed these checks, and a failed sync leaves the previous valid snapshot in place:

  1. the manifest and the two files it names exist and parse
  2. schemaVersion is supported (same major version) and consistent across manifest and documents
  3. sha256 of each file equals the manifest; manifest.sourceHash and manifest.release are consistent with the files
  4. basic structure (companies / snapshots present, as-of dates present and equal to the manifest's)
  5. the release directory is copied completely and re-verified BEFORE manifest.json is replaced; the manifest is replaced last
     (it is the commit point), so the app can never see a new manifest with old files or the reverse.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from .config import OUT_DIR
from .contracts.version import SNAPSHOT_SCHEMA_VERSION
from .providers.http import now_taipei

FILES = ("market-snapshot.json", "industry-snapshots.json")
SUPPORTED_MAJOR = int(SNAPSHOT_SCHEMA_VERSION.split(".")[0])
KEEP_TARGET_RELEASES = 3  # the current one plus a couple of earlier ones for rollback; older ones are removed


class SyncRefused(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _safe_rel(path: str) -> bool:
    return isinstance(path, str) and path != "" and not path.startswith(("/", "\\")) and ".." not in Path(path).parts and ":" not in path


def verify_source(source_dir: Path) -> tuple[dict, dict, dict, dict]:
    """Returns (market_doc, industry_doc, source_manifest, raw_bytes_by_name) or raises SyncRefused."""
    problems: list[str] = []
    source_dir = Path(source_dir)
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.exists():
        raise SyncRefused([f"source manifest missing: {manifest_path}"])
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SyncRefused([f"source manifest is not valid JSON: {e}"])

    blobs: dict[str, bytes] = {}
    docs: dict[str, dict] = {}
    for name in FILES:
        rec = (manifest.get("files") or {}).get(name)
        if not rec:
            problems.append(f"{name} is not listed in the source manifest")
            continue
        rel = rec.get("path", name)
        if not _safe_rel(rel):
            problems.append(f"{name}: unsafe path {rel!r}")
            continue
        p = source_dir / rel
        if not p.exists():
            problems.append(f"required file missing: {rel}")
            continue
        b = p.read_bytes()
        blobs[name] = b
        if rec.get("sha256") != _sha(b):
            problems.append(f"{name}: sha256 does not match the source manifest (file changed after publication)")
        try:
            docs[name] = json.loads(b.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            problems.append(f"{name}: not valid JSON ({e})")
    if problems:
        raise SyncRefused(problems)

    mk, ind = docs["market-snapshot.json"], docs["industry-snapshots.json"]
    for label, doc in (("manifest", manifest), ("market", mk), ("industry", ind)):
        v = str(doc.get("schemaVersion", ""))
        if not v.split(".")[0].isdigit() or int(v.split(".")[0]) != SUPPORTED_MAJOR:
            problems.append(f"{label}: unsupported schemaVersion {v!r} (this sync supports {SUPPORTED_MAJOR}.x)")
    if len({mk.get("schemaVersion"), ind.get("schemaVersion"), manifest.get("schemaVersion")}) != 1:
        problems.append("schemaVersion differs between manifest and documents")
    for k in ("generatedAt", "marketAsOf", "financialAsOf"):
        if not mk.get(k):
            problems.append(f"market-snapshot.json: missing {k}")
    if manifest.get("marketAsOf") != mk.get("marketAsOf"):
        problems.append("manifest.marketAsOf differs from market-snapshot.json")
    expected = _sha("".join(_sha(blobs[n]) for n in FILES).encode("ascii"))
    if manifest.get("sourceHash") != expected:
        problems.append("manifest.sourceHash does not match the two files")
    if manifest.get("release") != expected[:12]:
        problems.append("manifest.release does not match sourceHash")
    if not isinstance(mk.get("companies"), list) or len(mk["companies"]) < 100:
        problems.append("market-snapshot.json: companies missing or implausibly few")
    if not isinstance(ind.get("snapshots"), list) or not ind["snapshots"]:
        problems.append("industry-snapshots.json: snapshots missing")
    if problems:
        raise SyncRefused(problems)
    return mk, ind, manifest, blobs


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def sync(source_dir: Path = OUT_DIR, target_dir: Path | None = None) -> dict:
    if target_dir is None:
        raise SyncRefused(["target directory is required"])
    source_dir, target_dir = Path(source_dir), Path(target_dir)
    mk, ind, src_manifest, blobs = verify_source(source_dir)
    release = src_manifest["release"]

    health = source_dir / "health.json"  # information only: refreshed on every sync, never part of the hash-pinned release
    if health.exists():
        _atomic_write(target_dir / "health.json", health.read_bytes())

    # Same release again => nothing is rewritten (manifest, including syncedAt, stays byte-identical).
    existing = target_dir / "manifest.json"
    if existing.exists():
        try:
            prev = json.loads(existing.read_text(encoding="utf-8"))
            rel_dir = target_dir / "releases" / release
            same = all((rel_dir / n).exists() and _sha((rel_dir / n).read_bytes()) == _sha(blobs[n]) for n in FILES)
            if same and prev.get("sourceHash") == src_manifest["sourceHash"] and prev.get("schemaVersion") == src_manifest["schemaVersion"]:
                return prev
        except (json.JSONDecodeError, OSError):
            pass

    app_manifest = {**src_manifest, "syncedAt": now_taipei().isoformat(timespec="seconds")}
    rel_dir = target_dir / "releases" / release
    try:
        for n in FILES:
            _atomic_write(rel_dir / n, blobs[n])
        _atomic_write(rel_dir / "release.json", json.dumps(src_manifest, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
        for n in FILES:  # verify the copy that will be served, not the source we copied from
            if _sha((rel_dir / n).read_bytes()) != src_manifest["files"][n]["sha256"]:
                raise SyncRefused([f"{n}: copy in the target does not match the manifest; manifest.json was not replaced"])
        _atomic_write(existing, json.dumps(app_manifest, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))  # LAST
    except SyncRefused:
        raise
    except Exception:
        for p in (target_dir.glob(".*.tmp")):
            p.unlink()
        raise
    _prune_target(target_dir, current=release)
    return app_manifest


def _prune_target(target_dir: Path, current: str) -> None:
    d = target_dir / "releases"
    dirs = sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in dirs[KEEP_TARGET_RELEASES:]:
        if p.name != current:
            shutil.rmtree(p, ignore_errors=True)
