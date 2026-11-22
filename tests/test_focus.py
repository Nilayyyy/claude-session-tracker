import unittest

from src import focus

# pid -> (parent pid, exe): claude.exe under the VS Code extension host, and a CLI under a terminal.
PARENTS = {
    100: (200, "claude.exe"), 200: (300, "code.exe"), 300: (900, "code.exe"), 900: (1, "explorer.exe"),
    110: (120, "claude.exe"), 120: (130, "pwsh.exe"), 130: (900, "windowsterminal.exe"),
    140: (150, "claude.exe"), 150: (900, "pwsh.exe"),  # Console host is not an ancestor.
}
WINDOWS = [
    (1, 300, "app.py - qcomm-ai-agents - Visual Studio Code"),
    (2, 300, "Folder review - session-overlay - Visual Studio Code"),
    (3, 130, "Windows PowerShell"),
    (4, 900, "Program Manager"),
    (5, 900, "session-overlay"),  # A File Explorer window named after the folder.
]


class FocusTests(unittest.TestCase):
    def test_ancestors_stop_at_desktop_shell_and_cycles(self):
        self.assertEqual(focus.ancestors(100, PARENTS), [100, 200, 300])
        self.assertEqual(focus.ancestors(140, PARENTS), [140, 150])
        self.assertEqual(focus.ancestors(1, {1: (2, "a.exe"), 2: (1, "b.exe")}), [1, 2])
        self.assertEqual(focus.ancestors(999, PARENTS), [])

    def test_pick_vscode_window_by_folder_including_subfolder(self):
        chain = focus.ancestors(100, PARENTS)
        self.assertEqual(focus.pick_window(WINDOWS, chain, r"C:\Users\x\session-overlay"), 2)
        self.assertEqual(focus.pick_window(WINDOWS, chain, r"C:\GitHub\qcomm-ai-agents\src\api"), 1)
        self.assertIsNone(focus.pick_window(WINDOWS, chain, r"C:\other\repo"))  # Don't guess.

    def test_pick_terminal_window_and_never_the_desktop(self):
        self.assertEqual(focus.pick_window(WINDOWS, focus.ancestors(110, PARENTS), r"C:\x\repo"), 3)
        self.assertIsNone(focus.pick_window(WINDOWS, focus.ancestors(140, PARENTS), r"C:\x\session-overlay"))

    def test_vscode_extension_detection(self):
        self.assertTrue(focus.hosted_by_vscode_extension(focus.ancestors(100, PARENTS), PARENTS))
        # CLI in a terminal (even VS Code's integrated one) has a shell parent: no chat-tab link.
        self.assertFalse(focus.hosted_by_vscode_extension(focus.ancestors(110, PARENTS), PARENTS))

    def test_folder_names(self):
        self.assertEqual(focus.folder_names(r"C:\Users\x\repo"), ["repo", "x", "Users"])
        self.assertEqual(focus.folder_names("/home/x/repo"), ["repo", "x", "home"])


if __name__ == "__main__":
    unittest.main()
