"""Compare three outlier policies on the published market snapshot, per industry, with the COMPANIES that each policy removes.

  METHOD A  RAW             every positive official P/E
  METHOD B  CURRENT_ROBUST  Tukey fence k=3 on positive P/E, only when n >= 8   (ROBUST_IQR_3_V1, the production policy)
  METHOD C  LEGACY_ROBUST   a fixed-cap screen used before: keep 0 < P/E < 200, then (if >= 3 remain) Tukey fence k=1.5 on what remains
                            (kept only so the difference can be audited)

    python scripts/outlier_comparison.py [--snapshot DIR] [--out report.md]

Read-only analysis. It changes nothing in production.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

INDUSTRIES = [("24", "半導體業"), ("28", "電子零組件業"), ("22", "生技醫療業"), ("02", "食品工業"), ("14", "建材營造業"), ("17", "金融保險業"), ("32", "文化創意業")]


def pct(s: list[float], p: float) -> float:
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def tukey(items: list[tuple[str, float]], k: float) -> tuple[list, list, tuple | None]:
    vals = sorted(v for _, v in items)
    q1, q3 = pct(vals, 0.25), pct(vals, 0.75)
    lo, hi = q1 - k * (q3 - q1), q3 + k * (q3 - q1)
    return [x for x in items if lo <= x[1] <= hi], [x for x in items if not lo <= x[1] <= hi], (lo, hi)


def method_a(items):
    return list(items), [], None


def method_b(items):
    if len(items) < 8:
        return list(items), [], None
    return tukey(items, 3.0)


def method_c(items):
    capped = [x for x in items if 0 < x[1] < 200]
    over = [x for x in items if x[1] >= 200]
    if len(capped) < 3:
        return capped, over, None
    kept, out, fences = tukey(capped, 1.5)
    return kept, over + out, fences


METHODS = {"A RAW": method_a, "B CURRENT_ROBUST (k=3, n>=8)": method_b, "C LEGACY (<200 then k=1.5)": method_c}


def describe(kept: list[tuple[str, float]]) -> dict | None:
    if not kept:
        return None
    s = sorted(v for _, v in kept)
    return {"n": len(s), "p25": pct(s, 0.25), "median": pct(s, 0.5), "mean": sum(s) / len(s), "p75": pct(s, 0.75), "min": s[0], "max": s[-1]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=str(Path(__file__).resolve().parent.parent / "out"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    root = Path(args.snapshot)
    m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    mk = json.loads((root / m["files"]["market-snapshot.json"]["path"]).read_text(encoding="utf-8"))
    names = {c["ticker"]: c.get("shortName") or c["name"] for c in mk["companies"]}
    lines = [f"# Outlier policy comparison  (release {m['release']}, TWSE {m['marketAsOf']['TWSE']}, TPEx {m['marketAsOf']['TPEX']})", ""]
    summary = {}
    for code, label in INDUSTRIES:
        comps = [c for c in mk["companies"] if c["industry"]["code"] == code]
        if not comps:
            lines += [f"## {label} ({code}): not found", ""]
            continue
        items = [(c["ticker"], c["officialPE"]["value"]) for c in comps if c["officialPE"]["value"] is not None and c["officialPE"]["value"] > 0]
        lines += [f"## {label} ({code}): {len(comps)} companies, {len(items)} with a positive official P/E", "",
                  "| method | n | excluded | P25 | median | mean | P75 | min | max |", "|---|---|---|---|---|---|---|---|---|"]
        res = {}
        for name, fn in METHODS.items():
            kept, removed, fences = fn(items)
            d = describe(kept)
            res[name] = (kept, removed, fences, d)
            f = lambda x: "—" if x is None else f"{x:.1f}"
            lines.append(f"| {name} | {d['n'] if d else 0} | {len(removed)} | {f(d and d['p25'])} | {f(d and d['median'])} | {f(d and d['mean'])} | {f(d and d['p75'])} | {f(d and d['min'])} | {f(d and d['max'])} |")
        b = {t for t, _ in res["B CURRENT_ROBUST (k=3, n>=8)"][1]}
        c = {t for t, _ in res["C LEGACY (<200 then k=1.5)"][1]}
        pe = dict(items)
        fmt = lambda ts: ", ".join(f"{t} {names[t]} ({pe[t]:.1f})" for t in sorted(ts, key=lambda t: -pe[t])) or "—"
        lines += ["", f"- removed ONLY by C (legacy): {len(c - b)}  {fmt(c - b)}", f"- removed ONLY by B (current): {len(b - c)}  {fmt(b - c)}", f"- removed by BOTH: {len(b & c)}  {fmt(b & c)}", ""]
        for name in ("C LEGACY (<200 then k=1.5)",):
            fences = res[name][2]
            if fences:
                lines.append(f"- legacy fences after the <200 cap: [{fences[0]:.1f}, {fences[1]:.1f}]")
        fb = res["B CURRENT_ROBUST (k=3, n>=8)"][2]
        if fb:
            lines.append(f"- current fences: [{fb[0]:.1f}, {fb[1]:.1f}]")
        lines.append("")
        summary[label] = {k: v[3] for k, v in res.items()}
    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
