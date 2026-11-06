import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="subagent-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project ' spaces ; $()"
        self.project.mkdir()
        self.config = self.project / ".codex"

    def run_install(self, target, scope):
        with contextlib.redirect_stdout(io.StringIO()):
            return install.install(target, scope)

    def local(self):
        return self.run_install(self.config, {"mode": "local", "project": str(self.project)})

    def test_local_cli_paths_existing_hooks_upgrade_and_policy(self):
        self.config.mkdir()
        unrelated = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo existing"}]}
        original = {"hooks": {"PreToolUse": [unrelated], "Stop": []}, "extra": 7}
        hooks = self.config / "hooks.json"
        hooks.write_text(json.dumps(original))
        command = [str(install.SOURCE / "install.sh"), "--local", str(self.project)]
        subprocess.run(command, check=True, capture_output=True)
        installed = self.config / "subagent-hooks"
        current = json.loads(hooks.read_text())
        self.assertEqual(current["hooks"]["PreToolUse"][0], unrelated)
        self.assertEqual(current["hooks"]["Stop"], [])
        self.assertEqual(current["extra"], 7)
        self.assertEqual(len(list(self.config.glob("hooks.json.backup-*"))), 1)
        policy = installed / "agent_acl_policy.json"
        policy.write_text('{"custom": "preserve verbatim"}\n')
        subprocess.run(command, check=True, capture_output=True)
        self.assertEqual(json.loads(hooks.read_text()), current)
        self.assertEqual(policy.read_text(), '{"custom": "preserve verbatim"}\n')
        self.assertEqual(len(list(self.config.glob("hooks.json.backup-*"))), 1)
        # Run exactly the registered shell command from an unrelated cwd.
        result = subprocess.run(current["hooks"]["PreToolUse"][-1]["hooks"][0]["command"],
                                shell=True, cwd=self.root, input=json.dumps({"cwd": str(self.root)}),
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), {})

    def test_invalid_registration_does_not_mutate_installation(self):
        self.config.mkdir()
        hooks = self.config / "hooks.json"
        for content in ('{', '{"hooks":{"PreToolUse":{}}}', '{"hooks":{"PreToolUse":[null]}}'):
            hooks.write_text(content)
            with self.assertRaises(ValueError):
                self.local()
            self.assertEqual(hooks.read_text(), content)
            self.assertFalse((self.config / "subagent-hooks").exists())

    def test_legacy_migration_and_custom_policy(self):
        self.config.mkdir()
        # Real legacy command uses shell quoting, including paths with spaces.
        import shlex
        legacy = shlex.join(["/usr/bin/python3", str(self.config / "agent_acl_hook.py")])
        (self.config / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": ".*", "hooks": [{"type": "command", "command": legacy}]}]}}))
        (self.config / "agent_acl_policy.json").write_text('{"legacy": true}')
        runtime = self.local()
        self.assertEqual((runtime / "agent_acl_policy.json").read_text(), '{"legacy": true}')
        self.assertEqual(len(json.loads((self.config / "hooks.json").read_text())["hooks"]["PreToolUse"]), 1)

    def test_global_destination_and_cli_in_isolated_home(self):
        # Mock the home lookup; never change HOME or CODEX_HOME or touch real user settings.
        with patch.object(install.Path, "home", return_value=self.root), \
                patch.object(install.os.environ, "get", return_value=None), \
                patch("sys.argv", ["install.py", "--global"]), \
                contextlib.redirect_stdout(io.StringIO()):
            install.main()
        runtime = self.root / ".codex" / "subagent-hooks"
        self.assertEqual(json.loads((runtime / "installation.json").read_text()), {"mode": "global"})

    def test_global_runtime_multiple_cwds_and_local_scope(self):
        runtime = self.run_install(self.root / "global", {"mode": "global"})
        db_path = self.root / "state.sqlite"
        with contextlib.closing(sqlite3.connect(db_path)) as db, db:
            db.executescript("CREATE TABLE threads(id TEXT,agent_path TEXT,cwd TEXT);"
                             "CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);")
            db.executemany("INSERT INTO threads VALUES (?,?,?)", [
                ("r", None, str(self.root)), ("a", "/root/a", str(self.project)),
                ("c", "/root/a/c", "/different/worktree")])
            db.executemany("INSERT INTO thread_spawn_edges VALUES (?,?)", [("r", "a"), ("a", "c")])
        with contextlib.closing(sqlite3.connect(self.root / "thread_history_1.sqlite")) as db, db:
            db.execute("CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT)")
            db.execute("INSERT INTO thread_turns VALUES ('c','turn-c')")
        env = dict(os.environ, PROJECT_AGENT_ACL_DB=str(db_path))
        for cwd in (str(self.project), "/different/worktree"):
            for target, allowed in (("a", True), ("r", False)):
                event = {"cwd": cwd, "session_id": "r", "agent_id": "c", "turn_id": "turn-c",
                         "tool_name": "collaborationsend_message", "tool_input": {"target": target}}
                result = subprocess.check_output(["python3", str(runtime / "agent_acl_hook.py")],
                                                 input=json.dumps(event), text=True, env=env)
                self.assertEqual(json.loads(result) == {}, allowed)
        local = self.local()
        event["cwd"] = str(self.root)
        result = subprocess.check_output(["python3", str(local / "agent_acl_hook.py")],
                                         input=json.dumps(event), text=True, env=env)
        self.assertEqual(json.loads(result), {})
        (runtime / "installation.json").unlink()
        result = subprocess.check_output(["python3", str(runtime / "agent_acl_hook.py")], input="{}", text=True)
        self.assertEqual(json.loads(result)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_symlink_registration_rejected(self):
        self.config.mkdir()
        original = self.root / "original.json"
        original.write_text("{}")
        (self.config / "hooks.json").symlink_to(original)
        with self.assertRaises(ValueError):
            self.local()
        self.assertEqual(original.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
