import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src import hook

PROJECT = Path(__file__).resolve().parents[1]


def payload(event: str, **extra) -> dict:
    return {"session_id": "abc-123", "hook_event_name": event, "cwd": r"C:\work\demo", **extra}


class StatusMappingTest(unittest.TestCase):
    CASES = [
        (payload("UserPromptSubmit", prompt="hi"), "running"),
        (payload("PostToolUse", tool_name="Bash"), "running"),
        (payload("PermissionRequest", tool_name="Bash"), "attention"),
        (payload("PreToolUse", tool_name="AskUserQuestion"), "attention"),
        (payload("PreToolUse", tool_name="Bash"), None),
        (payload("Notification", notification_type="permission_prompt"), "attention"),
        (payload("Notification", notification_type="elicitation_dialog"), "attention"),
        (payload("Notification", notification_type="agent_needs_input"), "attention"),
        (payload("Notification", notification_type="idle_prompt"), "answered"),
        (payload("Notification", notification_type="auth_success"), None),
        (payload("Stop"), "answered"),
        (payload("SessionStart", source="startup"), "answered"),
        (payload("SessionStart", source="compact"), None),
        (payload("StopFailure"), "error"),
        (payload("SessionEnd", reason="exit"), hook.DELETE),
        (payload("SubagentStop"), None),
    ]

    def test_table(self):
        for event_payload, expected in self.CASES:
            with self.subTest(event=event_payload["hook_event_name"], extra=event_payload):
                self.assertEqual(hook.status_for_event(event_payload), expected)


class HandleEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "sessions"

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, event_payload, seq, env=None, image=""):
        return hook.handle_event(event_payload, self.dir, "claude", seq, 4242, env or {}, image)

    def state(self):
        return json.loads((self.dir / "claude-abc-123.json").read_text(encoding="utf-8"))

    def test_lifecycle(self):
        self.apply(payload("SessionStart", source="startup"), 1, {"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"})
        self.assertEqual(self.state()["status"], "answered")
        self.assertEqual(self.state()["source"], "VS Code")
        self.apply(payload("UserPromptSubmit", prompt="  Fix   the\nbug please "), 2)
        self.assertEqual(self.state()["status"], "running")
        self.assertEqual(self.state()["first_prompt"], "Fix the bug please")
        self.apply(payload("UserPromptSubmit", prompt="second prompt"), 3)
        self.assertEqual(self.state()["first_prompt"], "Fix the bug please")
        self.apply(payload("PermissionRequest", tool_name="Bash"), 4)
        self.assertEqual(self.state()["status"], "attention")
        self.assertEqual(self.state()["attention_reason"], "Bash")
        self.apply(payload("PostToolUse", tool_name="Bash"), 5)
        self.assertEqual(self.state()["status"], "running")
        self.assertNotIn("attention_reason", self.state())
        self.apply(payload("Stop"), 6)
        self.assertEqual(self.state()["status"], "answered")
        self.assertEqual(self.state()["pid"], 4242)
        self.apply(payload("SessionEnd", reason="exit"), 7)
        self.assertFalse((self.dir / "claude-abc-123.json").exists())

    def test_out_of_order_event_is_ignored(self):
        self.apply(payload("Stop"), 20)
        self.assertIsNone(self.apply(payload("UserPromptSubmit", prompt="late"), 10))
        self.assertEqual(self.state()["status"], "answered")

    def test_ignored_event_writes_nothing(self):
        self.assertIsNone(self.apply(payload("Notification", notification_type="auth_success"), 1))
        self.assertFalse(self.dir.exists())

    def test_missing_session_id(self):
        self.assertIsNone(self.apply({"hook_event_name": "Stop"}, 1))

    def test_session_id_is_sanitised(self):
        self.apply({"session_id": "../../evil", "hook_event_name": "Stop"}, 1)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["claude-evil.json"])

    def test_shell_parent_pid_not_tracked(self):
        self.apply(payload("Stop"), 1, image="powershell.exe")
        self.assertEqual(self.state()["pid"], 0)
        self.apply(payload("Stop"), 2, image="claude.exe")
        self.assertEqual(self.state()["pid"], 4242)

    def test_long_prompt_truncated(self):
        self.apply(payload("UserPromptSubmit", prompt="x" * 500), 1)
        self.assertEqual(len(self.state()["first_prompt"]), 120)


class HookProcessTest(unittest.TestCase):
    """Run hook.py as Claude Code would: a separate process with JSON on stdin."""

    def run_hook(self, stdin: str, state_dir: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(PROJECT / "src" / "hook.py"), hook.MARKER, "--agent", "claude", "--state-dir", str(state_dir)],
            input=stdin, capture_output=True, text=True, timeout=20,
        )

    def test_end_to_end_and_never_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "sessions"
            result = self.run_hook(json.dumps(payload("PermissionRequest", tool_name="Bash")), state_dir)
            self.assertEqual((result.returncode, result.stdout), (0, ""))
            saved = json.loads((state_dir / "claude-abc-123.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "attention")
            for bad_input in ("", "not json", "[1,2]", "{\"session_id\": 5"):
                result = self.run_hook(bad_input, state_dir)
                self.assertEqual((result.returncode, result.stdout), (0, ""), bad_input)

    def test_unwritable_state_dir_still_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            result = self.run_hook(json.dumps(payload("Stop")), blocker / "sessions")
            self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
