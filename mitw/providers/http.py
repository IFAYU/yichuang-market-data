"""Request policy for official batch endpoints.

Allowed: one GET per dataset, conditional requests where the source supports them, a minimum gap between requests to the
same host, and a very small number of retries for plain network errors / 5xx.

Never: retry after 403 / 429 / HTML / schema drift / empty market response (the source is STOPPED for the run);
rotate User-Agent, IP or proxy; use a browser; touch captchas. There is intentionally no configuration for any of those.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import requests

from ..config import REQUEST_POLICY, RequestPolicy
from ..raw.store import RawMeta, RawRef, RawStore, sha256_hex
from ..validate.schema import SchemaDriftError, check_payload
from .endpoints import ENDPOINTS, Endpoint

TZ_TAIPEI = timezone(timedelta(hours=8))


def now_taipei() -> datetime:
    return datetime.now(TZ_TAIPEI)


class SourceStopped(Exception):
    """A source answered in a way that means we must stop talking to it for this run."""

    def __init__(self, endpoint_id: str, reason: str):
        self.endpoint_id = endpoint_id
        self.reason = reason
        super().__init__(f"{endpoint_id}: {reason}")


@dataclass
class RunContext:
    run_id: str
    run_date: str  # ISO date, Asia/Taipei
    request_count: int = 0
    reused_today: int = 0
    reused_recent: int = 0  # copied from an earlier raw snapshot inside the endpoint's reuse window (no request)
    not_modified: int = 0
    halted: dict = field(default_factory=dict)  # market -> reason
    events: list = field(default_factory=list)
    last_request_at: dict = field(default_factory=dict)  # host -> monotonic seconds

    def log(self, msg: str) -> None:
        self.events.append(f"{now_taipei().isoformat(timespec='seconds')} {msg}")


def new_run(run_date: Optional[str] = None) -> RunContext:
    t = now_taipei()
    return RunContext(run_id=t.strftime("%Y%m%dT%H%M%S"), run_date=run_date or t.date().isoformat())


def _looks_like_html(resp: requests.Response, body: bytes) -> bool:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    head = body[:200].lstrip().lower()
    return "text/html" in ctype or head.startswith(b"<!doctype html") or head.startswith(b"<html")


def fetch_endpoint(
    ep: Endpoint,
    store: RawStore,
    ctx: RunContext,
    policy: RequestPolicy = REQUEST_POLICY,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> RawRef:
    # 1. Same-day success is never fetched twice.
    existing = store.get(ctx.run_date, ep.market, ep.dataset)
    if existing:
        ctx.reused_today += 1
        ctx.log(f"{ep.id}: reused today's raw snapshot (0 requests)")
        return RawRef(meta=existing.meta, path=existing.path, reusedToday=True)

    # 2. A stopped source stays stopped.
    if ep.market in ctx.halted:
        raise SourceStopped(ep.id, f"source {ep.market} already stopped this run: {ctx.halted[ep.market]}")

    sess = session or requests.Session()
    headers = {"User-Agent": policy.user_agent, "Accept": "application/json"}
    prior = store.latest_before(ctx.run_date, ep.market, ep.dataset) if ep.conditional else None
    if prior:
        if prior.meta.etag:
            headers["If-None-Match"] = prior.meta.etag
        if prior.meta.lastModified:
            headers["If-Modified-Since"] = prior.meta.lastModified

    last_err: Optional[str] = None
    for attempt in range(policy.max_attempts):
        # politeness gap per host
        last = ctx.last_request_at.get(ep.host)
        if last is not None:
            wait = policy.min_delay_s - (monotonic() - last)
            if wait > 0:
                sleep(wait + random.uniform(0, 0.5))
        ctx.last_request_at[ep.host] = monotonic()
        ctx.request_count += 1
        try:
            resp = sess.get(ep.url, headers=headers, timeout=policy.timeout_s)
        except requests.RequestException as e:
            last_err = f"network error: {type(e).__name__}"
            ctx.log(f"{ep.id}: {last_err} (attempt {attempt + 1}/{policy.max_attempts})")
            if attempt + 1 < policy.max_attempts:
                sleep(policy.backoff_s[min(attempt, len(policy.backoff_s) - 1)] + random.uniform(0, 1.5))
                continue
            ctx.halted[ep.market] = last_err
            raise SourceStopped(ep.id, last_err)

        code = resp.status_code
        if code in (403, 429):
            ctx.halted[ep.market] = f"HTTP {code}"
            ctx.log(f"{ep.id}: HTTP {code} -> source stopped, no retry")
            raise SourceStopped(ep.id, f"HTTP {code}")
        if code == 304 and prior:
            ctx.not_modified += 1
            body = prior.path.read_bytes()
            meta = RawMeta(ep.id, ep.url, ctx.run_date, now_taipei().isoformat(timespec="seconds"), 304,
                           prior.meta.etag, prior.meta.lastModified, sha256_hex(body), len(body), prior.meta.rows,
                           ctx.run_id, reusedFrom=prior.meta.runDate)
            ctx.log(f"{ep.id}: 304 Not Modified, re-used snapshot of {prior.meta.runDate}")
            return store.write(meta, body, ep.market, ep.dataset)
        if code >= 500:
            last_err = f"HTTP {code}"
            ctx.log(f"{ep.id}: {last_err} (attempt {attempt + 1}/{policy.max_attempts})")
            if attempt + 1 < policy.max_attempts:
                sleep(policy.backoff_s[min(attempt, len(policy.backoff_s) - 1)] + random.uniform(0, 1.5))
                continue
            ctx.halted[ep.market] = last_err
            raise SourceStopped(ep.id, last_err)
        if code != 200:
            ctx.halted[ep.market] = f"HTTP {code}"
            raise SourceStopped(ep.id, f"unexpected HTTP {code}")

        body = resp.content
        if _looks_like_html(resp, body):
            store.write_rejected(ctx.run_date, ep.market, ep.dataset, body, "unexpected HTML instead of JSON")
            ctx.halted[ep.market] = "unexpected HTML"
            ctx.log(f"{ep.id}: HTML instead of JSON -> source stopped")
            raise SourceStopped(ep.id, "unexpected HTML response")
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            store.write_rejected(ctx.run_date, ep.market, ep.dataset, body, "not valid JSON")
            ctx.halted[ep.market] = "invalid JSON"
            raise SourceStopped(ep.id, "response is not valid JSON")

        report = check_payload(ep, payload)
        if not report.ok:
            store.write_rejected(ctx.run_date, ep.market, ep.dataset, body, "; ".join(report.problems))
            ctx.halted[ep.market] = "schema drift"
            ctx.log(f"{ep.id}: SCHEMA DRIFT {report.problems[:3]}")
            raise SchemaDriftError(ep.id, report.problems)

        meta = RawMeta(ep.id, ep.url, ctx.run_date, now_taipei().isoformat(timespec="seconds"), code,
                       resp.headers.get("ETag"), resp.headers.get("Last-Modified"), sha256_hex(body), len(body),
                       report.rows, ctx.run_id, newFields=report.new_fields or None)
        ctx.log(f"{ep.id}: 200 OK, {report.rows} rows, {len(body)} bytes")
        return store.write(meta, body, ep.market, ep.dataset)

    raise SourceStopped(ep.id, last_err or "failed")
