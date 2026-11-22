import json
import os
import tempfile
import unittest
from pathlib import Path

from src import cowork, store

NOW = 1_800_000_000.0


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prunes_dead_and_old_keeps_unknown(self):
        write(self.dir / "alive.json", {"id": "a", "status": "running", "pid": 1, "updated_at": NOW})
        write(self.dir / "dead.json", {"id": "d", "status": "answered", "pid": 2, "updated_at": NOW})
        write(self.dir / "old.json", {"id": "o", "status": "answered", "pid": 1, "updated_at": NOW - 90000})
        write(self.dir / "unknown.json", {"id": "u", "status": "answered", "pid": 0, "updated_at": NOW})
        (self.dir / "corrupt.json").write_text("{half", encoding="utf-8")
        alive = {1: True, 2: False}
        sessions = store.load_hook_sessions(self.dir, 24, lambda pid, image: alive.get(pid), now=NOW)
        self.assertEqual(sorted(s["id"] for s in sessions), ["a", "u"])
        self.assertFalse((self.dir / "dead.json").exists())
        self.assertFalse((self.dir / "old.json").exists())
        self.assertTrue((self.dir / "corrupt.json").exists())

    def test_rows_sorted_coloured_and_named(self):
        sessions = [
            {"id": "1", "status": "answered", "cwd": "/w/alpha", "first_prompt": "do it", "updated_at": NOW - 600},
            {"id": "2", "status": "attention", "cwd": "/w/beta", "title": "Fix login", "updated_at": NOW - 10,
             "attention_reason": "Bash", "source": "VS Code"},
            {"id": "3", "status": "running", "cwd": "/w/gamma", "updated_at": NOW - 3600},  # stale
            {"id": "4", "status": "running", "cwd": "/w/delta", "updated_at": NOW - 5},
            {"id": "5", "agent": "cowork", "status": "running", "title": "Deck", "updated_at": NOW - 5000,
             "best_effort": True, "source": "Cowork"},
        ]
        rows = store.rows_from_sessions(sessions, 30, stale_minutes=15, now=NOW)
        self.assertEqual([r.id for r in rows], ["2", "4", "5", "3", "1"])
        self.assertEqual([r.status for r in rows], ["attention", "running", "running", "stale", "answered"])
        self.assertEqual(rows[0].name, "Fix login")
        self.assertIn("Waiting on: Bash", rows[0].tooltip)
        self.assertIn("/w/beta", rows[0].tooltip)
        self.assertIn("best-effort", rows[2].tooltip)
        self.assertEqual(rows[4].name, "do it")
        self.assertEqual(rows[1].name, "delta")  # No title yet: folder name.
        self.assertEqual(rows[1].age, "now")

    def test_transcript_activity_prevents_stale(self):
        transcript = self.dir / "t.jsonl"
        transcript.write_text("{}")
        os.utime(transcript, (NOW - 30, NOW - 30))
        session = {"status": "running", "updated_at": NOW - 3600, "transcript_path": str(transcript)}
        self.assertEqual(store.effective_status(session, 15, NOW), "running")

    def test_title_priority(self):
        base = {"cwd": "/w/repo", "first_prompt": "prompt"}
        self.assertEqual(store.display_name({**base, "ai_title": "Auto"}, 40), "Auto")
        self.assertEqual(store.display_name({**base, "ai_title": "Auto", "custom_title": "Mine"}, 40), "Mine")
        self.assertEqual(store.display_name(base, 40), "prompt")

    def test_transcript_titles_latest_wins_and_tail_only(self):
        store._info_cache.clear()
        transcript = self.dir / "t.jsonl"
        lines = [
            {"type": "ai-title", "aiTitle": "Old auto"},
            {"type": "user", "message": "x" * 300},
            {"type": "custom-title", "customTitle": "Renamed"},
            {"type": "ai-title", "aiTitle": "New auto"},
            "not json {",
        ]
        transcript.write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines), encoding="utf-8")
        self.assertEqual(store.transcript_titles(str(transcript)), {"custom": "Renamed", "ai": "New auto"})
        # Tail too small to hold any title line: the previously found titles are kept.
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write("\n" + json.dumps({"type": "user", "message": "y" * 500}))
        self.assertEqual(store.transcript_titles(str(transcript), tail_bytes=200), {"custom": "Renamed", "ai": "New auto"})
        sessions = store.add_transcript_info([{"transcript_path": str(transcript)}, {"agent": "cowork", "title": "C"}])
        self.assertEqual(sessions[0]["custom_title"], "Renamed")
        self.assertNotIn("ai_title", sessions[1])
        self.assertEqual(store.transcript_titles(""), {})

    def test_name_truncation_and_fallbacks(self):
        self.assertEqual(store.display_name({"first_prompt": "y" * 80}, 20), "y" * 19 + "…")
        self.assertEqual(store.display_name({"session_id": "abcdef123456"}, 20), "abcdef12")

    def test_clear_finished(self):
        write(self.dir / "a.json", {"status": "answered"})
        write(self.dir / "b.json", {"status": "running"})
        write(self.dir / "c.json", {"status": "error"})
        self.assertEqual(store.clear_finished(self.dir), 2)
        self.assertEqual([p.name for p in self.dir.glob("*.json")], ["b.json"])

    def test_format_age(self):
        self.assertEqual([store.format_age(s) for s in (5, 125, 7300)], ["now", "2m", "2h"])


class CoworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "local-agent-mode-sessions"

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name: str, data: dict, mtime: float) -> Path:
        path = self.dir / "acct" / "org" / name
        write(path, data)
        os.utime(path, (mtime, mtime))
        return path

    def test_status_from_activity(self):
        self.make("local_a.json", {"sessionId": "a", "title": "Report", "lastActivityAt": (NOW - 10) * 1000}, NOW - 10)
        self.make("local_b.json", {"title": "Idle task", "lastActivityAt": "2027-01-15T07:50:00Z"}, NOW - 600)
        self.make("local_c.json", {"title": "Archived", "isArchived": True}, NOW)
        self.make("local_d.json", {"title": "Ancient"}, NOW - 99999)
        (self.dir / "acct" / "org" / "local_e.json").write_text("garbage", encoding="utf-8")
        sessions = {s["title"]: s for s in cowork.load_cowork_sessions([self.dir], 45, 3, now=NOW)}
        self.assertEqual(set(sessions), {"Report", "Idle task"})
        self.assertEqual(sessions["Report"]["status"], "running")
        self.assertEqual(sessions["Report"]["id"], "cowork:a")
        self.assertEqual(sessions["Idle task"]["status"], "answered")
        self.assertTrue(all(s["best_effort"] for s in sessions.values()))

    def test_missing_dir_and_diagnostics(self):
        self.assertEqual(cowork.load_cowork_sessions([self.dir / "nope"], 45, 3, now=NOW), [])
        self.make("local_a.json", {"title": "x", "secret": "value"}, NOW)
        lines = cowork.describe_cowork_files([self.dir])
        self.assertIn("1 session file(s)", lines[0])
        self.assertNotIn("value", "\n".join(lines))

    def test_cowork_file_deleted_mid_scan_is_skipped(self):
        self.assertIsNone(cowork.parse_session_file(self.dir / "local_gone.json", NOW, 45, 3))

    def test_transcript_cache_forgets_untracked_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text('{"type":"ai-title","aiTitle":"Hi"}\n', encoding="utf-8")
            store._info_cache["stale-path"] = ((0, 0), {})
            sessions = store.add_transcript_info([{"agent": "claude", "transcript_path": str(transcript)}])
            self.assertEqual(sessions[0]["ai_title"], "Hi")
            self.assertNotIn("stale-path", store._info_cache)
            self.assertIn(str(transcript), store._info_cache)
            store._info_cache.clear()


if __name__ == "__main__":
    unittest.main()
