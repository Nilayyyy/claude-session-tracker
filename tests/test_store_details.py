"""Live details read from transcripts: activity, context size, subagents, last reply, interrupts."""

import json
import unittest

from src import store

NOW = 1_800_000_000.0


def jsonl(*entries: dict) -> bytes:
    # Claude Code writes compact JSON, which the parser's cheap pre-filter relies on.
    return "\n".join(json.dumps(entry, separators=(",", ":")) for entry in entries).encode()


def assistant(*blocks: dict, usage: dict | None = None, sidechain: bool = False) -> dict:
    return {"type": "assistant", "isSidechain": sidechain,
            "message": {"role": "assistant", "content": list(blocks), "usage": usage or {}}}


def user(content) -> dict:
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": content}}


def tool_use(tool_id: str, name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def tool_result(tool_id: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}


class TranscriptTailTest(unittest.TestCase):
    def test_open_tool_is_the_activity_and_closed_tools_are_not(self):
        info = store.parse_transcript_tail(jsonl(
            assistant(tool_use("a", "Read", file_path=r"C:\w\src\store.py")),
            user([tool_result("a")]),
            assistant({"type": "text", "text": "Now  editing."}, tool_use("b", "Edit", file_path="/w/src/overlay.py"),
                      usage={"input_tokens": 2, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 77000}),
        ))
        self.assertEqual(info["activity"], "Editing overlay.py")
        self.assertEqual(info["context_tokens"], 78002)
        self.assertEqual(info["last_text"], "Now editing.")
        self.assertFalse(info["interrupted"])

    def test_no_open_tool_means_no_activity(self):
        info = store.parse_transcript_tail(jsonl(assistant(tool_use("a", "Bash", command="ls")), user([tool_result("a")])))
        self.assertNotIn("activity", info)

    def test_subagents_count_open_agent_calls_and_ignore_sidechains(self):
        info = store.parse_transcript_tail(jsonl(
            assistant(tool_use("a", "Agent", description="Find callers"), tool_use("b", "Task", description="Review")),
            assistant(tool_use("c", "Bash", command="rm -rf /"), sidechain=True),
        ))
        self.assertEqual(info["subagents"], 2)
        self.assertEqual(info["activity"], "Agent: Review")

    def test_interrupt_ends_the_turn_until_the_next_prompt(self):
        interrupted = store.parse_transcript_tail(jsonl(
            assistant(tool_use("a", "Bash", command="sleep 100")),
            user([{"type": "text", "text": "[Request interrupted by user for tool use]"}]),
        ))
        self.assertTrue(interrupted["interrupted"])
        self.assertNotIn("activity", interrupted)
        resumed = store.parse_transcript_tail(jsonl(
            user([{"type": "text", "text": "[Request interrupted by user]"}]),
            user("try again"),
        ))
        self.assertFalse(resumed["interrupted"])

    def test_describe_tool(self):
        self.assertEqual(store.describe_tool("Bash", {"command": "pytest -q", "description": "Run   tests"}), "Run tests")
        self.assertEqual(store.describe_tool("Grep", {"pattern": "TODO"}), "Searching TODO")
        self.assertEqual(store.describe_tool("WebFetch", {"url": "https://docs.python.org/3/x"}), "Reading docs.python.org")
        self.assertEqual(store.describe_tool("mcp__github__create_issue", {}), "Using create issue")
        self.assertEqual(store.describe_tool("Write", None), "Editing files")


class DetailsTest(unittest.TestCase):
    def test_interrupted_running_session_shows_as_done(self):
        session = {"status": "running", "updated_at": NOW, "interrupted": True}
        self.assertEqual(store.effective_status(session, 15, NOW), "answered")
        self.assertTrue(store.status_line(session, "answered", NOW + 120).startswith("Stopped · 2 min ago"))

    def test_running_row_shows_activity_and_card_details(self):
        session = {"id": "s", "status": "running", "updated_at": NOW, "status_since": NOW - 360, "source": "VS Code",
                   "activity": "Editing store.py", "context_tokens": 78_000, "subagents": 1, "last_text": "old reply"}
        [row] = store.rows_from_sessions([session], 40, 15, NOW)
        self.assertEqual(row.activity, "Editing store.py")
        lines = row.tooltip.split("\n")
        self.assertEqual(lines[1], "Running · 6 min · VS Code")
        self.assertEqual(lines[2], "Editing store.py")
        self.assertEqual(lines[3], "78k context · 1 agent working")
        self.assertNotIn("“old reply”", lines)  # The reply preview is for finished/waiting sessions.

    def test_waiting_session_card(self):
        session = {"id": "s", "status": "attention", "updated_at": NOW - 240, "status_since": NOW - 240,
                   "attention_reason": "Bash permission", "last_text": "x" * 300}
        [row] = store.rows_from_sessions([session], 40, 15, NOW)
        lines = row.tooltip.split("\n")
        self.assertEqual(row.activity, "")
        self.assertEqual(lines[1], "Waiting on you · 4 min")
        self.assertEqual(lines[2], "Waiting on: Bash permission")
        self.assertEqual(len(lines[3]), store.PREVIEW_CHARS + 2)  # Quoted and trimmed.

    def test_format_helpers(self):
        self.assertEqual(store.format_duration(30), "under a minute")
        self.assertEqual(store.format_duration(3900), "1 h 5 min")
        self.assertEqual(store.format_tokens(1_250_000), "1.2M")


if __name__ == "__main__":
    unittest.main()
