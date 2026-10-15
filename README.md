# claude-lease

Advisory, queued leases on shared machine resources for concurrent Claude Code
sessions on one host. Typical resources: an app that every worktree installs to
the same place (`make install` → `/Applications/…`), or full-screen computer use.

Without it, sessions do not know about each other: one session's install
silently replaces the build another session is verifying. With it:

- **Discovery.** A `PreToolUse` hook blocks a tool call that uses a resource the
  session does not hold, and the block message says who holds it (session name
  from `ListAgents`, folder, branch, purpose, since/until), how many other
  sessions wait, and what to do next (queue, or your place if already queued).
  A `SessionStart` hook puts the current state into every new session's context.
  A `SessionEnd` hook releases the ending session's leases and takes it out of
  every queue.
- **Queueing, not grabbing.** First come, first served, with a time limit.
  `--urgent` (only for when the user says so) moves a session to the front of
  the queue; it never takes the lease away from its holder.
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
./install.py                     # copies the CLI to ~/.claude/lease/bin, links ~/.local/bin/claude-lease, creates resources.json
$EDITOR ~/.claude/lease/resources.json   # see examples/resources.json
./install.py --hooks --dry-run   # preview the hooks section
./install.py --hooks             # register the hooks in ~/.claude/settings.json (backs it up first)
```

The CLI is copied, not linked, so editing the checkout does not change what
running sessions execute; re-run `./install.py` to ship a change (it refuses a
file that does not parse). Re-run `./install.py --hooks` after changing any `"tool"` pattern: the
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
        {"tool": "^Bash$", "input": "^(make\\s+install|\\S*scripts/install\\.sh)(\\s|$)"}
      ]
    }
  }
}
```

- `match`: a tool call uses the resource when `tool` matches the tool name and,
  if given, `input` matches. Regexes, Python syntax. For other tools than Bash,
  `input` is tried against the JSON of the tool input. For Bash, `input` is
  tried against each simple command that runs on this host, so anchor patterns
  with `^`. A Bash command is taken apart like this:
  - split on newlines, `;`, `&&`, `||`, `|`, `&` and parentheses, with quotes respected;
  - the bodies of `` `…` `` and `$(…)` are commands of their own (also inside
    double quotes; not inside single quotes);
  - a heredoc body is commands when fed to a shell (`bash <<EOF`). Fed to
    anything else (`cat`, `python3 -`, `git commit -F -`, `ssh`) it is data,
    except that with an unquoted delimiter (`<<EOF`) the local shell still
    runs the `` `…` `` and `$(…)` in it, whatever quotes surround them; with a
    quoted delimiter (`<<'EOF'`, `<<"EOF"`) nothing in the body runs here;
  - in front of the real command, `VAR=value`, the keywords `if then else elif
    fi while until do done { } !` and the wrappers `time env command exec nohup
    builtin` (with their own `-options`) are dropped;
  - a command starting with `ssh` is left out with its arguments and heredoc,
    because it runs on another host. A `$(…)` in double quotes inside it still
    counts, because the local shell runs it before `ssh` starts.

  So `^make\s+install(\s|$)` matches `cd x && make install`,
  `if ok; then make install; fi`, `time make install` and `echo "$(make install)"`,
  but not `git commit -m "… make install …"`, `cat <<EOF` text that mentions it,
  or `ssh mini 'make install'`.
- `present_if_exists`: the resource only exists on hosts where this path does.
  Elsewhere it is not shown, never blocks anything, and cannot be acquired.
- `ttl_minutes`: default lease length; `--minutes` overrides it per call.
  Minutes may be fractional (`--minutes 0.5`).

A `resources.json` that does not parse is an error: commands exit 1 and say so,
and the hook lets every call through and logs it to
`~/.claude/lease/hook-errors.log`. A missing file means no resources.

Lease what is actually shared and for as long as it is shared: for an app that
is the deploy *and* the verification after it, not the compile before it.

## Use (what Claude does)

```bash
claude-lease acquire duo-app --purpose 'verify #1312 tooltip'
# exit 0: ACQUIRED   exit 3: QUEUED -> then, as a background task:
claude-lease wait duo-app --purpose 'verify #1312 tooltip'
claude-lease renew duo-app --minutes 30
claude-lease release duo-app
claude-lease status [resource...] [--json]
claude-lease <command> --help
```

- `release` gives the lease back, or, for a session that is only queued,
  leaves the queue. Either way it exits 0; it exits 1 when there was nothing
  to give back.
- `renew --minutes N` makes the lease last until N minutes from now
  (`ttl_minutes` without `--minutes`). It never shortens a lease: if the lease
  already lasts longer, it prints `UNCHANGED` and keeps the later time.
- `status` lists every resource present on this host, or the named ones. A
  named resource that is not configured is an error (exit 2); one that is
  configured but not present on this host is listed as such.
- Exit codes: 0 ok, 3 queued, 2 usage error, 1 failure.

`--name` is the session's name as `ListAgents` shows it, so a blocked session
can `SendMessage` the holder. When it is left out, it is read from Claude Code's
own session file `~/.claude/sessions/$CLAUDE_PID.json`; that file is not a
documented interface, so this is only a fallback and an unreadable file just
leaves the name empty.

The block message and the session-start context tell Claude all of this, so no
CLAUDE.md instructions are needed.

A queued session that has neither a running `wait` nor called `acquire` for
120 s (`CLAUDE_LEASE_GRACE_SECONDS`) while the resource is free loses its turn.
Waiters whose Claude Code process is gone are dropped. `status` and the hooks
already show the queue without them; their queue files are deleted at the next
`acquire`, `wait` or `release`.

A lease belongs to a Claude Code process (`$CLAUDE_PID`), and a subagent runs
in its parent's process: a subagent shares its parent session's leases and
place in the queue, and its default `--name` is the parent's.

## Guarantees and limits

- Advisory: it only governs tool calls that match a rule. Not taken apart, on
  purpose: `sudo …`, `nice …`, `xargs …`, `eval …`, `sh -c '…'`/`bash -c '…'`,
  and anything inside a script or Makefile the command runs. Those, and a
  human in a terminal, are not stopped.
- What `ssh host '…'` runs on the other host is never matched: that host has
  its own leases (install claude-lease there), and a lease cannot be held
  across hosts.
- The hook fails open: any error (unparseable input, a broken
  `resources.json`, a bad regex) leaves the call alone and is logged to
  `~/.claude/lease/hook-errors.log`. Empty input is treated as no tool call,
  not as an error. A broken lease system must not break sessions.
- One host. State lives in `~/.claude/lease/state/`.

## Test

```bash
python3 -m unittest -v tests/test_lease.py
```
