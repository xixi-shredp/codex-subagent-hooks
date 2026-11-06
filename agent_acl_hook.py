#!/usr/bin/env python3
"""Fail-closed watchdog for the project collaboration policy."""

import json
import pathlib
import subprocess
import sys


WORKER = pathlib.Path(__file__).with_name("agent_acl_policy.py")
FALLBACK = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "agent communication policy failed or timed out",
    }
}


def valid_output(value):
    if value == {}:
        return True
    if not isinstance(value, dict) or set(value) != {"hookSpecificOutput"}:
        return False
    decision = value["hookSpecificOutput"]
    return (
        isinstance(decision, dict)
        and decision.get("hookEventName") == "PreToolUse"
        and decision.get("permissionDecision") == "deny"
        and isinstance(decision.get("permissionDecisionReason"), str)
        and bool(decision["permissionDecisionReason"])
    )


def main():
    result = FALLBACK
    try:
        from hook_scope import project_root
        project = project_root()
        event = sys.stdin.buffer.read()
        request = json.loads(event)
        if isinstance(request, dict) and isinstance(request.get("cwd"), str):
            if project is not None and pathlib.Path(request["cwd"]).resolve() != project:
                print("{}")
                return
        process = subprocess.run(
            ["/usr/bin/python3", str(WORKER)],
            input=event,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=True,
        )
        parsed = json.loads(process.stdout)
        if valid_output(parsed):
            result = parsed
    except Exception:
        pass
    print(json.dumps(result))


if __name__ == "__main__":
    main()
