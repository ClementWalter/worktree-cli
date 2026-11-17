#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.1",
#     "pytest==9.0.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-11-03T00:00:00Z"
# ///
"""Offline tests for worktree_cli: the gc verdict, porcelain parsing, and real git round trips."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worktree_cli as wt  # noqa: E402  (import follows the sys.path setup above)

# Deterministic identity, no signing or global hooks, so commits succeed anywhere.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV}).stdout.strip()


@pytest.fixture(autouse=True)
def git_env(monkeypatch):
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A main checkout with one commit, pushed to a bare origin."""
    origin = tmp_path / "origin.git"
    run(tmp_path, "init", "--bare", "-b", "main", str(origin))
    main = tmp_path / "src" / "proj"
    main.mkdir(parents=True)
    run(main, "init", "-b", "main")
    (main / "a.txt").write_text("a\n")
    run(main, "add", ".")
    run(main, "commit", "-m", "init")
    run(main, "remote", "add", "origin", str(origin))
    run(main, "push", "-u", "origin", "main")
    run(main, "remote", "set-head", "origin", "main")
    return main


@pytest.fixture()
def wt_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "worktrees"
    monkeypatch.setattr(wt, "WT_ROOT", root)
    return root


@pytest.fixture()
def stub_gh(monkeypatch):
    """No GitHub: every branch has no PR."""
    monkeypatch.setattr(wt, "pr_for", lambda repo, branch: (None, None))


BASE = wt.Facts(dirty=False, unpushed=0, merged=False, pr_state=None, pr_head=None,
                head="abc", detached=False, live_lock=False, idle_hours=48)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"live_lock": True, "merged": True}, False),
        ({"dirty": True, "merged": True}, False),
        ({"idle_hours": 1, "merged": True}, False),
        ({"merged": True}, True),
        ({"unpushed": 2, "merged": True}, False),
        ({"pr_state": "MERGED", "pr_head": "abc", "unpushed": 3}, True),
        ({"pr_state": "CLOSED", "pr_head": "abc"}, True),
        ({"pr_state": "CLOSED", "pr_head": "other", "unpushed": 1}, False),
        ({"pr_state": "OPEN", "pr_head": "abc"}, False),
        ({"detached": True}, True),
        ({}, False),
    ],
)
def test_verdict_removable(changes, expected):
    facts = wt.Facts(**{**BASE.__dict__, **changes})
    assert wt.verdict(facts)[0] is expected


def test_parse_worktree_list_drops_main_and_reads_fields():
    porcelain = (
        "worktree /r\nHEAD 111\nbranch refs/heads/main\n\n"
        "worktree /w1\nHEAD 222\nbranch refs/heads/feat/x\nlocked claude agent a (pid 1 start x)\n\n"
        "worktree /w2\nHEAD 333\ndetached\nprunable gitdir file points to non-existent location\n"
    )
    assert wt.parse_worktree_list(porcelain) == [
        wt.Worktree(Path("/w1"), "222", "feat/x", "claude agent a (pid 1 start x)", False),
        wt.Worktree(Path("/w2"), "333", None, None, True),
    ]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (None, False),
        ("manual lock", True),
        (f"claude agent x (pid {os.getpid()} start y)", True),
        ("claude agent x (pid 999999 start y)", False),
    ],
)
def test_lock_is_live(reason, expected):
    assert wt.lock_is_live(reason) is expected


@pytest.mark.parametrize(("name", "expected"), [("feat/x", "feat-x"), ("a b//c", "a-b-c")])
def test_slug(name, expected):
    assert wt.slug(name) == expected


def test_new_creates_worktree_under_root(repo, wt_root):
    result = CliRunner().invoke(wt.cli, ["new", "feat/x", "--repo", str(repo)])
    assert Path(result.output.strip()) == wt_root / "proj" / "feat-x"


def test_new_checks_out_the_new_branch(repo, wt_root):
    path = Path(CliRunner().invoke(wt.cli, ["new", "feat/x", "--repo", str(repo)]).output.strip())
    assert run(path, "branch", "--show-current") == "feat/x"


def test_new_refuses_existing_target(repo, wt_root):
    CliRunner().invoke(wt.cli, ["new", "feat/x", "--repo", str(repo)])
    result = CliRunner().invoke(wt.cli, ["new", "feat/x", "--repo", str(repo)])
    assert "already exists" in result.output


def test_hook_create_prints_path_last(repo, wt_root):
    payload = json.dumps({"name": "agent-1", "cwd": str(repo)})
    result = CliRunner().invoke(wt.cli, ["hook", "create"], input=payload)
    assert Path(result.output.strip().splitlines()[-1]).is_dir()


def test_rm_refuses_dirty_worktree(repo, wt_root, stub_gh):
    path = wt.create_worktree(repo, "feat/x", None, None)
    (path / "a.txt").write_text("changed\n")
    result = CliRunner().invoke(wt.cli, ["rm", str(path)])
    assert "uncommitted changes" in result.output


def test_rm_force_removes_dirty_worktree(repo, wt_root, stub_gh):
    path = wt.create_worktree(repo, "feat/x", None, None)
    (path / "a.txt").write_text("changed\n")
    CliRunner().invoke(wt.cli, ["rm", "--force", str(path)])
    assert not path.exists()


def test_rm_removes_fresh_clean_worktree(repo, wt_root, stub_gh):
    path = wt.create_worktree(repo, "feat/x", None, None)
    CliRunner().invoke(wt.cli, ["rm", str(path)])
    assert not path.exists()


def test_new_prints_normalized_path(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "WT_ROOT", tmp_path / "x" / ".." / "worktrees")
    result = CliRunner().invoke(wt.cli, ["new", "feat/x", "--repo", str(repo)])
    assert ".." not in result.output


def test_hook_remove_deletes_worktree(repo, wt_root):
    path = wt.create_worktree(repo, "agent-2", None, None)
    CliRunner().invoke(wt.cli, ["hook", "remove"], input=json.dumps({"worktree_path": str(path)}))
    assert not path.exists()


def test_survey_keeps_fresh_merged_worktree(repo, wt_root, stub_gh):
    wt.create_worktree(repo, "feat/x", None, None)
    [row] = wt.survey([repo], fetch=False, size=False)
    assert row.reason == f"active in the last {wt.MIN_IDLE_HOURS}h"


def test_survey_marks_idle_merged_worktree_removable(repo, wt_root, stub_gh, monkeypatch):
    wt.create_worktree(repo, "feat/x", None, None)
    monkeypatch.setattr(wt, "MIN_IDLE_HOURS", 0)
    [row] = wt.survey([repo], fetch=False, size=False)
    assert row.removable


def test_survey_respects_protected_paths(repo, wt_root, stub_gh, monkeypatch):
    wt.create_worktree(repo, "feat/x", None, None)
    monkeypatch.setattr(wt, "PROTECT", [wt_root])
    [row] = wt.survey([repo], fetch=False, size=False)
    assert row.reason == "protected (WT_PROTECT)"


def test_discover_repos_finds_repo_with_worktree(repo, wt_root):
    wt.create_worktree(repo, "feat/x", None, None)
    assert wt.discover_repos([repo.parent.parent]) == [repo.resolve()]


def test_stale_trunk_caches_selects_idle_dir(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "f").write_text("x")
    os.utime(old / "f", (0, 0))
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "f").write_text("x")
    assert wt.stale_trunk_caches(tmp_path, 7, time.time()) == [old]


def test_gc_dry_run_keeps_worktree_and_prints_json(repo, wt_root, stub_gh, monkeypatch):
    path = wt.create_worktree(repo, "feat/x", None, None)
    monkeypatch.setattr(wt, "MIN_IDLE_HOURS", 0)
    monkeypatch.setattr(wt, "SCAN_ROOTS", [repo.parent.parent])
    monkeypatch.setattr(wt, "TRUNK_REPOS", repo.parent / "none")
    monkeypatch.setattr(wt, "ANVIL_TMP", repo.parent / "none")
    out = json.loads(CliRunner().invoke(wt.cli, ["gc", "--json"]).output)
    assert (out["applied"], out["worktrees"][0]["path"], path.exists()) == (False, str(path), True)


def test_gc_yes_removes_idle_merged_worktree(repo, wt_root, stub_gh, monkeypatch):
    path = wt.create_worktree(repo, "feat/x", None, None)
    monkeypatch.setattr(wt, "MIN_IDLE_HOURS", 0)
    monkeypatch.setattr(wt, "SCAN_ROOTS", [repo.parent.parent])
    monkeypatch.setattr(wt, "TRUNK_REPOS", repo.parent / "none")
    monkeypatch.setattr(wt, "ANVIL_TMP", repo.parent / "none")
    CliRunner().invoke(wt.cli, ["gc", "--yes"])
    assert not path.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
