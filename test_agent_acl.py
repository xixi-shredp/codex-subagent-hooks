import copy
import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing


HOOK = pathlib.Path(__file__).with_name("agent_acl_hook.py")
POLICY = pathlib.Path(__file__).with_name("agent_acl_policy.py")
spec = importlib.util.spec_from_file_location("agent_acl_policy", POLICY)
acl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acl)

DEFAULT_POLICY = {
    "version": 1,
    "identity": {"state_db": None},
    "communication": {
        "allowed_relations": ["parent", "siblings", "children"],
        "status_reads": "neighborhood",
    },
}

NODES = [
    ("root-id", None, None),
    ("a-id", "/root/orbit", "root-id"),
    ("b-id", "/root/harbor", "root-id"),
    ("c-id", "/root/orbit/birch", "a-id"),
    ("d-id", "/root/orbit/cedar", "a-id"),
    ("e-id", "/root/harbor/spruce", "b-id"),
    ("g-id", "/root/orbit/birch/larch", "c-id"),
    ("h-id", "/root/orbit/cedar/willow", "d-id"),
]
ALLOWED_PAIRS = {
    frozenset(pair) for pair in [
        ("root-id", "a-id"), ("root-id", "b-id"),
        ("a-id", "b-id"), ("a-id", "c-id"), ("a-id", "d-id"),
        ("b-id", "e-id"), ("c-id", "d-id"), ("c-id", "g-id"), ("d-id", "h-id"),
    ]
}


class AgentAclTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-acl-topology-")
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / "state.sqlite"
        self.history = self.db.with_name("thread_history_1.sqlite")
        self.policy_path = pathlib.Path(self.temp.name) / "policy.json"
        self.write_policy(DEFAULT_POLICY)
        with closing(sqlite3.connect(self.history)) as history, history:
            history.execute("CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT)")
            for agent_id, _, _ in NODES:
                history.execute("INSERT INTO thread_turns VALUES (?, ?)", (agent_id, "turn-" + agent_id))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("""
                CREATE TABLE threads(id TEXT PRIMARY KEY, agent_path TEXT, cwd TEXT);
                CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY);
            """)
            for agent_id, path, parent in NODES:
                db.execute("INSERT INTO threads VALUES (?, ?, ?)", (agent_id, path, str(acl.PROJECT)))
                if parent:
                    db.execute("INSERT INTO thread_spawn_edges VALUES (?, ?)", (parent, agent_id))

    def write_policy(self, policy):
        self.policy_path.write_text(json.dumps(policy), encoding="utf-8")

    def event(self, actor, tool="collaborationsend_message", **args):
        return {
            "cwd": str(acl.PROJECT), "session_id": "root-id",
            "agent_id": None if actor == "root-id" else actor,
            "turn_id": "turn-" + actor, "tool_name": tool, "tool_input": args,
        }

    def decide(self, event):
        return acl.decide(event, self.db, self.policy_path)

    def check_all_pairs(self):
        for actor, _, _ in NODES:
            for target, _, _ in NODES:
                with self.subTest(actor=actor, target=target):
                    expected = actor != target and frozenset((actor, target)) in ALLOWED_PAIRS
                    self.assertEqual(self.decide(self.event(actor, target=target))[0], expected)

    def test_all_directed_pairs_of_multilevel_tree(self):
        self.check_all_pairs()

    def test_names_and_path_depth_do_not_determine_relationships(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            for index, (agent_id, _, _) in enumerate(NODES[1:]):
                db.execute("UPDATE threads SET agent_path=? WHERE id=?", ("/root/renamed_" + str(index), agent_id))
        self.check_all_pairs()

    def test_targets_and_other_communication_entrypoints(self):
        self.assertTrue(self.decide(self.event("a-id", target="birch"))[0])
        self.assertTrue(self.decide(self.event("c-id", target="/root/orbit/cedar"))[0])
        for tool in ("collaboration.followup_task", "collaborationinterrupt_agent"):
            self.assertTrue(self.decide(self.event("c-id", tool, target="g-id"))[0])
            self.assertFalse(self.decide(self.event("c-id", tool, target="root-id"))[0])
            self.assertFalse(self.decide(self.event("e-id", tool, target="c-id"))[0])
        with self.assertRaises(acl.PolicyError):
            self.decide(self.event("c-id", target="../cedar"))

    def test_every_node_can_spawn_direct_children(self):
        for actor, _, _ in NODES:
            with self.subTest(actor=actor):
                self.assertTrue(self.decide(self.event(actor, "collaborationspawn_agent", task_name="arbitrary_child"))[0])

    def test_configured_relations_and_status_policy(self):
        policy = copy.deepcopy(DEFAULT_POLICY)
        policy["communication"]["allowed_relations"] = ["siblings"]
        self.write_policy(policy)
        self.assertTrue(self.decide(self.event("c-id", target="d-id"))[0])
        self.assertFalse(self.decide(self.event("c-id", target="a-id"))[0])
        self.assertFalse(self.decide(self.event("c-id", target="g-id"))[0])
        self.assertFalse(self.decide(self.event("c-id", "collaborationspawn_agent", task_name="child"))[0])
        policy["communication"]["status_reads"] = "deny"
        self.write_policy(policy)
        self.assertFalse(self.decide(self.event("c-id", "collaborationlist_agents", path_prefix="d-id"))[0])

    def test_global_status_and_filtered_neighborhood(self):
        for actor, _, _ in NODES:
            for tool in ("collaborationlist_agents", "collaborationwait_agent"):
                self.assertFalse(self.decide(self.event(actor, tool))[0])
        self.assertTrue(self.decide(self.event("c-id", "collaborationlist_agents", path_prefix="g-id"))[0])
        self.assertFalse(self.decide(self.event("c-id", "collaborationlist_agents", path_prefix="a-id"))[0])
        self.assertFalse(self.decide(self.event("root-id", "collaborationlist_agents", path_prefix="a-id"))[0])
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM thread_spawn_edges WHERE parent_thread_id != 'root-id'")
            db.execute("DELETE FROM threads WHERE id NOT IN ('root-id','a-id','b-id')")
        for actor in ("root-id", "a-id", "b-id"):
            self.assertTrue(self.decide(self.event(actor, "collaborationwait_agent"))[0])

    def test_missing_forged_and_ambiguous_identity(self):
        cases = [
            dict(self.event("c-id", target="a-id"), agent_id=None),
            dict(self.event("c-id", target="a-id"), agent_id="root-id"),
            dict(self.event("c-id", target="a-id"), turn_id="missing-turn"),
            dict(self.event("c-id", target="a-id"), session_id="missing-session"),
        ]
        for event in cases:
            with self.subTest(event=event), self.assertRaises(acl.PolicyError):
                self.decide(event)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO threads VALUES (?, ?, ?)", ("duplicate-id", "/root/orbit/birch", str(acl.PROJECT)))
            db.execute("INSERT INTO thread_spawn_edges VALUES (?, ?)", ("a-id", "duplicate-id"))
        with self.assertRaises(acl.PolicyError):
            self.decide(self.event("c-id", target="a-id"))

    def test_other_sessions_with_identical_paths_do_not_cross(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO threads VALUES (?, ?, ?)", ("other-root", None, str(acl.PROJECT)))
            db.execute("INSERT INTO threads VALUES (?, ?, ?)", ("other-c", "/root/orbit/birch", str(acl.PROJECT)))
            db.execute("INSERT INTO thread_spawn_edges VALUES (?, ?)", ("other-root", "other-c"))
        self.assertTrue(self.decide(self.event("c-id", target="a-id"))[0])
        with self.assertRaises(acl.PolicyError):
            self.decide(self.event("c-id", target="other-c"))
        with self.assertRaises(acl.PolicyError):
            self.decide(dict(self.event("c-id", target="a-id"), session_id="other-root"))

    def test_invalid_or_missing_policy_is_rejected(self):
        invalid = [
            dict(DEFAULT_POLICY, version=2),
            dict(DEFAULT_POLICY, version=True),
            dict(DEFAULT_POLICY, unknown=True),
            dict(DEFAULT_POLICY, identity={"state_db": "relative.sqlite"}),
            dict(DEFAULT_POLICY, communication={"allowed_relations": ["ancestor"], "status_reads": "neighborhood"}),
            dict(DEFAULT_POLICY, communication={"allowed_relations": ["parent", "parent"], "status_reads": "neighborhood"}),
            dict(DEFAULT_POLICY, communication={"allowed_relations": "parent", "status_reads": "neighborhood"}),
        ]
        for policy in invalid:
            with self.subTest(policy=policy):
                self.write_policy(policy)
                with self.assertRaises(acl.PolicyError):
                    self.decide(self.event("c-id", target="a-id"))
        self.policy_path.write_text("{")
        with self.assertRaises(ValueError):
            self.decide(self.event("c-id", target="a-id"))
        self.policy_path.unlink()
        with self.assertRaises(OSError):
            self.decide(self.event("c-id", target="a-id"))

    def test_real_hook_json_and_missing_database(self):
        env = dict(os.environ, PROJECT_AGENT_ACL_DB=str(self.db))
        for target, expected in (("a-id", True), ("e-id", False)):
            output = subprocess.check_output(
                ["/usr/bin/python3", str(HOOK)], input=json.dumps(self.event("c-id", target=target)),
                text=True, env=env,
            )
            result = json.loads(output)
            self.assertEqual(result == {}, expected)
            if not expected:
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        env["PROJECT_AGENT_ACL_DB"] = str(self.db.with_name("missing.sqlite"))
        output = subprocess.check_output(
            ["/usr/bin/python3", str(HOOK)], input=json.dumps(self.event("c-id", target="a-id")),
            text=True, env=env,
        )
        self.assertEqual(json.loads(output)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_other_directories_and_non_collaboration_tools(self):
        self.policy_path.unlink()
        event = dict(self.event("c-id", target="root-id"), cwd="/tmp/other-project")
        self.assertTrue(self.decide(event)[0])
        event = self.event("c-id", "Bash")
        self.assertTrue(self.decide(event)[0])

    def test_unknown_collaboration_operation_is_rejected(self):
        self.assertFalse(self.decide(self.event("c-id", "collaboration_unknown_tool"))[0])

    def test_watchdog_denies_worker_failure_and_timeout(self):
        with tempfile.TemporaryDirectory(prefix="agent-acl-watchdog-") as directory:
            wrapper = pathlib.Path(directory) / "agent_acl_hook.py"
            shutil.copyfile(HOOK, wrapper)
            shutil.copyfile(HOOK.with_name("hook_scope.py"), wrapper.with_name("hook_scope.py"))
            wrapper.with_name("installation.json").write_text(json.dumps({"mode": "local", "project": str(acl.PROJECT)}))
            worker = wrapper.with_name("agent_acl_policy.py")
            for code in (None, "print('not-json')", "import time\ntime.sleep(10)"):
                with self.subTest(worker=code):
                    if code is not None:
                        worker.write_text(code)
                    result = subprocess.check_output(
                        ["/usr/bin/python3", str(wrapper)], input="{}", text=True, timeout=8,
                    )
                    self.assertEqual(json.loads(result)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_watchdog_does_not_affect_other_directories(self):
        with tempfile.TemporaryDirectory(prefix="agent-acl-scope-") as directory:
            wrapper = pathlib.Path(directory) / "agent_acl_hook.py"
            shutil.copyfile(HOOK, wrapper)
            shutil.copyfile(HOOK.with_name("hook_scope.py"), wrapper.with_name("hook_scope.py"))
            wrapper.with_name("installation.json").write_text(json.dumps({"mode": "local", "project": str(acl.PROJECT)}))
            result = subprocess.check_output(
                ["/usr/bin/python3", str(wrapper)], input=json.dumps({"cwd": "/tmp/unrelated-agent-acl-project"}),
                text=True,
            )
            self.assertEqual(json.loads(result), {})


if __name__ == "__main__":
    unittest.main()
