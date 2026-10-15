# claude-lease

Advisory, queued leases on shared machine resources for concurrent Claude Code
sessions on one host. Typical resources: an app that every worktree installs to
the same place (`make install` → `/Applications/…`), or full-screen computer use.

Without it, sessions do not know about each other: one session's install
silently replaces the build another session is verifying. With it:

- **Discovery.** A `PreToolUse` hook blocks a tool call that uses a resource the
  session does not hold, and the block message says who holds it (session name
  from `ListAgents`, folder, branch, purpose, since/until) and how to queue.
  A `SessionStart` hook puts the current state into every new session's context.
- **Queueing, not grabbing.** First come, first served, with a time limit.
  `--urgent` exists only for when the user says so.
- **Wake-up on release.** The waiting session runs `claude-lease wait` as a
  background Bash task; it exits when it is that session's turn, which wakes
  the session. Nothing depends on the holder remembering to send a message.
- **No stale locks.** The lease is an `flock` held by a detached process that
  exits when the owning Claude Code process (`$CLAUDE_PID`) dies, when the lease
  expires, or on release.

## Install

```bash
git clone https://github.com/jizhi0v0/claude-lease.git ~/Developer/github/claude-lease
cd ~/Developer/github/claude-lease
./install.py                     # links ~/.local/bin/claude-lease, creates ~/.claude/lease/resources.json
$EDITOR ~/.claude/lease/resources.json   # see examples/resources.json
./install.py --hooks --dry-run   # preview the hooks section
./install.py --hooks             # register the hooks in ~/.claude/settings.json (backs it up first)
```

Re-run `./install.py --hooks` after changing any `"tool"` pattern: the
`PreToolUse` matcher is built from them, so other tools never start the hook.
`./install.py --remove-hooks` takes the hooks out again. Hooks are reloaded by
running sessions within a couple of seconds.

Needs Python 3.9+ and Claude Code 2.1.214+ (`CLAUDE_PID`). Works on macOS and
Linux. Install it on every host whose resources need leases: an SSH session
runs Claude Code on the remote host and reads that host's `~/.claude`.
Cloud sessions run on Anthropic's machines and never touch this host's
resources, so they do not take part.

## Configure

`~/.claude/lease/resources.json`:

```json
{
  "resources": {
    "duo-app": {
      "description": "/Applications/DuoUpdater.app: deploy + verify",
      "ttl_minutes": 20,
      "present_if_exists": "/Applications/DuoUpdater.app",
      "match": [
        {"tool": "^Bash$", "input": "(^|[;&|(]\\s*)(make\\s+install|\\S*scripts/install\\.sh)(?=$|[\\s;&|)])"}
      ]
    }
  }
}
```

- `match`: a tool call uses the resource when `tool` matches the tool name and,
  if given, `input` matches the Bash command (or the JSON of any other tool's
  input). Regexes, Python syntax.
- `present_if_exists`: skip the resource on hosts where this path is missing.
- `ttl_minutes`: default lease length (`--minutes` overrides, `renew` extends).

Lease what is actually shared and for as long as it is shared: for an app that
is the deploy *and* the verification after it, not the compile before it.

## Use (what Claude does)

```bash
claude-lease acquire duo-app --name '<ListAgents name>' --purpose 'verify #1312 tooltip'
# exit 0: ACQUIRED   exit 3: QUEUED -> then, as a background task:
claude-lease wait duo-app --name '<ListAgents name>' --purpose 'verify #1312 tooltip'
claude-lease renew duo-app --minutes 30
claude-lease release duo-app
claude-lease status [--json]
```

The block message and the session-start context tell Claude all of this, so no
CLAUDE.md instructions are needed.

A queued session that has neither a running `wait` nor called `acquire` for
120 s (`CLAUDE_LEASE_GRACE_SECONDS`) while the resource is free loses its turn.
Waiters whose Claude Code process is gone are dropped.

## Guarantees and limits

- Advisory: it only governs tool calls that match a rule. A command spelled
  differently, or a human in a terminal, is not stopped.
- The hook fails open: any error leaves the call alone and is logged to
  `~/.claude/lease/hook-errors.log`. A broken lease system must not break sessions.
- One host. State lives in `~/.claude/lease/state/`.

## Test

```bash
python3 -m unittest -v tests/test_lease.py
```
