"""Schema-drift detection. Any finding => the ingestion FAILS; no snapshot is produced from suspect data."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..providers.endpoints import Endpoint

_SCALARS = (str, int, float, type(None))


class SchemaDriftError(Exception):
    def __init__(self, endpoint_id: str, problems: list[str]):
        self.endpoint_id = endpoint_id
        self.problems = problems
        super().__init__(f"{endpoint_id}: " + "; ".join(problems[:6]))


@dataclass
class SchemaReport:
    endpoint_id: str
    rows: int
    problems: list
    new_fields: list  # fields present in the feed that the contract does not list: informational only

    @property
    def ok(self) -> bool:
        return not self.problems


def check_payload(ep: Endpoint, payload: Any) -> SchemaReport:
    problems: list[str] = []
    new_fields: list[str] = []

    if ep.kind == "swagger":
        if not isinstance(payload, dict):
            problems.append("swagger payload is not an object")
        else:
            for f in ep.required_fields:
                if f not in payload:
                    problems.append(f"missing top-level key {f!r}")
            if isinstance(payload.get("paths"), dict) and not payload["paths"]:
                problems.append("swagger has no paths")
        return SchemaReport(ep.id, 0, problems, new_fields)

    if not isinstance(payload, list):
        return SchemaReport(ep.id, 0, [f"expected a JSON array, got {type(payload).__name__}"], new_fields)
    rows = len(payload)
    if rows == 0:
        problems.append("response is empty")
    elif rows < ep.min_rows:
        problems.append(f"only {rows} rows, expected at least {ep.min_rows} (response suddenly small)")

    # A required entry may be a tuple of aliases (the exchanges name the same field differently across feeds): any one will do.
    required = set()
    for spec in ep.required_fields:
        required.update(spec if isinstance(spec, tuple) else (spec,))
    missing_counts: dict[str, int] = {}
    bad_type = 0
    not_dict = 0
    seen_fields: set[str] = set()
    for rec in payload:
        if not isinstance(rec, dict):
            not_dict += 1
            continue
        seen_fields.update(rec.keys())
        for spec in ep.required_fields:
            alts = spec if isinstance(spec, tuple) else (spec,)
            if not any(a in rec for a in alts):
                key = "/".join(alts)
                missing_counts[key] = missing_counts.get(key, 0) + 1
        for v in rec.values():
            if not isinstance(v, _SCALARS):
                bad_type += 1
                break
    if not_dict:
        problems.append(f"{not_dict} records are not objects")
    for f, c in sorted(missing_counts.items()):
        problems.append(f"required field {f!r} missing in {c}/{rows} records (renamed or removed?)")
    if bad_type:
        problems.append(f"{bad_type} records contain nested/unexpected value types")
    new_fields = sorted(seen_fields - required)
    return SchemaReport(ep.id, rows, problems, new_fields)


def require_ok(ep: Endpoint, payload: Any) -> SchemaReport:
    report = check_payload(ep, payload)
    if not report.ok:
        raise SchemaDriftError(ep.id, report.problems)
    return report
