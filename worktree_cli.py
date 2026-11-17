#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-11-03T00:00:00Z"
# ///
"""wt: create git worktrees in one place and garbage-collect what they leave behind.

A worktree costs gigabytes: forge `out/`, soldeer deps, and a trunk cache in
`~/.cache/trunk/repos/<hash>` that outlives the checkout. Worktrees made ad hoc
(sibling folders, a session scratchpad, Claude agent isolation) are never removed.

- `wt new` puts every worktree under `$WT_ROOT/<repo>/<slug>` (default `~/worktrees`).
- `wt rm` removes one, refusing to drop uncommitted or unpushed work.
- `wt gc` removes every worktree whose work is safe elsewhere (merged, or its PR is
  closed at the same commit), prunes stale registrations, and deletes idle trunk
  caches and orphaned anvil state. Dry run unless `--yes`.
- `wt hook create|remove` implement Claude Code's WorktreeCreate/WorktreeRemove hooks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import click

logger = logging.getLogger("wt")

WT_ROOT = Path(os.environ.get("WT_ROOT", str(Path.home() / "worktrees")))
SCAN_ROOTS = [
    Path(p).expanduser()
    for p in os.environ.get("WT_SCAN_ROOTS", str(Path.home() / "Documents")).split(":")
    if p
]
# Repos sit at most this deep under a scan root (~/Documents/org/repo = 2).
SCAN_DEPTH = 3
CACHE_DIR = Path.home() / ".cache" / "worktree-cli"
TRUNK_REPOS = Path(os.environ.get("WT_TRUNK_REPOS", str(Path.home() / ".cache" / "trunk" / "repos")))
ANVIL_TMP = Path(os.environ.get("WT_ANVIL_TMP", str(Path.home() / ".foundry" / "anvil" / "tmp")))
# Worktrees other tools keep as long-lived workspaces (e.g. inbox workers); gc reports them only.
PROTECT = [
    Path(p).expanduser()
    for p in os.environ.get("WT_PROTECT", str(Path.home() / ".cache")).split(":")
    if p
]
# A worktree touched more recently than this may belong to a session still setting it up.
MIN_IDLE_HOURS = 24
TRUNK_IDLE_DAYS = 7
GC_MIN_INTERVAL_S = 3600
GIT_TIMEOUT_S = 120
# Registered worktree whose checkout is gone; `git worktree prune` drops it.
GONE = "folder already gone"


# --- git plumbing -------------------------------------------------------------------


def git(repo: Path, *args: str, check: bool = True, timeout: int = GIT_TIMEOUT_S) -> str:
    """Run git in `repo` and return stripped stdout."""
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and res.returncode != 0:
        raise click.ClickException(f"git {' '.join(args)} failed in {repo}: {res.stderr.strip()}")
    return res.stdout.strip()


def main_checkout(path: Path) -> Path:
    """The main checkout owning `path`, which may itself be a linked worktree."""
    common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common).parent


@dataclass
class Worktree:
    """One linked worktree as `git worktree list --porcelain` reports it."""

    path: Path
    head: str
    branch: str | None
    lock_reason: str | None
    prunable: bool


def parse_worktree_list(porcelain: str) -> list[Worktree]:
    """Parse `git worktree list --porcelain`, dropping the main checkout (listed first)."""
    blocks = [b for b in porcelain.strip().split("\n\n") if b.strip()]
    out: list[Worktree] = []
    for block in blocks[1:]:
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        branch = fields.get("branch")
        out.append(
            Worktree(
                path=Path(fields["worktree"]),
                head=fields.get("HEAD", ""),
                branch=branch.removeprefix("refs/heads/") if branch else None,
                lock_reason=fields["locked"] if "locked" in fields else None,
                prunable="prunable" in fields,
            )
        )
    return out


def lock_is_live(reason: str | None) -> bool:
    """A lock holds while its owner lives; Claude writes `(pid N ...)` into its reason."""
    if reason is None:
        return False
    m = re.search(r"\(pid (\d+)", reason)
    if not m:
        return True
    try:
        os.kill(int(m.group(1)), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def discover_repos(roots: list[Path], depth: int = SCAN_DEPTH) -> list[Path]:
    """Main checkouts under `roots` that have at least one linked worktree registered."""
    found: set[Path] = set()
    for root in roots:
        for d in range(depth):
            pattern = "/".join(["*"] * (d + 1)) + "/.git/worktrees"
            for wt_dir in root.glob(pattern):
                if wt_dir.is_dir() and any(wt_dir.iterdir()):
                    found.add(wt_dir.parent.parent.resolve())
    return sorted(found)


# --- safety verdict -----------------------------------------------------------------


@dataclass
class Facts:
    """What decides whether a worktree can go without losing work."""

    dirty: bool
    unpushed: int
    merged: bool
    pr_state: str | None
    pr_head: str | None
    head: str
    detached: bool
    live_lock: bool
    idle_hours: float


def verdict(f: Facts) -> tuple[bool, str]:
    """Return (removable, reason). Removable only when no work exists solely here."""
    if f.live_lock:
        return False, "locked by a running process"
    if f.dirty:
        return False, "uncommitted changes"
    if f.idle_hours < MIN_IDLE_HOURS:
        return False, f"active in the last {MIN_IDLE_HOURS}h"
    if f.pr_state in ("MERGED", "CLOSED") and f.pr_head == f.head:
        return True, f"PR {f.pr_state.lower()} at this commit"
    if f.unpushed:
        return False, f"{f.unpushed} unpushed commit(s)"
    if f.merged:
        return True, "merged into the default branch"
    if f.detached:
        return True, "detached HEAD already on a remote"
    if f.pr_state == "OPEN":
        return False, "PR open"
    return False, "pushed but not merged"


def default_ref(repo: Path) -> str | None:
    """`origin/<default>` or None when the repo has no origin HEAD."""
    ref = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    return ref.removeprefix("refs/remotes/") or None


def pr_for(repo: Path, branch: str) -> tuple[str | None, str | None]:
    """(state, headRefOid) of the latest PR for `branch`; (None, None) without gh or PR."""
    if not shutil.which("gh"):
        return None, None
    res = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1",
         "--json", "state,headRefOid"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        logger.warning("gh pr list failed in %s: %s", repo, res.stderr.strip())
        return None, None
    prs = json.loads(res.stdout or "[]")
    if not prs:
        return None, None
    return prs[0]["state"], prs[0]["headRefOid"]


def gather_facts(repo: Path, wt: Worktree, base: str | None) -> Facts:
    """Read git and GitHub state for one existing worktree."""
    gitdir = Path(git(wt.path, "rev-parse", "--absolute-git-dir"))
    touched = [p.stat().st_mtime for p in (gitdir / "HEAD", gitdir / "index", gitdir / "logs" / "HEAD")
               if p.exists()]
    idle_hours = (time.time() - max(touched, default=0)) / 3600
    # --no-optional-locks keeps status from rewriting the index, whose mtime is the idle clock.
    dirty = bool(git(wt.path, "--no-optional-locks", "status", "--porcelain", check=False))
    unpushed = int(git(wt.path, "rev-list", "--count", "HEAD", "--not", "--remotes", check=False) or 0)
    merged = bool(base) and subprocess.run(
        ["git", "-C", str(wt.path), "merge-base", "--is-ancestor", "HEAD", base],
        capture_output=True,
    ).returncode == 0
    pr_state, pr_head = pr_for(repo, wt.branch) if wt.branch and not merged else (None, None)
    return Facts(
        dirty=dirty, unpushed=unpushed, merged=merged, pr_state=pr_state, pr_head=pr_head,
        head=wt.head, detached=wt.branch is None, live_lock=lock_is_live(wt.lock_reason),
        idle_hours=idle_hours,
    )


@dataclass
class Row:
    """One worktree with its gc verdict, as `list` and `gc` print it."""

    repo: str
    path: str
    branch: str | None
    removable: bool
    reason: str
    size_mb: int | None = None


def survey(repos: list[Path], fetch: bool, size: bool) -> list[Row]:
    """Verdict for every linked worktree of `repos`; prunable entries are reported too."""
    rows: list[Row] = []
    for repo in repos:
        if fetch:
            res = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "--prune", "origin"],
                                 capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
            if res.returncode != 0:
                logger.warning("fetch failed in %s: %s", repo, res.stderr.strip())
        base = default_ref(repo)
        for wt in parse_worktree_list(git(repo, "worktree", "list", "--porcelain")):
            if wt.prunable or not wt.path.is_dir():
                rows.append(Row(str(repo), str(wt.path), wt.branch, True, GONE))
                continue
            if any(wt.path.is_relative_to(p) for p in PROTECT):
                rows.append(Row(str(repo), str(wt.path), wt.branch, False, "protected (WT_PROTECT)"))
                continue
            ok, why = verdict(gather_facts(repo, wt, base))
            rows.append(Row(str(repo), str(wt.path), wt.branch, ok, why,
                            dir_size_mb(wt.path) if size else None))
    return rows


def dir_size_mb(path: Path) -> int:
    """Disk usage via `du`, which counts APFS clones at full size (an upper bound)."""
    res = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True)
    return int(res.stdout.split()[0]) // 1024 if res.returncode == 0 and res.stdout else 0


# --- side caches --------------------------------------------------------------------


def stale_trunk_caches(root: Path, idle_days: int, now: float) -> list[Path]:
    """Trunk per-checkout caches with no live daemon and no write for `idle_days`.

    Trunk names them by an opaque hash, so the owning checkout is unknown; an idle
    cache is only a cache, and trunk rebuilds it on the next run.
    """
    if not root.is_dir():
        return []
    stale = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        pid_file = d / "server.pid"
        if pid_file.is_file():
            pid = pid_file.read_text().strip()
            if pid.isdigit() and _alive(int(pid)):
                continue
        newest = max((p.stat().st_mtime for p in d.rglob("*") if p.is_file()), default=d.stat().st_mtime)
        if now - newest > idle_days * 86400:
            stale.append(d)
    return stale


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def orphan_anvil_state(root: Path) -> list[Path]:
    """Anvil state dirs left by killed anvils; none while any anvil is running."""
    if not root.is_dir():
        return []
    if subprocess.run(["pgrep", "-x", "anvil"], capture_output=True).returncode == 0:
        return []
    return sorted(root.iterdir())


def remove_path(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


# --- actions ------------------------------------------------------------------------


def slug(name: str) -> str:
    """Folder name for a branch: `feat/x` -> `feat-x`."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")


def create_worktree(repo: Path, name: str, base: str | None, branch: str | None) -> Path:
    """Create `$WT_ROOT/<repo>/<slug>` on `branch` (new from `base`, or existing)."""
    main = main_checkout(repo)
    branch = branch or name
    target = WT_ROOT.expanduser().resolve() / main.name / slug(name)
    if target.exists():
        raise click.ClickException(f"{target} already exists; pick another name or `wt rm {target}`")
    target.parent.mkdir(parents=True, exist_ok=True)
    exists = subprocess.run(["git", "-C", str(main), "show-ref", "--verify", "--quiet",
                             f"refs/heads/{branch}"]).returncode == 0
    if exists:
        git(main, "worktree", "add", str(target), branch)
    else:
        git(main, "worktree", "add", "-b", branch, str(target), base or "HEAD")
    return target


def remove_worktree(path: Path, force: bool) -> str:
    """Remove the worktree at `path`; refuse when it holds the only copy of some work."""
    path = path.resolve()
    if not path.is_dir():
        raise click.ClickException(f"{path} is not a directory; `wt list` shows the worktrees")
    repo = main_checkout(path)
    if path == repo:
        raise click.ClickException(f"{path} is a main checkout, not a linked worktree")
    wt = next((w for w in parse_worktree_list(git(repo, "worktree", "list", "--porcelain"))
               if w.path.resolve() == path), None)
    if wt is None:
        raise click.ClickException(f"{path} is not a worktree of {repo}")
    if not force:
        facts = gather_facts(repo, wt, default_ref(repo))
        # The idle grace guards against gc racing a session, not against an explicit rm.
        facts.idle_hours = float("inf")
        ok, why = verdict(facts)
        if not ok:
            raise click.ClickException(f"kept {path}: {why}; pass --force to discard it")
    if wt.lock_reason is not None:
        git(repo, "worktree", "unlock", str(path), check=False)
    git(repo, "worktree", "remove", "--force", str(path))
    return str(path)


# --- CLI ----------------------------------------------------------------------------


@click.group(epilog="\b\nExamples:\n  wt new feat/x\n  wt list\n  wt gc --yes")
@click.option("--debug", is_flag=True, help="Log at DEBUG level on stderr.")
def cli(debug: bool) -> None:
    """Create worktrees in one place and garbage-collect what they leave behind."""
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if debug else logging.INFO,
                        format="%(message)s")


@cli.command(epilog="\b\nExamples:\n  wt new feat/redeem-warmup\n"
             "  wt new fix/x --base origin/main\n  cd \"$(wt new feat/y | tail -1)\"")
@click.argument("name")
@click.option("--repo", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=Path.cwd, show_default="cwd", help="Any checkout of the repo.")
@click.option("--base", help="Start point for a new branch.  [default: HEAD]")
@click.option("--branch", help="Branch name, when it differs from NAME.")
def new(name: str, repo: Path, base: str | None, branch: str | None) -> None:
    """Create a worktree at $WT_ROOT/<repo>/<slug of NAME> and print its path."""
    click.echo(create_worktree(repo, name, base, branch))


@cli.command("list", epilog="\b\nExamples:\n  wt list\n  wt list --size\n  wt list --json | jq '.[] | select(.removable)'")
@click.option("--json", "as_json", is_flag=True, help="Normalized JSON on stdout.")
@click.option("--size", is_flag=True, help="Add disk usage (du; slow on large trees).")
@click.option("--fetch/--no-fetch", default=False, help="Fetch origin first for fresh merge state.")
def list_cmd(as_json: bool, size: bool, fetch: bool) -> None:
    """Every linked worktree under the scan roots, with what gc would do to it."""
    rows = survey(discover_repos(SCAN_ROOTS), fetch=fetch, size=size)
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2))
        return
    for r in rows:
        mark = "rm  " if r.removable else "keep"
        sz = f"\t{r.size_mb}M" if r.size_mb is not None else ""
        click.echo(f"{mark}\t{r.branch or '(detached)'}\t{r.reason}{sz}\t{r.path}")


@cli.command(epilog="\b\nExamples:\n  wt rm ~/worktrees/confidential-defi/feat-x\n  wt rm . --force")
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Remove even with uncommitted or unpushed work.")
def rm(paths: tuple[Path, ...], force: bool) -> None:
    """Remove worktrees; refuses one that holds uncommitted or unpushed work."""
    for p in paths:
        click.echo(remove_worktree(p, force))


@cli.command(epilog="\b\nExamples:\n  wt gc            # dry run\n  wt gc --yes\n"
             "  wt gc --yes --background   # SessionStart hook")
@click.option("--yes", is_flag=True, help="Delete; without it, print the plan only.")
@click.option("--json", "as_json", is_flag=True, help="Normalized JSON on stdout.")
@click.option("--background", is_flag=True,
              help=f"Detach, log to {CACHE_DIR}/gc.log, at most once per {GC_MIN_INTERVAL_S // 60} min.")
def gc(yes: bool, as_json: bool, background: bool) -> None:
    """Remove safe worktrees, stale registrations, idle trunk caches and orphan anvil state."""
    if background:
        _spawn_background()
        return
    repos = discover_repos(SCAN_ROOTS)
    rows = survey(repos, fetch=True, size=False)
    trunk = stale_trunk_caches(TRUNK_REPOS, TRUNK_IDLE_DAYS, time.time())
    anvil = orphan_anvil_state(ANVIL_TMP)
    plan = {
        "worktrees": [asdict(r) for r in rows if r.removable],
        "kept": [asdict(r) for r in rows if not r.removable],
        "trunk_caches": [str(p) for p in trunk],
        "anvil_state": [str(p) for p in anvil],
    }
    if yes:
        for r in rows:
            if r.removable and r.reason != GONE:
                try:
                    remove_worktree(Path(r.path), force=False)
                except click.ClickException as e:
                    logger.warning("%s", e.message)
        for repo in repos:
            git(repo, "worktree", "prune", check=False)
        for p in [*trunk, *anvil]:
            remove_path(p)
    if as_json:
        click.echo(json.dumps({"applied": yes, **plan}, indent=2))
        return
    verb = "removed" if yes else "would remove"
    for r in plan["worktrees"]:
        click.echo(f"{verb}\tworktree\t{r['reason']}\t{r['path']}")
    for p in plan["trunk_caches"]:
        click.echo(f"{verb}\ttrunk-cache\tidle {TRUNK_IDLE_DAYS}d+\t{p}")
    for p in plan["anvil_state"]:
        click.echo(f"{verb}\tanvil-state\tno anvil running\t{p}")
    for r in plan["kept"]:
        click.echo(f"kept\tworktree\t{r['reason']}\t{r['path']}")


def _spawn_background() -> None:
    """Fork `wt gc --yes` detached, so a SessionStart hook returns at once."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = CACHE_DIR / "gc.last"
    if stamp.exists() and time.time() - stamp.stat().st_mtime < GC_MIN_INTERVAL_S:
        return
    stamp.touch()
    log = (CACHE_DIR / "gc.log").open("a")
    log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
    log.flush()
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "gc", "--yes"],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


@cli.group(hidden=True, epilog="\b\nExamples:\n  echo '{\"name\":\"a\",\"cwd\":\".\"}' | wt hook create")
def hook() -> None:
    """Claude Code WorktreeCreate/WorktreeRemove hooks (JSON on stdin)."""


@hook.command("create", epilog="\b\nExamples:\n  echo '{\"name\":\"agent-1\",\"cwd\":\"/repo\"}' | wt hook create")
def hook_create() -> None:
    """Read {name, cwd}; create the worktree; print its absolute path as the last line."""
    payload = json.load(sys.stdin)
    click.echo(create_worktree(Path(payload["cwd"]), payload["name"], None, None))


@hook.command("remove", epilog="\b\nExamples:\n  echo '{\"worktree_path\":\"/wt/agent-1\"}' | wt hook remove")
def hook_remove() -> None:
    """Read {worktree_path}; remove it. Claude already decided, so no safety check."""
    payload = json.load(sys.stdin)
    remove_worktree(Path(payload["worktree_path"]), force=True)


if __name__ == "__main__":
    cli()
