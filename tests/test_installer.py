import json
import tempfile
import unittest
from pathlib import Path

from src import installer
from src.hook import MARKER

HANDLER = installer.build_handler(r"C:\Python\pythonw.exe", Path(r"C:\overlay\src\hook.py"), Path(r"C:\data\sessions"))

THEIR_HOOKS = {
    "model": "opus",
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}],
    },
}


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name) / ".claude"
        self.config_dir.mkdir()
        self.settings_file = self.config_dir / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        return json.loads(self.settings_file.read_text(encoding="utf-8"))

    def test_handler_is_exec_form_async(self):
        self.assertEqual(HANDLER["command"], r"C:\Python\pythonw.exe")
        self.assertIn(MARKER, HANDLER["args"])
        self.assertTrue(HANDLER["async"])

    def test_install_preserves_existing_and_is_idempotent(self):
        self.settings_file.write_text(json.dumps(THEIR_HOOKS), encoding="utf-8")
        installer.install([self.config_dir], HANDLER)
        installer.install([self.config_dir], HANDLER)
        data = self.read()
        self.assertEqual(data["model"], "opus")
        self.assertIn({"hooks": [{"type": "command", "command": "notify-send done"}]}, data["hooks"]["Stop"])
        for event, matcher in installer.HOOK_EVENTS:
            ours = [g for g in data["hooks"][event] if installer._is_ours(g["hooks"][0])]
            self.assertEqual(len(ours), 1, event)
            self.assertEqual(ours[0].get("matcher"), matcher)
        self.assertTrue(installer.hooks_installed(self.config_dir))
        self.assertGreaterEqual(len(list(self.config_dir.glob("settings.json.bak-*"))), 1)

    def test_install_creates_missing_settings(self):
        installer.install([self.config_dir], HANDLER)
        self.assertTrue(installer.hooks_installed(self.config_dir))

    def test_invalid_json_aborts_without_writing(self):
        self.settings_file.write_text("{ broken", encoding="utf-8")
        with self.assertRaises(installer.InstallError):
            installer.install([self.config_dir], HANDLER)
        self.assertEqual(self.settings_file.read_text(encoding="utf-8"), "{ broken")

    def test_uninstall_removes_only_ours(self):
        self.settings_file.write_text(json.dumps(THEIR_HOOKS), encoding="utf-8")
        installer.install([self.config_dir], HANDLER)
        installer.uninstall([self.config_dir])
        self.assertEqual(self.read(), THEIR_HOOKS)
        self.assertFalse(installer.hooks_installed(self.config_dir))

    def test_uninstall_drops_empty_hooks_key(self):
        installer.install([self.config_dir], HANDLER)
        installer.uninstall([self.config_dir])
        self.assertEqual(self.read(), {})

    def test_shell_form_fallback(self):
        handler = installer.build_handler(r"C:\Py\pythonw.exe", Path("h.py"), Path("s"), shell_form=True)
        self.assertEqual(handler["shell"], "powershell")
        self.assertIn(r"C:\Py\python.exe", handler["command"])
        self.assertIn(MARKER, handler["command"])
        self.assertTrue(installer._is_ours(handler))

    def test_hooks_installed_tolerates_malformed_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            for hooks in ([], {"Stop": "x"}, {"Stop": ["x", {"hooks": "y"}, {"hooks": ["z"]}]}):
                path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
                self.assertFalse(installer.hooks_installed(Path(tmp)), hooks)
            path.write_text(json.dumps({"hooks": {"Stop": ["x", {"hooks": [HANDLER]}]}}), encoding="utf-8")
            self.assertTrue(installer.hooks_installed(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
