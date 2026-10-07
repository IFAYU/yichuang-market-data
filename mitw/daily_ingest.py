"""Market-scoped raw ingestion for the daily policy (Phase 3I.2).

Only the markets that OWE a newer date are queried. A market that owes nothing is CARRIED: its raw files are copied (no request) from the raw
partition its Last Known Good was built from, with their ORIGINAL fetch times and `reusedFrom` recorded, so provenance stays honest and the
unchanged pure build() sees one complete partition. If the carried partition is not available (a cold CI runner, a cache miss, a Last Known Good
from the weekly policy) the market is fetched instead: correctness never depends on a cache, only the request count does.
"""
from __future__ import annotations

from pathlib import Path

from .ingest import IngestResult, reuse_recent
from .providers.endpoints import CORE_IDS, ENDPOINTS, EXTRA_IDS
from .providers.http import RunContext, SourceStopped, fetch_endpoint, new_run
from .raw.store import RawMeta, RawStore
from .validate.schema import SchemaDriftError


def _copy_carried(store: RawStore, src_partition: str, partition: str, ep, ctx: RunContext):
    ref = store.get(src_partition, ep.market, ep.dataset)
    if ref is None:
        return None
    m = ref.meta
    meta = RawMeta(ep.id, ep.url, partition, m.fetchedAt, m.httpStatus, m.etag, m.lastModified, m.sha256, m.bytes, m.rows, ctx.run_id, reusedFrom=m.runDate)
    ctx.reused_recent += 1
    return store.write(meta, ref.path.read_bytes(), ep.market, ep.dataset)


def _can_carry(store: RawStore, src: str | None, market: str) -> bool:
    """A market is carried only as a WHOLE: every dataset of it must exist in the source partition (never a mixture of copied and fetched files)."""
    if not src:
        return False
    try:
        return all(store.get(src, ENDPOINTS[e].market, ENDPOINTS[e].dataset) is not None for e in list(CORE_IDS) + list(EXTRA_IDS) if ENDPOINTS[e].market == market and ENDPOINTS[e].kind != "swagger")
    except Exception:  # a raw file that no longer matches its hash is not carried either
        return False


def ingest_markets(partition: str, raw_dir: Path, ctx: RunContext | None, fetch_markets: tuple, carry: dict, fetch=fetch_endpoint) -> tuple:
    """Returns (IngestResult, fallbacks). `carry` = {market: raw partition name of its Last Known Good}. `fallbacks` = carried markets that had to be fetched."""
    store = RawStore(raw_dir)
    ctx = ctx or new_run(partition)
    result = IngestResult(ctx=ctx)
    markets = sorted({ENDPOINTS[e].market for e in list(CORE_IDS) + list(EXTRA_IDS)})
    plan = {m: ("FETCH" if m in fetch_markets else "CARRY" if _can_carry(store, carry.get(m), m) else "FALLBACK") for m in markets}
    for eid in list(CORE_IDS) + list(EXTRA_IDS):
        ep = ENDPOINTS[eid]
        try:
            if plan[ep.market] == "CARRY":
                if ep.kind != "swagger":  # the API description is only a schema-drift probe: the pure build never reads it
                    result.fetched[eid] = _copy_carried(store, carry[ep.market], partition, ep, ctx)
                continue
            if store.get(ctx.run_date, ep.market, ep.dataset) is None:
                reused = reuse_recent(ep, store, ctx)
                if reused is not None:
                    result.fetched[eid] = reused
                    continue
            result.fetched[eid] = fetch(ep, store, ctx)
        except SchemaDriftError as e:
            result.failures[eid] = str(e)
            result.schema_problems[eid] = e.problems
        except SourceStopped as e:
            result.failures[eid] = str(e)
    return result, tuple(m for m in markets if plan[m] == "FALLBACK")


def failures_by_market(result: IngestResult) -> dict:
    out: dict = {}
    for eid, why in result.failures.items():
        out.setdefault(ENDPOINTS[eid].market, why)
    return out


def prune_raw(raw_dir: Path, keep: int = 4, protect=()) -> list:
    """The CI raw cache must not grow without bound: keep the newest `keep` partitions plus every partition a published manifest still names."""
    d = Path(raw_dir)
    if not d.exists():
        return []
    parts = sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    gone = []
    for p in parts[keep:]:
        if p.name in set(protect):
            continue
        for f in sorted(p.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        p.rmdir()
        gone.append(p.name)
    return gone
