"""Safe publication of explicit paths to the `data` branch (Phase 3I.2).

Two independent workflows (TWSE/TPEx and 興櫃) push disjoint paths of the same branch. GitHub's concurrency groups are not relied on for safety:
the publication code itself looks at the remote.

  1. fetch BEFORE anything is mutated; if the local branch is behind the remote, fast-forward only (a diverged local branch is a hard stop);
  2. stage ONLY the explicit paths given (never `add -A`), commit;
  3. fetch again; if the remote moved: it may be reconciled ONLY when nothing it changed touches `guard` (the namespace this publisher owns);
     otherwise fail closed. Reconcile = rebase our single commit onto the remote head, then push;
  4. push without force; verify with `ls-remote` that the remote head IS our head. Never "assume it worked".
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable, Optional


class PublishRace(Exception):
    """The remote moved in a way that cannot be reconciled safely. Nothing was pushed."""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def _is_ancestor(repo: Path, a: str, b: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b], capture_output=True).returncode == 0


def _touches(changed: list[str], guard: list[str]) -> list[str]:
    """Changed paths inside a namespace this publisher owns ("emerging/" = everything below it; "manifest.json" = exactly that file)."""
    return [c for c in changed if any((c == g.rstrip("/")) or (g.endswith("/") and c.startswith(g)) for g in guard)]


def publish_paths(repo: Path, branch: str, paths: list[str], message: str, guard: list[str], remote: str = "origin", retries: int = 3,
                  sleep: Callable = time.sleep) -> dict:
    repo = Path(repo)
    if not paths:
        raise ValueError("publish_paths needs explicit paths")
    if any(p in (".", "", "*") or p.startswith("-") for p in paths):
        raise ValueError("explicit paths only: never '.', '*' or a flag")
    _git(repo, "fetch", "-q", remote, branch)
    head, rhead = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", f"{remote}/{branch}")
    if head != rhead:
        if _is_ancestor(repo, head, rhead):
            clash = _touches(_git(repo, "diff", "--name-only", head, rhead).splitlines(), guard)
            if clash:
                raise PublishRace(f"the remote moved and changed paths this publisher owns: {clash[:3]}; nothing was pushed")
            try:
                _git(repo, "merge", "--ff-only", "-q", f"{remote}/{branch}")  # behind: fast-forward only
            except RuntimeError as e:
                raise PublishRace(f"the local data branch could not be fast-forwarded: {e}") from e
        else:
            raise PublishRace("the local data branch has diverged from the remote before anything was staged")
    _git(repo, "add", "--", *paths)
    if not _git(repo, "diff", "--cached", "--name-only", "--", *paths):
        return {"pushed": False, "reason": "NOTHING_TO_COMMIT", "head": _git(repo, "rev-parse", "HEAD"), "attempts": 0, "rebased": False}
    staged = _git(repo, "diff", "--cached", "--name-only").splitlines()
    outside = [s for s in staged if not any(s == p or s.startswith(p.rstrip("/") + "/") for p in paths)]
    if outside:
        _git(repo, "reset", "-q", "--", *outside)
        raise PublishRace(f"something outside the explicit paths was staged: {outside[:3]}")
    _git(repo, "commit", "-q", "-m", message)
    rebased = False
    for attempt in range(1, retries + 1):
        _git(repo, "fetch", "-q", remote, branch)
        rhead, mine = _git(repo, "rev-parse", f"{remote}/{branch}"), _git(repo, "rev-parse", "HEAD")
        if not _is_ancestor(repo, rhead, mine):
            base = _git(repo, "merge-base", mine, rhead)
            changed = _git(repo, "diff", "--name-only", base, rhead).splitlines()
            clash = _touches(changed, guard)
            if clash:
                raise PublishRace(f"the remote moved and changed paths this publisher owns: {clash[:3]}; nothing was pushed")
            _git(repo, "rebase", "-q", "--autostash", f"{remote}/{branch}")  # the pointer files (manifest.json / health.json) are still modified in the tree: keep them  # disjoint paths: replay our one commit on top
            rebased = True
        r = subprocess.run(["git", "-C", str(repo), "push", "-q", remote, f"HEAD:{branch}"], capture_output=True, text=True, encoding="utf-8")  # never --force
        if r.returncode == 0:
            ours = _git(repo, "rev-parse", "HEAD")
            remote_head = _git(repo, "ls-remote", remote, f"refs/heads/{branch}").split()[0]
            if remote_head != ours:
                raise PublishRace(f"push reported success but the remote head is {remote_head[:12]}, not {ours[:12]}")
            return {"pushed": True, "head": ours, "attempts": attempt, "rebased": rebased}
        sleep(2 * attempt)  # rejected (the remote moved between fetch and push): look again
    raise PublishRace(f"could not push after {retries} attempts (the remote kept moving)")
