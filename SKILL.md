---
name: worktree-cli
description:
  Create git worktrees in one place and garbage-collect what they leave behind
  (worktree folders, stale registrations, idle trunk caches, orphaned anvil
  state) with the `wt` command. Use whenever you need a worktree for branch
  work, instead of a raw `git worktree add`, and when the disk fills up with
  worktree build output. Triggers - "new worktree", "work in a worktree", "clean
  up worktrees", "worktrees eat my disk", "wt gc".
---

# worktree-cli

## How to invoke

`wt` is on `$PATH` through `~/.local/bin/wt` (run
`uv run ~/Documents/claudine/scripts/link_clis.py` if it is missing). Fallback:
`<skill-dir>/bin/wt`.

## Authentication

None. `gc` calls `gh pr list` when `gh` is logged in, to recognise squash-merged
and closed PRs; without it, only ancestry merges count.

## Output conventions

One line per record, tab-separated, path last. `--json` on `list` and `gc` for
`jq`. Logs and warnings go to stderr. `wt new` prints only the new path, so
`cd "$(wt new feat/x)"` works.

## Commands

```bash
# Read
wt list                    # every linked worktree under ~/Documents, with gc's verdict
wt list --size --fetch     # add du sizes; fetch origin first for fresh merge state
wt gc                      # dry run: what gc would delete, and what it keeps and why

# Write
wt new feat/x                       # ~/worktrees/<repo>/feat-x on new branch feat/x from HEAD
wt new fix/y --base origin/main     # new branch from a given start point
wt new z --branch existing/branch   # check out an existing branch
wt rm ~/worktrees/<repo>/feat-x     # refuses uncommitted or unpushed work
wt rm <path> --force                # discards that work
wt gc --yes                         # delete everything the dry run listed
```

## Recipes

- Start branch work: `cd "$(wt new feat/x --base origin/main)"`, then install
  and build there. Never `git worktree add` into a sibling folder or a session
  scratchpad: those are never cleaned.
- Done with a branch whose PR merged: `wt rm <path>` (or let gc do it).
- Disk is full: `wt gc`, read the plan, then `wt gc --yes`.

## What gc removes

| Item                          | Removed when                                                                                                                                                                          |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Worktree                      | Clean, idle 24 h, no live lock, and its work is elsewhere: merged into `origin/<default>`, or its PR is merged or closed at this exact commit, or a detached HEAD already on a remote |
| Registration                  | The folder is gone (`git worktree prune`)                                                                                                                                             |
| `~/.cache/trunk/repos/<hash>` | No live trunk daemon and nothing written for 7 days                                                                                                                                   |
| `~/.foundry/anvil/tmp/*`      | No `anvil` process is running                                                                                                                                                         |

Everything else is kept and listed with the reason.

## Claude Code wiring

The hooks in the user-level `~/.claude/settings.json` route Claude's own
worktrees through `wt`:

- `WorktreeCreate` -> `wt hook create`: agent and `--worktree` checkouts land in
  `~/worktrees/<repo>/<name>` on branch `<name>` from the session's `HEAD`.
- `WorktreeRemove` -> `wt hook remove`: removes without a safety check, because
  Claude already decided.
- `SessionStart` -> `wt gc --yes --background`: detached, at most once an hour,
  logs to `~/.cache/worktree-cli/gc.log`.

## Gotchas

- `du` overstates `node_modules`: pnpm clones files from its store on APFS (link
  count 1, shared blocks), so a worktree's `node_modules` costs almost nothing.
  The real per-worktree weight is `contracts/out` and soldeer `dependencies`
  (0.5–1 GB), plus the trunk cache (up to 1.7 GB).
- Trunk names its caches by an opaque hash, so gc cannot tell which checkout
  owns one. It deletes by idleness instead; trunk rebuilds a cache on its next
  run.
- `WT_PROTECT` (default `~/.cache`) lists roots whose worktrees belong to other
  tools (inbox workers); gc reports them and never removes them.
- A worktree on a pushed branch with no PR and no merge is kept forever. Remove
  it with `wt rm` when you are done.
- Env overrides: `WT_ROOT` (default `~/worktrees`), `WT_SCAN_ROOTS`
  (colon-separated, default `~/Documents`, repos up to 3 levels deep),
  `WT_PROTECT`.
