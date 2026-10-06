"""Orchestrates the raw fetch. Raw only: no transformation happens here.

`ingest(stage)`      the original one-shot fetch (core / full), used by hand.
`ingest_daily(...)`  the scheduled form: prices and official ratios are fetched for a new trading day (4 requests), while datasets
                     that change monthly / quarterly (company list, EPS table, monthly revenue, statement-form lists) are COPIED from
                     a recent earlier raw snapshot with no request when they are younger than their reuse window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .config import RAW_DIR
from .normalize.parse import parse_tw_date
from .providers.endpoints import CORE_IDS, ENDPOINTS, EXTRA_IDS, Endpoint, max_age_days
from .providers.http import RunContext, SourceStopped, fetch_endpoint, new_run
from .raw.store import RawMeta, RawRef, RawStore
from .validate.schema import SchemaDriftError


@dataclass
class IngestResult:
    ctx: RunContext
    fetched: dict = field(default_factory=dict)  # endpoint id -> RawRef
    failures: dict = field(default_factory=dict)  # endpoint id -> reason
    schema_problems: dict = field(default_factory=dict)  # endpoint id -> [problems] (schema drift only)

    @property
    def ok(self) -> bool:
        return not self.failures


def ingest(stage: str, raw_dir: Path = RAW_DIR, run_date: str | None = None, ctx: RunContext | None = None,
           fetch=fetch_endpoint, only: list[str] | None = None) -> IngestResult:
    """stage: 'core' (bounded dry run set) | 'full' (core + extra). Endpoints already fetched today are re-used, never re-requested.
    `only` restricts to specific endpoint ids (still subject to the same-day reuse rule)."""
    ids = list(CORE_IDS) if stage == "core" else list(CORE_IDS) + list(EXTRA_IDS)
    if only:
        ids = [i for i in ids if i in only]
    store = RawStore(raw_dir)
    ctx = ctx or new_run(run_date)
    result = IngestResult(ctx=ctx)
    for eid in ids:
        ep = ENDPOINTS[eid]
        try:
            result.fetched[eid] = fetch(ep, store, ctx)
        except SchemaDriftError as e:
            result.failures[eid] = str(e)
            result.schema_problems[eid] = e.problems
        except SourceStopped as e:
            result.failures[eid] = str(e)
    return result


def _day(partition: str) -> date:
    return date.fromisoformat(partition[:10])


def reuse_recent(ep: Endpoint, store: RawStore, ctx: RunContext) -> Optional[RawRef]:
    """Copy an earlier successful raw file into this partition (no request) when it is within the endpoint's reuse window.
    The copy keeps the ORIGINAL fetch time and records where it came from, so provenance stays honest."""
    window = max_age_days(ep)
    if window <= 0:
        return None
    prior = store.latest_before(ctx.run_date, ep.market, ep.dataset)
    if prior is None:
        return None
    fetched_day = date.fromisoformat(prior.meta.fetchedAt[:10])
    if (_day(ctx.run_date) - fetched_day).days > window:
        return None
    body = prior.path.read_bytes()
    meta = RawMeta(ep.id, ep.url, ctx.run_date, prior.meta.fetchedAt, prior.meta.httpStatus, prior.meta.etag, prior.meta.lastModified,
                   prior.meta.sha256, prior.meta.bytes, prior.meta.rows, ctx.run_id, reusedFrom=prior.meta.runDate)
    ctx.reused_recent += 1
    ctx.log(f"{ep.id}: re-used raw from {prior.meta.runDate} (fetched {prior.meta.fetchedAt[:10]}, window {window}d), 0 requests")
    return store.write(meta, body, ep.market, ep.dataset)


def ingest_daily(partition: str, raw_dir: Path = RAW_DIR, ctx: RunContext | None = None, fetch=fetch_endpoint) -> IngestResult:
    store = RawStore(raw_dir)
    ctx = ctx or new_run(partition)
    result = IngestResult(ctx=ctx)
    for eid in list(CORE_IDS) + list(EXTRA_IDS):
        ep = ENDPOINTS[eid]
        try:
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
    return result


def list_partitions(raw_dir: Path, run_date: str) -> list[str]:
    d = Path(raw_dir)
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and (p.name == run_date or p.name.startswith(run_date + ".")))


def next_partition(raw_dir: Path, run_date: str) -> str:
    existing = list_partitions(raw_dir, run_date)
    return run_date if not existing else f"{run_date}.{len(existing) + 1}"


def partition_market_dates(raw_dir: Path, partition: str) -> dict:
    """Trade date of the prices file of each exchange inside a raw partition (what that fetch actually returned)."""
    store, out = RawStore(raw_dir), {}
    for market, key in (("TWSE", "Date"), ("TPEX", "Date")):
        ref = store.get(partition, market, "prices")
        out[market] = None
        if ref:
            rows = ref.load()
            out[market] = parse_tw_date(rows[0].get(key)) if rows else None
    return out
