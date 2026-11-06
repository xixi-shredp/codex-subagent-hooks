#!/usr/bin/env python3
"""Project-local, name-independent topology policy for Codex collaboration."""

import json
import os
import pathlib
import re
import sqlite3
import sys
from contextlib import closing
from urllib.parse import quote
from hook_scope import project_root


PROJECT = project_root()
DEFAULT_DB = pathlib.Path(os.environ.get("CODEX_HOME", str(pathlib.Path.home() / ".codex"))) / "state_5.sqlite"
CONFIG = pathlib.Path(__file__).with_suffix(".json")
AUDIT = pathlib.Path(__file__).with_name("agent-acl-audit.jsonl")
PATH_PATTERN = re.compile(r"/root(?:/[A-Za-z0-9_-]+)*\Z")
RELATIVE_PATTERN = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")


class PolicyError(Exception):
    pass


def load_policy(path=CONFIG):
    if pathlib.Path(path).stat().st_size > 65536:
        raise PolicyError("policy is too large")
    with open(path, encoding="utf-8") as stream:
        policy = json.load(stream)
    if not isinstance(policy, dict) or set(policy) != {"version", "identity", "communication"}:
        raise PolicyError("invalid policy fields")
    if type(policy["version"]) is not int or policy["version"] != 1:
        raise PolicyError("unsupported policy version")
    identity = policy["identity"]
    if not isinstance(identity, dict) or set(identity) != {"state_db"}:
        raise PolicyError("invalid identity configuration")
    state_db = identity["state_db"]
    if state_db is not None and (not isinstance(state_db, str) or not pathlib.Path(state_db).is_absolute()):
        raise PolicyError("state database path must be absolute")
    communication = policy["communication"]
    if not isinstance(communication, dict) or set(communication) != {"allowed_relations", "status_reads"}:
        raise PolicyError("invalid communication configuration")
    relations = communication["allowed_relations"]
    if not isinstance(relations, list) or any(type(value) is not str for value in relations):
        raise PolicyError("invalid relationship list")
    if len(relations) != len(set(relations)) or not set(relations) <= {"parent", "siblings", "children"}:
        raise PolicyError("invalid or duplicate relationship")
    if communication["status_reads"] not in ("neighborhood", "deny"):
        raise PolicyError("invalid status read policy")
    return policy


def _turn_owner(turn_id, db_path):
    if not isinstance(turn_id, str) or not turn_id:
        raise PolicyError("turn identity unavailable")
    history_path = pathlib.Path(db_path).with_name("thread_history_1.sqlite")
    uri = "file:" + quote(str(history_path), safe="/") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as db:
        db.execute("PRAGMA query_only = ON")
        rows = db.execute(
            "SELECT thread_id FROM thread_turns WHERE turn_id = ? LIMIT 2",
            (turn_id,),
        ).fetchall()
    if len(rows) != 1:
        raise PolicyError("turn identity unavailable or ambiguous")
    return rows[0][0]


def _family(root_id, db_path):
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as db:
        db.execute("PRAGMA query_only = ON")
        rows = db.execute(
            """
            WITH RECURSIVE family(id, depth) AS (
                SELECT ?, 0
                UNION ALL
                SELECT edge.child_thread_id, family.depth + 1
                FROM thread_spawn_edges AS edge
                JOIN family ON edge.parent_thread_id = family.id
                WHERE family.depth < 64
            )
            SELECT thread.id, thread.agent_path, thread.cwd, edge.parent_thread_id
            FROM family JOIN threads AS thread ON thread.id = family.id
            LEFT JOIN thread_spawn_edges AS edge ON edge.child_thread_id = thread.id
            """,
            (root_id,),
        ).fetchall()
    by_id = {}
    by_path = {}
    parents = {}
    for agent_id, path, cwd, parent_id in rows:
        if PROJECT is not None and (not cwd or pathlib.Path(cwd).resolve() != PROJECT):
            continue
        path = "/root" if agent_id == root_id else path
        if not isinstance(path, str) or not PATH_PATTERN.fullmatch(path):
            raise PolicyError("agent path unavailable")
        if agent_id in by_id or path in by_path:
            raise PolicyError("ambiguous agent identity")
        by_id[agent_id] = path
        by_path[path] = agent_id
        parents[agent_id] = parent_id
    if by_id.get(root_id) != "/root":
        raise PolicyError("root session unavailable")
    if parents[root_id] is not None:
        raise PolicyError("session is not the root of the agent tree")
    for agent_id, parent_id in parents.items():
        if agent_id != root_id and (parent_id not in by_id or parent_id == agent_id):
            raise PolicyError("agent parent identity unavailable")
    return by_id, by_path, parents


def _target_path(raw, actor, by_id, by_path):
    if not isinstance(raw, str) or not raw:
        raise PolicyError("target unavailable")
    if raw in by_id:
        return by_id[raw]
    if raw.startswith("/"):
        target = raw
    elif RELATIVE_PATTERN.fullmatch(raw):
        target = actor + "/" + raw
    else:
        raise PolicyError("invalid target")
    if not PATH_PATTERN.fullmatch(target):
        raise PolicyError("invalid target")
    if target not in by_path:
        raise PolicyError("target identity unavailable")
    return target


def relationship(actor_id, target_id, parents):
    if actor_id == target_id:
        return None
    if parents.get(actor_id) == target_id:
        return "parent"
    if parents.get(target_id) == actor_id:
        return "children"
    parent_id = parents.get(actor_id)
    if parent_id is not None and parent_id == parents.get(target_id):
        return "siblings"
    return None


def decide(event, db_path=None, policy_path=CONFIG):
    cwd = event.get("cwd")
    if not isinstance(cwd, str):
        raise PolicyError("working directory unavailable")
    if PROJECT is not None and pathlib.Path(cwd).resolve() != PROJECT:
        return True, "outside project", None, None

    tool = event.get("tool_name")
    if not isinstance(tool, str):
        raise PolicyError("tool name unavailable")
    name = "".join(ch.lower() for ch in tool if ch.isalnum())
    if not name.startswith("collaboration"):
        return True, "outside collaboration policy", None, None

    policy = load_policy(policy_path)
    communication = policy["communication"]
    relations = set(communication["allowed_relations"])
    if db_path is None:
        db_path = pathlib.Path(os.environ.get("PROJECT_AGENT_ACL_DB") or policy["identity"]["state_db"] or DEFAULT_DB)

    root_id = event.get("session_id")
    if not isinstance(root_id, str) or not root_id:
        raise PolicyError("root session unavailable")
    owner_id = _turn_owner(event.get("turn_id"), db_path)
    agent_id = event.get("agent_id")
    if agent_id is None:
        if owner_id != root_id or event.get("agent_type"):
            raise PolicyError("subagent identity unavailable")
    elif not isinstance(agent_id, str) or agent_id != owner_id:
        raise PolicyError("agent identity does not match turn owner")
    by_id, by_path, parents = _family(root_id, db_path)
    actor = by_id.get(owner_id)
    if actor is None:
        raise PolicyError("caller identity unavailable")
    args = event.get("tool_input")
    if not isinstance(args, dict):
        raise PolicyError("tool input unavailable")

    if name == "collaborationspawnagent":
        task = args.get("task_name")
        if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task):
            raise PolicyError("new agent name unavailable")
        target = actor + "/" + task
        return "children" in relations, "children" if "children" in relations else "children relation is not permitted", actor, target
    elif name in ("collaborationsendmessage", "collaborationfollowuptask", "collaborationinterruptagent"):
        target = _target_path(args.get("target"), actor, by_id, by_path)
    elif name in ("collaborationlistagents", "collaborationwaitagent"):
        if communication["status_reads"] == "deny":
            return False, "status access is disabled", actor, None
        visible = set(by_id)
        if name == "collaborationlistagents" and args.get("path_prefix") is not None:
            prefix = _target_path(args["path_prefix"], actor, by_id, by_path)
            visible = {agent_id for agent_id, path in by_id.items() if path == prefix or path.startswith(prefix + "/")}
        if any(other != owner_id and relationship(owner_id, other, parents) not in relations for other in visible):
            return False, "status operation may expose agents outside the permitted neighborhood", actor, None
        return True, "allowed status read", actor, None
    else:
        return False, "unrecognized collaboration operation", actor, None

    relation = relationship(owner_id, by_path[target], parents)
    if relation not in relations:
        return False, "only configured parent, sibling, or child relationships are permitted", actor, target
    return True, "allowed " + relation, actor, target


def _audit(event, allowed, reason, actor, target):
    record = {
        "session_id": event.get("session_id"),
        "agent_id": event.get("agent_id"),
        "agent_type": event.get("agent_type"),
        "turn_id": event.get("turn_id"),
        "agent_path": actor,
        "tool_name": event.get("tool_name"),
        "target": target,
        "allowed": allowed,
        "reason": reason,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(AUDIT, flags, 0o600)
        try:
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def main():
    event = {}
    actor = target = None
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise PolicyError("invalid hook input")
        allowed, reason, actor, target = decide(event)
    except (PolicyError, OSError, sqlite3.Error, ValueError, TypeError):
        allowed, reason = False, "agent identity or policy could not be verified"
    if reason != "outside project" and isinstance(event, dict) and "collaboration" in str(event.get("tool_name", "")).lower():
        _audit(event, allowed, reason, actor, target)
    if allowed:
        print("{}")
    else:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))


if __name__ == "__main__":
    main()
