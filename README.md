# worktree-cli

**Visibility: public.**
**Extension: not shipped** (it manages local git checkouts and Claude Code
hooks; there is nothing for a Claude Desktop user without a terminal).

Git worktrees are cheap to create and expensive to forget. Each one carries its
own build output, and tools such as trunk keep a per-checkout cache outside the
worktree that outlives it. `wt` creates every worktree in one place and
garbage-collects what they leave behind, without ever dropping work that exists
only locally.

## Install

`wt` is a [PEP 723](https://peps.python.org/pep-0723/) `uv` script, so its
dependencies resolve on first run:

```bash
git clone https://github.com/ClementWalter/worktree-cli.git
ln -sfn "$PWD/worktree-cli/bin/wt" ~/.local/bin/wt
```

Requires [uv](https://docs.astral.sh/uv/) and git; `gh` (logged in) lets gc
recognise squash-merged and closed PRs.

## Use

```bash
wt new feat/x --base origin/main   # ~/worktrees/<repo>/feat-x, prints the path
wt list                            # every worktree, with what gc would do
wt rm ~/worktrees/<repo>/feat-x    # refuses uncommitted or unpushed work
wt gc                              # dry run
wt gc --yes                        # apply
```

`gc` removes a worktree only when it is clean, idle for 24 h, not locked by a
live process, and its commits are merged or its PR is merged or closed at the
same commit. It also prunes dead registrations, deletes trunk caches idle for 7
days, and clears anvil state when no anvil runs.

## Claude Code

Route Claude's worktrees through `wt` and collect on every session start, in
`~/.claude/settings.json`:

```json
{
  "hooks": {
    "WorktreeCreate": [{ "hooks": [{ "type": "command", "command": "\"$HOME/.local/bin/wt\" hook create" }] }],
    "WorktreeRemove": [{ "hooks": [{ "type": "command", "command": "\"$HOME/.local/bin/wt\" hook remove" }] }],
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "\"$HOME/.local/bin/wt\" gc --yes --background" }] }]
  }
}
```

[SKILL.md](SKILL.md) is the agent's manual.

## Tests

```bash
uv run --script tests/test_worktree_cli.py
```
