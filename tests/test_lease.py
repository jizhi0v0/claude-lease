"""End-to-end tests: every call goes through the real executable in a
subprocess, with a `sleep` process standing in for each Claude Code session."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "claude-lease")

CONFIG = {
    "resources": {
        "app": {
            "description": "the installed app",
            "ttl_minutes": 20,
            "match": [{"tool": "^Bash$", "input": r"^make\s+install(\s|$)"}],
        },
        "screen": {"match": [{"tool": r"^mcp__computer-use__computer_batch$"}]},
        "absent": {
            "present_if_exists": "/nonexistent/claude-lease-test",
            "match": [{"tool": "^Bash$", "input": "absent-thing"}],
        },
    }
}


def wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class Session:
    def __init__(self, t, sid):
        self.t, self.sid = t, sid
        self.proc = subprocess.Popen(["sleep", "300"])
        t.addCleanup(self.die)

    @property
    def pid(self):
        return self.proc.pid

    def env(self):
        e = dict(self.t.env, CLAUDE_CODE_SESSION_ID=self.sid, CLAUDE_PID=str(self.pid),
                 CLAUDE_CODE_MESSAGING_SOCKET="/tmp/cc-socks/%d.sock" % self.pid)
        return e

    def run(self, *args, stdin=None):
        return subprocess.run([sys.executable, BIN] + list(args), env=self.env(), input=stdin,
                              capture_output=True, text=True, timeout=20)

    def start(self, *args):
        p = subprocess.Popen([sys.executable, BIN] + list(args), env=self.env(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.t.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def hook(self, event, payload):
        payload = dict(payload, session_id=self.sid)
        r = self.run("hook", event, stdin=json.dumps(payload))
        self.t.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def bash(self, command):
        return self.hook("pretooluse", {"tool_name": "Bash", "tool_input": {"command": command}})

    def die(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="claude-lease-test-")
        self.addCleanup(shutil.rmtree, self.home, True)
        with open(os.path.join(self.home, "resources.json"), "w") as f:
            json.dump(CONFIG, f)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        self.env.update(CLAUDE_LEASE_HOME=self.home, CLAUDE_LEASE_TICK_SECONDS="0.1",
                        CLAUDE_LEASE_GRACE_SECONDS="60", HOME=self.home)
        self.addCleanup(self.kill_holders)

    def kill_holders(self):
        for res in CONFIG["resources"]:
            h = self.holder(res)
            if h:
                try:
                    os.kill(h["holder_pid"], 9)
                except OSError:
                    pass

    def holder(self, res="app"):
        try:
            with open(os.path.join(self.home, "state", res, "holder.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def acquire(self, s, res="app", *extra):
        return s.run("acquire", res, "--name", "S-" + s.sid, "--purpose", "test " + s.sid, *extra)

    # -------------------------------------------------------------- lease basics

    def test_acquire_free_then_second_session_queues(self):
        a, b = Session(self, "a"), Session(self, "b")
        r = self.acquire(a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ACQUIRED app", r.stdout)
        h = self.holder()
        self.assertEqual(h["session_id"], "a")
        self.assertEqual(h["name"], "S-a")
        self.assertEqual(h["socket"], "/tmp/cc-socks/%d.sock" % a.pid)
        r = self.acquire(b)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("QUEUED app: position 1 of 1; held by S-a", r.stdout)
        self.assertEqual(self.acquire(a).returncode, 0)  # re-acquire by holder is a no-op
        self.assertIn("ALREADY HELD", self.acquire(a).stdout)

    def write_registry(self, s, content):
        d = os.path.join(self.home, ".claude", "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "%d.json" % s.pid), "w") as f:
            f.write(content)

    def test_name_falls_back_to_claude_session_registry(self):
        a, b, c = Session(self, "a"), Session(self, "b"), Session(self, "c")
        self.write_registry(a, json.dumps({"sessionId": "a", "name": "Registry Name", "status": "busy"}))
        self.write_registry(b, json.dumps({"name": "Ignored"}))
        self.write_registry(c, "{not json")
        self.assertEqual(a.run("acquire", "app", "--purpose", "p").returncode, 0)
        self.assertEqual(self.holder()["name"], "Registry Name")
        a.run("release", "app")
        self.assertEqual(b.run("acquire", "app", "--name", "Given", "--purpose", "p").returncode, 0)
        self.assertEqual(self.holder()["name"], "Given", "an explicit --name wins")
        b.run("release", "app")
        self.assertEqual(c.run("acquire", "app", "--purpose", "p").returncode, 0)
        self.assertEqual(self.holder()["name"], "", "unreadable registry: no name, as before")

    def test_purpose_is_required(self):
        a = Session(self, "a")
        r = a.run("acquire", "app", "--name", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--purpose is required", r.stderr)

    def test_outside_a_session_is_refused(self):
        r = subprocess.run([sys.executable, BIN, "acquire", "app", "--purpose", "x"], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("inside a Claude Code session", r.stderr)

    def test_release_wakes_background_waiter(self):
        a, b = Session(self, "a"), Session(self, "b")
        self.assertEqual(self.acquire(a).returncode, 0)
        w = b.start("wait", "app", "--name", "S-b", "--purpose", "wait test")
        time.sleep(0.5)
        self.assertIsNone(w.poll(), "waiter must block while the lease is held")
        r = a.run("release", "app")
        self.assertIn("RELEASED app. Next in line: S-b", r.stdout)
        out, _ = w.communicate(timeout=5)
        self.assertEqual(w.returncode, 0)
        self.assertIn("ACQUIRED app", out)
        self.assertEqual(self.holder()["session_id"], "b")

    def test_holder_session_death_frees_lease(self):
        a, b = Session(self, "a"), Session(self, "b")
        self.acquire(a)
        hpid = self.holder()["holder_pid"]
        a.die()
        self.assertTrue(wait_until(lambda: self.holder() is None), "record must go when the owner dies")
        self.assertTrue(wait_until(lambda: subprocess.run(["kill", "-0", str(hpid)],
                                                          capture_output=True).returncode != 0))
        self.assertEqual(self.acquire(b).returncode, 0)

    def test_expiry_frees_lease_and_renew_extends_it(self):
        a, b = Session(self, "a"), Session(self, "b")
        self.acquire(a, "app", "--minutes", "0.02")  # 1.2 s
        r = a.run("renew", "app", "--minutes", "0.06")  # 3.6 s from now
        self.assertIn("RENEWED", r.stdout)
        time.sleep(1.8)
        self.assertIsNotNone(self.holder(), "renew must outlive the original expiry")
        self.assertTrue(wait_until(lambda: self.holder() is None, timeout=5))
        self.assertEqual(self.acquire(b).returncode, 0)

    def test_fifo_order(self):
        a, b, c = Session(self, "a"), Session(self, "b"), Session(self, "c")
        self.acquire(a)
        wb = b.start("wait", "app", "--name", "S-b", "--purpose", "p")
        time.sleep(0.3)
        wc = c.start("wait", "app", "--name", "S-c", "--purpose", "p")
        time.sleep(0.3)
        a.run("release", "app")
        wb.communicate(timeout=5)
        self.assertEqual(self.holder()["session_id"], "b")
        time.sleep(0.5)
        self.assertIsNone(wc.poll(), "c must keep waiting behind b")
        b.run("release", "app")
        wc.communicate(timeout=5)
        self.assertEqual(self.holder()["session_id"], "c")

    def test_free_resource_respects_queue_head(self):
        a, b, c = Session(self, "a"), Session(self, "b"), Session(self, "c")
        self.acquire(a)
        self.assertEqual(self.acquire(b).returncode, 3)
        a.run("release", "app")
        # Free now, but b is first in line and still within its grace period.
        r = self.acquire(c)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("position 2 of 2", r.stdout)
        self.assertEqual(self.acquire(b).returncode, 0)

    def test_inactive_head_loses_turn_after_grace(self):
        self.env["CLAUDE_LEASE_GRACE_SECONDS"] = "1"
        a, b, c = Session(self, "a"), Session(self, "b"), Session(self, "c")
        self.acquire(a)
        self.assertEqual(self.acquire(b).returncode, 3)  # queued, never runs wait
        a.run("release", "app")
        time.sleep(1.5)
        self.assertEqual(self.acquire(c).returncode, 0)
        self.assertNotIn("S-b", a.run("status").stdout)

    def test_dead_waiter_is_dropped(self):
        a, b, c = Session(self, "a"), Session(self, "b"), Session(self, "c")
        self.acquire(a)
        self.acquire(b)
        b.die()
        a.run("release", "app")
        self.assertEqual(self.acquire(c).returncode, 0)

    def test_urgent_jumps_the_queue_but_not_the_holder(self):
        a, b, c = Session(self, "a"), Session(self, "b"), Session(self, "c")
        self.acquire(a)
        self.acquire(b)
        r = self.acquire(c, "app", "--urgent")
        self.assertEqual(r.returncode, 3)
        self.assertIn("position 1 of 2", r.stdout)

    def test_release_without_holding_and_leaving_queue(self):
        a, b = Session(self, "a"), Session(self, "b")
        self.assertEqual(b.run("release", "app").returncode, 1)
        self.acquire(a)
        self.acquire(b)
        r = b.run("release", "app")
        self.assertIn("LEFT the queue", r.stdout)
        self.assertNotIn("S-b", a.run("status").stdout)

    def test_absent_resource_cannot_be_acquired(self):
        a = Session(self, "a")
        for cmd in ("acquire", "wait"):
            r = a.run(cmd, "absent", "--purpose", "p")
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("not present on this host", r.stderr)
        r = a.run("status", "absent")
        self.assertEqual(r.returncode, 0)
        self.assertIn("- absent: not present on this host", r.stdout)

    def test_status_of_unknown_resource_is_an_error(self):
        a = Session(self, "a")
        r = a.run("status", "nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown resource 'nope'", r.stderr)

    def test_subcommand_help(self):
        a = Session(self, "a")
        for cmd in ("acquire", "wait", "release", "renew", "status"):
            r = a.run(cmd, "--help")
            self.assertEqual(r.returncode, 0, cmd)
            self.assertIn("usage:", r.stdout)

    def test_renew_never_shortens(self):
        a = Session(self, "a")
        self.acquire(a, "app", "--minutes", "5")
        before = self.holder()["expires_at"]
        r = a.run("renew", "app", "--minutes", "1")
        self.assertIn("UNCHANGED app", r.stdout)
        self.assertEqual(self.holder()["expires_at"], before)
        r = a.run("renew", "app", "--minutes", "10")
        self.assertIn("RENEWED app", r.stdout)
        self.assertGreater(self.holder()["expires_at"], before + 200)

    def test_broken_config_is_reported(self):
        a = Session(self, "a")
        with open(os.path.join(self.home, "resources.json"), "w") as f:
            f.write("{broken")
        r = a.run("status")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not parse", r.stderr)
        r = a.run("hook", "pretooluse", stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": "make install"}}))
        self.assertEqual((r.returncode, r.stdout), (0, ""), "the hook still fails open")
        with open(os.path.join(self.home, "hook-errors.log")) as f:
            self.assertIn("does not parse", f.read())

    def test_status_hides_a_waiter_that_lost_its_turn(self):
        self.env["CLAUDE_LEASE_GRACE_SECONDS"] = "1"
        a, b = Session(self, "a"), Session(self, "b")
        self.acquire(a)
        self.acquire(b)
        a.run("release", "app")
        time.sleep(1.5)
        self.assertNotIn("S-b", a.run("status").stdout)  # no acquire has run since

    def test_unknown_resource(self):
        a = Session(self, "a")
        r = a.run("acquire", "nope", "--purpose", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown resource", r.stderr)

    # -------------------------------------------------------------- hooks

    def test_pretooluse_free_resource_denies_with_instructions(self):
        a = Session(self, "a")
        out = a.bash("cd /x && make install")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("claude-lease acquire app", reason)

    def test_pretooluse_allows_holder_and_names_holder_to_others(self):
        a, b = Session(self, "a"), Session(self, "b")
        self.acquire(a)
        self.assertIsNone(a.bash("make install"))
        reason = b.bash("make install")["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("held by S-a", reason)
        self.assertIn("SendMessage to S-a", reason)
        self.assertIn("test a", reason)

    def test_pretooluse_ignores_unrelated_and_absent(self):
        a = Session(self, "a")
        self.assertIsNone(a.bash("echo hello"))
        self.assertIsNone(a.bash("grep 'make install' Makefile"))
        self.assertIsNone(a.bash("echo absent-thing"))
        self.assertIsNone(a.hook("pretooluse", {"tool_name": "Read", "tool_input": {"file_path": "/x"}}))

    def test_pretooluse_matches_local_simple_commands_only(self):
        a = Session(self, "a")
        install = "make" + " install"
        local = [
            "cd ~/x && %s" % install,
            "cd ~/x\n%s" % install,
            "git pull\n%s 2>&1 | tail -5" % install,
            "%s 2>&1 | tail -5" % install,
            "FOO=1 %s" % install,
            "(%s)" % install,
            "bash <<'EOF'\ncd x\n%s\nEOF" % install,
            "cd x && \\\n  %s" % install,
        ]
        not_local = [
            "ssh mini 'cd x && %s'" % install,
            "ssh mini 'echo a; %s; echo b'" % install,
            "ssh -o BatchMode=yes mini \"cd x; %s 2>&1 | tail\"" % install,
            "ssh mini <<'EOF'\ncd x\n%s\nEOF" % install,
            "cd x && ssh mini <<EOF\n%s\nEOF\necho done" % install,
            "echo 'next: cd x; %s'" % install,
            'git commit -m "fix: %s; then verify"' % install,
            "grep '%s' Makefile" % install,
            "%s-tools" % install,
        ]
        for c in local:
            self.assertIsNotNone(a.bash(c), "must be treated as local: %r" % c)
        for c in not_local:
            self.assertIsNone(a.bash(c), "must not match: %r" % c)

    def test_pretooluse_unwraps_keywords_wrappers_and_substitutions(self):
        a = Session(self, "a")
        install = "make" + " install"
        local = [
            "`%s`" % install,
            "echo \"`%s`\"" % install,
            "$(%s)" % install,
            "echo \"done: $(%s)\"" % install,
            "x=$(cd y && %s)" % install,
            "ssh mini \"echo $(%s)\"" % install,  # expanded by the local shell before ssh runs
            "{ %s; }" % install,
            "if true; then %s; fi" % install,
            "while false; do %s; done" % install,
            "! %s" % install,
            "time %s" % install,
            "time -p %s" % install,
            "env FOO=1 %s" % install,
            "env -i FOO=1 %s" % install,
            "command %s" % install,
            "nohup %s &" % install,
            # An unquoted heredoc delimiter: the local shell expands the body,
            # and quote characters in the body protect nothing.
            "cat <<EOF\nresult: $(%s)\nEOF" % install,
            "cat <<EOF\n'`%s`'\nEOF" % install,
            "ssh mini <<EOF\n$(%s)\nEOF" % install,
            "bash <<'EOF'\n%s\nEOF" % install,
        ]
        not_local = [
            # A quoted delimiter: nothing in the body runs here.
            "git commit -q -F - <<'EOF'\nfix `if x; then %s; fi` and $(%s)\nEOF" % (install, install),
            "cat <<\"EOF\"\n$(%s)\nEOF" % install,
            "ssh mini <<'EOF'\n$(%s)\nEOF" % install,
            "cat <<X\n%s\nX" % install,
            "python3 - <<'EOF'\nprint('%s')\nEOF" % install,
            "cat <<< '%s'" % install,
            "ssh mini 'echo $(%s)'" % install,
            "echo '`%s`'" % install,
            # Documented gaps (README "Guarantees and limits"): not unwrapped on purpose.
            "sudo %s" % install,
            "sh -c '%s'" % install,
        ]
        for c in local:
            self.assertIsNotNone(a.bash(c), "must be treated as local: %r" % c)
        for c in not_local:
            self.assertIsNone(a.bash(c), "must not match: %r" % c)

    def test_pretooluse_heredoc_operator_counts_only_outside_quotes(self):
        a = Session(self, "a")
        install = "make" + " install"
        local = [
            # A quoted << is text, so the next line is a command, not heredoc data.
            "echo \"a <<EOF\"\n%s" % install,
            "echo 'a <<EOF'\n%s" % install,
            "echo $'a <<EOF'\n%s" % install,
            "echo \"a \\\" <<EOF\"\n%s" % install,
            "echo \"a\n<<EOF\"\n%s" % install,
            "echo \"a <<EOF\" </dev/null\n%s" % install,
            "echo \"<<EOF\"; %s" % install,
        ]
        not_local = [
            # The << after a closed quote is still a heredoc.
            "echo \"a\" <<EOF\n%s\nEOF" % install,
            "echo 'it\"s' <<EOF\n%s\nEOF" % install,
        ]
        for c in local:
            self.assertIsNotNone(a.bash(c), "must be treated as local: %r" % c)
        for c in not_local:
            self.assertIsNone(a.bash(c), "must not match: %r" % c)

    def test_pretooluse_comment_ends_at_its_line(self):
        a = Session(self, "a")
        install = "make" + " install"
        local = [
            # A << in a comment is not a heredoc, so the next line is a command.
            "echo hi # <<EOF\n%s" % install,
            "echo hi;# <<EOF\n%s" % install,
            "# <<EOF\n%s" % install,
            # A comment ends at its newline.
            "echo hi # note\n%s" % install,
            # A # inside a word does not start a comment.
            "echo a#b; %s" % install,
            "echo \\#b; %s" % install,
            "echo '#'b; %s" % install,
            "claude-lease status a#b; %s" % install,
            "claude-lease status # note; x\n%s" % install,
            # In heredoc data a # line is data, and its $(...) still runs here.
            "cat <<EOF\n# $(%s)\nEOF" % install,
        ]
        not_local = [
            "true # %s" % install,
            "# %s" % install,
            "true;#%s" % install,
            "true # x; %s" % install,
            "true # $(%s)" % install,
            # A << before the comment still starts a heredoc.
            "cat <<EOF # note\n%s\nEOF" % install,
            "echo '#' <<EOF\n%s\nEOF" % install,
        ]
        for c in local:
            self.assertIsNotNone(a.bash(c), "must be treated as local: %r" % c)
        for c in not_local:
            self.assertIsNone(a.bash(c), "must not match: %r" % c)

    def test_pretooluse_skips_wrapper_options_and_their_values(self):
        a = Session(self, "a")
        install = "make" + " install"
        local = [
            "exec -a foo %s" % install,
            "env -C /tmp %s" % install,
            "env -u X %s" % install,
            "env --unset=X %s" % install,
            "env --chdir /tmp FOO=1 %s" % install,
            "env -i -u X FOO=1 %s" % install,
            "env -- %s" % install,
            "env -S '%s'" % install,
            "env --split-string='FOO=1 %s'" % install,
            "time -o /tmp/t %s" % install,
            "time -f '%%e' %s" % install,
            "time env -C /tmp nohup %s" % install,
        ]
        for c in local:
            self.assertIsNotNone(a.bash(c), "must be treated as local: %r" % c)
        # A value that only looks like the command is still just a value.
        self.assertIsNone(a.bash("env -u make install-tools"))

    def test_queue_entry_follows_the_session(self):
        a, b = Session(self, "a"), Session(self, "b")
        self.acquire(a)
        repo = tempfile.mkdtemp(prefix="claude-lease-git-")
        self.addCleanup(shutil.rmtree, repo, True)
        for args in (["init", "-q", "-b", "lease-test"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git"] + args, cwd=repo, check=True, env=self.env, capture_output=True)
        p = subprocess.run([sys.executable, BIN, "acquire", "app", "--name", "S-b", "--purpose", "first"],
                           env=b.env(), cwd=repo, capture_output=True, text=True)
        self.assertEqual(p.returncode, 3)
        q = os.path.join(self.home, "state", "app", "queue")
        with open(os.path.join(q, os.listdir(q)[0])) as f:
            self.assertEqual(json.load(f)["branch"], "lease-test", "precondition: queued from a branch")
        outside = tempfile.mkdtemp(prefix="claude-lease-nogit-")
        self.addCleanup(shutil.rmtree, outside, True)
        p = subprocess.run([sys.executable, BIN, "acquire", "app", "--purpose", "second"],
                           env=b.env(), cwd=outside, capture_output=True, text=True)
        self.assertEqual(p.returncode, 3)
        (entry_file,) = os.listdir(q)
        with open(os.path.join(q, entry_file)) as f:
            e = json.load(f)
        self.assertEqual(os.path.realpath(e["cwd"]), os.path.realpath(outside))
        self.assertEqual(e["branch"], "", "no git repo there: no branch, not the old one")
        self.assertEqual(e["purpose"], "second")
        self.assertEqual(e["name"], "S-b", "kept when not given again")

    def test_unchanged_message_keeps_fractional_minutes(self):
        a = Session(self, "a")
        self.acquire(a, "app", "--minutes", "5")
        self.assertIn("later than 0.5 minute(s) from now", a.run("renew", "app", "--minutes", "0.5").stdout)

    def test_pretooluse_counts_others_and_tells_a_queued_caller_its_place(self):
        a, b, c = Session(self, "a"), Session(self, "b"), Session(self, "c")
        self.acquire(a)
        self.acquire(b)
        reason = lambda s: s.bash("make install")["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("already in the queue at position 1 of 1", reason(b))
        self.assertIn("1 other session(s) are waiting", reason(c))
        a.run("release", "app")
        r = reason(c)  # free, but b is first in line
        self.assertIn("is free", r)
        self.assertIn("1 other session(s) are waiting", r)
        self.assertIn("already in the queue at position 1 of 1", reason(b))

    def test_mcp_tool_rules_still_match_tool_names_only(self):
        a = Session(self, "a")
        self.assertIsNotNone(a.hook("pretooluse", {"tool_name": "mcp__computer-use__computer_batch",
                                                   "tool_input": {"actions": [{"text": "make install"}]}}))

    def test_pretooluse_mcp_tool(self):
        a = Session(self, "a")
        out = a.hook("pretooluse", {"tool_name": "mcp__computer-use__computer_batch", "tool_input": {"actions": []}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(a.hook("pretooluse", {"tool_name": "mcp__computer-use__app_click", "tool_input": {}}))

    def test_pretooluse_lease_command_is_exempt_only_when_alone(self):
        a = Session(self, "a")
        self.assertIsNone(a.bash("claude-lease acquire app --name x --purpose 'make install then verify'"))
        self.assertIsNotNone(a.bash("claude-lease status && make install"))

    def test_hook_fails_open(self):
        a = Session(self, "a")
        r = a.run("hook", "pretooluse", stdin="not json")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertTrue(os.path.exists(os.path.join(self.home, "hook-errors.log")))
        with open(os.path.join(self.home, "resources.json"), "w") as f:
            f.write("{broken")
        r = a.run("hook", "pretooluse", stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": "make install"}}))
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_sessionstart_reports_state(self):
        a, b = Session(self, "a"), Session(self, "b")
        self.acquire(a)
        ctx = b.hook("sessionstart", {"source": "startup"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- app: held by S-a", ctx)
        self.assertIn("- screen: free", ctx)
        self.assertNotIn("absent", ctx)
        self.assertIn("first come, first served", ctx)

    def test_sessionend_releases_and_dequeues(self):
        a, b, c = Session(self, "a"), Session(self, "b"), Session(self, "c")
        self.acquire(a)
        self.acquire(b)
        self.assertIsNone(a.hook("sessionend", {}))
        self.assertIsNone(self.holder())
        self.assertIsNone(b.hook("sessionend", {}))
        self.assertEqual(self.acquire(c).returncode, 0)

    def test_status_json(self):
        a = Session(self, "a")
        self.acquire(a)
        snap = json.loads(a.run("status", "--json").stdout)
        self.assertEqual(snap["app"]["holder"]["session_id"], "a")
        self.assertNotIn("absent", snap)


if __name__ == "__main__":
    unittest.main()
