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
  It blocks even when the resource is free: the session must take the lease
  before it uses the resource, so that it is on record while it does.
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
  - a `#` that starts a word outside quotes comments out the rest of its line,
    a `<<` in it included; a `#` inside a word (`a#b`, `\#`, `'#'`) is text;
  - the bodies of `` `…` `` and `$(…)` are commands of their own (also inside
    double quotes; not inside single quotes);
  - a heredoc body is commands when fed to a shell (`bash <<EOF`). Fed to
    anything else (`cat`, `python3 -`, `git commit -F -`, `ssh`) it is data,
    except that with an unquoted delimiter (`<<EOF`) the local shell still
    runs the `` `…` `` and `$(…)` in it, whatever quotes surround them; with a
    quoted delimiter (`<<'EOF'`, `<<"EOF"`) nothing in the body runs here.
    Only a `<<` outside quotes (`'…'`, `"…"`, `$'…'`) starts a heredoc;
  - in front of the real command, `VAR=value`, the keywords `if then else elif
    fi while until do done { } !` and the wrappers `time env command exec nohup
    builtin` are dropped, together with the wrappers' own options and those
    options' values (`env -C dir`, `env -u NAME`, `exec -a name`,
    `time -o file`, `--opt=value`, `--`); the value of `env -S`/`--split-string`
    is unpacked as the command itself;
  - a command starting with `ssh` is left out together with its arguments,
    because they run on another host. Two things in it still run here first
    and therefore count: a `$(…)` or `` `…` `` in double quotes in its
    arguments, and one in a heredoc fed to it with an unquoted delimiter (the
    heredoc rule above).

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
`~/.claude/lease/hook-errors.log`. A missing file means no resources. Only
parsing is checked up front; see "Known limits" for what happens with a bad
regex.

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
claude-lease <command> --help     # the same usage text as claude-lease --help
```

- `release` gives the lease back, or, for a session that is only queued,
  leaves the queue. Either way it exits 0; it exits 1 when there was nothing
  to give back.
- `renew --minutes N` makes the lease last until N minutes from now
  (`ttl_minutes` without `--minutes`). It never shortens a lease: if the lease
  already lasts longer, it prints `UNCHANGED` and keeps the later time.
- `acquire` requires `--purpose` (exit 2 without it), so that others can see
  why the lease is held. `wait` takes it too and should be given the same
  value; without it the lease it obtains shows no purpose.
- `status` lists every resource present on this host, or the named ones. A
  named resource that is not configured is an error (exit 2); one that is
  configured but not present on this host is listed as such in the text
  output, and left out of `--json` (see "Known limits").
- Exit codes: 0 ok, 3 queued, 2 usage error, 1 failure.
- `acquire`, `wait`, `release` and `renew` need to run inside a Claude Code
  session (they read `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID`); elsewhere they
  exit 2 and say so. `status` works anywhere.

`--name` is the session's name as `ListAgents` shows it, so a blocked session
can `SendMessage` the holder. When it is left out, it is read from Claude Code's
own session file `~/.claude/sessions/$CLAUDE_PID.json`; that file is not a
documented interface, so this is only a fallback and an unreadable file just
leaves the name empty.

The block message and the session-start context tell Claude all of this, so no
CLAUDE.md instructions are needed.

A queued session that has neither a running `wait` nor called `acquire` for
120 s (`CLAUDE_LEASE_GRACE_SECONDS`) while the resource is free loses its turn:
it is removed from the queue, and has to `acquire` again, which puts it at the
end.
Waiters whose Claude Code process is gone are dropped. `status` and the hooks
already show the queue without them; their queue files are deleted by the next
`acquire`, `release` or check of any running `wait` (a running `wait` checks
about once a second).

A lease, and a place in the queue, belong to a session: the session ID
(`$CLAUDE_CODE_SESSION_ID`; the hook uses the `session_id` of its input) says
whose it is, and the lease ends when that session's Claude Code process
(`$CLAUDE_PID`) exits. A subagent has its parent's session ID and process, so
it shares its parent's leases and place in the queue, and its default `--name`
is the parent's. Each call records where the session is now: a queue entry
shows the folder and branch of the session's latest `acquire` or `wait`.

## Guarantees and limits

- Advisory: it only governs tool calls that match a rule. Not taken apart, on
  purpose: `sudo …`, `nice …`, `xargs …`, `eval …`, `sh -c '…'`/`bash -c '…'`,
  and anything inside a script or Makefile the command runs. Nor does a
  configured pattern match more than it says: `^make\s+install` does not
  cover `bash scripts/install.sh`. Those, and a human in a terminal, are not
  stopped.
- What `ssh host '…'` runs on the other host is never matched: that host has
  its own leases (install claude-lease there), and a lease cannot be held
  across hosts.
- The hook fails open: any error (unparseable input, a broken
  `resources.json`, a bad regex) leaves the call alone and is logged to
  `~/.claude/lease/hook-errors.log`. Empty input is treated as no tool call,
  not as an error. A broken lease system must not break sessions.
- One host. State lives in `~/.claude/lease/state/`.

## Known limits

Found by testing and left as they are for now.

- **A comment that ends in `\` can hide the next line (lets a call through).**
  A backslash at the end of a line is joined with the next line before
  comments are looked at, so in `echo hi # note \` followed by a newline and
  `make install`, the second line is read as part of the comment, although
  bash ends a comment at the newline and runs it.
- **`$'…'` strings are not fully understood (blocks too much).** In bash's
  ANSI-C quoting a `\'` does not end the string. Finding heredocs knows this;
  the rest of the matching does not, so text after a `\'` in the same `$'…'`,
  such as a backtick, can be read as a command and the call blocked, although
  bash would run nothing. Plain `$'…'` strings without `\'` are fine.
- **Config errors other than parsing only show when used.** A bad regex is
  noticed when the hook tries it: the hook then fails open for that call (for
  every resource, not just the one with the bad regex) and logs it; `status`
  still exits 0 and shows the resource as usual.
- **`status --json` leaves out a resource that is not present on this host**,
  also when it is named (`status gone --json` prints `{}`), where the text
  output lists it as not present.
- **A command bash cannot parse** (unbalanced quotes) is matched as one piece
  of raw text, so it may be blocked or let through; bash would reject it and
  run nothing either way.

## Test

```bash
python3 -m unittest -v tests/test_lease.py
```
