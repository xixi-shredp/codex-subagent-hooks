#!/usr/bin/env python3
"""Install without modifying unrelated hook registrations or trust settings."""
import argparse
import copy
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
RUNTIME = ("agent_acl_hook.py", "agent_acl_policy.py", "hook_scope.py")


def destination(args):
    if args.global_install:
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve(), {"mode": "global"}
    project = Path(args.local).expanduser().resolve()
    if not project.is_dir():
        raise ValueError("project directory does not exist: " + str(project))
    return project / ".codex", {"mode": "local", "project": str(project)}


def atomic_write(path, data):
    fd, temp = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def merge_hooks(existing, command, old_commands):
    value = copy.deepcopy(existing)
    if not isinstance(value, dict):
        raise ValueError("hooks.json must be an object")
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(entries, list):
        raise ValueError("PreToolUse must be a list")
    kept = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            raise ValueError("invalid PreToolUse registration")
        if any(not isinstance(hook, dict) for hook in entry["hooks"]):
            raise ValueError("invalid hook registration")
        remaining = [hook for hook in entry["hooks"]
                     if not (hook.get("type") == "command" and hook.get("command") in old_commands | {command})]
        if remaining:
            entry["hooks"] = remaining
            kept.append(entry)
        elif not entry["hooks"]:
            kept.append(entry)
    kept.append({"matcher": ".*", "hooks": [{"type": "command", "command": command}]})
    hooks["PreToolUse"] = kept
    return value


def install(config_dir, scope):
    config_dir = Path(config_dir)
    runtime_dir = config_dir / "subagent-hooks"
    registration = config_dir / "hooks.json"
    policy = runtime_dir / "agent_acl_policy.json"
    # Validate before writing. Reject symlink destinations rather than replacing their targets.
    for path in (config_dir, runtime_dir, registration, policy,
                 *(runtime_dir / name for name in (*RUNTIME, "installation.json"))):
        if path.is_symlink():
            raise ValueError("symlink destination is not supported: " + str(path))
    before = registration.read_bytes() if registration.exists() else None
    existing = json.loads(before) if before is not None else {}
    interpreter = str(Path(sys.executable).resolve())
    command = shlex.join([interpreter, str(runtime_dir / "agent_acl_hook.py")])
    old_commands = set()
    if scope["mode"] == "local":
        # Migrate only the previous known registration at this same installation root.
        legacy = str(config_dir / "agent_acl_hook.py")
        old_commands.update(shlex.join([python, legacy]) for python in ("/usr/bin/python3", interpreter))
    merged = merge_hooks(existing, command, old_commands)
    payloads = {name: (SOURCE / name).read_bytes() for name in RUNTIME}
    default_policy = (SOURCE / "agent_acl_policy.json").read_bytes()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    for name, content in payloads.items():
        atomic_write(runtime_dir / name, content)
    atomic_write(runtime_dir / "installation.json", encoded(scope))
    if not policy.exists():
        # Carry forward the original local policy on the first packaged installation.
        legacy_policy = config_dir / "agent_acl_policy.json"
        content = legacy_policy.read_bytes() if scope["mode"] == "local" and legacy_policy.is_file() else default_policy
        atomic_write(policy, content)
    if merged != existing:
        if before is not None:
            fd, backup = tempfile.mkstemp(prefix="hooks.json.backup-", dir=config_dir)
            with os.fdopen(fd, "wb") as stream:
                stream.write(before)
            print("Backup: " + backup)
        atomic_write(registration, encoded(merged))
    print("Installed: " + str(runtime_dir))
    print("Registration: " + str(registration))
    print("Review/trust the hook in Codex, then start a new session. No trust settings were changed.")
    return runtime_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--global", dest="global_install", action="store_true", help="install into CODEX_HOME or ~/.codex")
    group.add_argument("--local", metavar="PROJECT", help="install into PROJECT/.codex")
    args = parser.parse_args()
    try:
        config_dir, scope = destination(args)
        install(config_dir, scope)
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, "Installation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
