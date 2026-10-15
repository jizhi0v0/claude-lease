#!/usr/bin/env python3
"""Install claude-lease for this user on this host.

  ./install.py                     install the CLI (a copy, so editing this checkout
                                   changes nothing until the next install), create an
                                   empty resources.json
  ./install.py --hooks             also register the hooks in ~/.claude/settings.json
  ./install.py --hooks --dry-run   print what --hooks would write, change nothing
  ./install.py --settings PATH     register the hooks in another settings file
  ./install.py --remove-hooks      take the hooks out again

Re-run with --hooks after changing the "tool" patterns in resources.json: the
PreToolUse matcher is built from them so other tools never start the hook.
"""

import json
import os
import shutil
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(REPO, "bin", "claude-lease")
LINK = os.path.expanduser("~/.local/bin/claude-lease")
HOME = os.path.expanduser(os.environ.get("CLAUDE_LEASE_HOME", "~/.claude/lease"))
CONFIG = os.path.join(HOME, "resources.json")
INSTALLED = os.path.join(HOME, "bin", "claude-lease")
MARK = "claude-lease hook "


def install_cli():
    """Copy the CLI out of the checkout so that editing the checkout never
    changes what running sessions' hooks execute until the next install."""
    source = open(BIN).read()
    compile(source, BIN, "exec")  # refuse to install a file that does not parse
    os.makedirs(os.path.dirname(INSTALLED), exist_ok=True)
    tmp = INSTALLED + ".tmp"
    with open(tmp, "w") as f:
        f.write(source)
    os.chmod(tmp, 0o755)
    os.replace(tmp, INSTALLED)
    print("Installed %s" % INSTALLED)
    os.makedirs(os.path.dirname(LINK), exist_ok=True)
    if os.path.islink(LINK) or os.path.exists(LINK):
        target = os.path.realpath(LINK)
        if target == os.path.realpath(INSTALLED):
            return
        if target != os.path.realpath(BIN):
            sys.exit("%s exists and is not claude-lease's; move it away first." % LINK)
        # An older install linked straight to the checkout; swap atomically so a
        # hook starting right now still finds a file.
    tmp_link = LINK + ".tmp"
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(INSTALLED, tmp_link)
    os.replace(tmp_link, LINK)
    print("Linked %s -> %s" % (LINK, INSTALLED))


def ensure_config():
    os.makedirs(HOME, exist_ok=True)
    if os.path.exists(CONFIG):
        json.load(open(CONFIG))  # fail loudly on a broken file
        print("Config kept: %s" % CONFIG)
        return
    with open(CONFIG, "w") as f:
        json.dump({"resources": {}}, f, indent=2)
        f.write("\n")
    print("Created empty %s (see examples/resources.json)" % CONFIG)


def matcher():
    resources = (json.load(open(CONFIG)).get("resources") or {}) if os.path.exists(CONFIG) else {}
    tools = []
    for spec in resources.values():
        for rule in spec.get("match") or []:
            if rule.get("tool") and rule["tool"] not in tools:
                tools.append(rule["tool"])
    return "|".join(tools)


def without_ours(hooks):
    out = {}
    for event, groups in hooks.items():
        kept = []
        for g in groups:
            hs = [h for h in g.get("hooks", []) if MARK not in h.get("command", "")]
            if hs:
                kept.append(dict(g, hooks=hs))
        if kept:
            out[event] = kept
    return out


def ours(match):
    cmd = lambda ev: {"type": "command", "command": "%s hook %s" % (LINK, ev), "timeout": 10}
    h = {"SessionStart": [{"hooks": [cmd("sessionstart")]}],
         "SessionEnd": [{"hooks": [cmd("sessionend")]}]}
    if match:
        h["PreToolUse"] = [{"matcher": match, "hooks": [cmd("pretooluse")]}]
    return h


def write_settings(path, remove, dry_run):
    settings = json.load(open(path)) if os.path.exists(path) else {}
    hooks = without_ours(settings.get("hooks") or {})
    if not remove:
        for event, groups in ours(matcher()).items():
            hooks.setdefault(event, []).extend(groups)
    new = dict(settings)
    if hooks:
        new["hooks"] = hooks
    else:
        new.pop("hooks", None)
    text = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    json.loads(text)
    if dry_run:
        print(json.dumps({"hooks": hooks}, indent=2, ensure_ascii=False))
        return
    if os.path.exists(path):
        backup = "%s.bak-claude-lease-%s-%09d" % (path, time.strftime("%Y%m%d-%H%M%S"), time.time_ns() % 10**9)
        if os.path.exists(backup):
            sys.exit("backup %s already exists; not overwriting it" % backup)
        shutil.copy2(path, backup)
        print("Backed up %s -> %s" % (path, backup))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".claude-lease.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    json.load(open(tmp))
    os.replace(tmp, path)
    print("%s claude-lease hooks in %s" % ("Removed" if remove else "Registered", path))


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return
    settings = os.path.expanduser("~/.claude/settings.json")
    if "--settings" in argv:
        settings = os.path.abspath(argv[argv.index("--settings") + 1])
    dry = "--dry-run" in argv
    if not dry:
        install_cli()
        ensure_config()
    if "--hooks" in argv or "--remove-hooks" in argv or "--settings" in argv:
        write_settings(settings, "--remove-hooks" in argv, dry)


if __name__ == "__main__":
    main(sys.argv[1:])
