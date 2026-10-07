import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import session_manager


def response(role, text):
    return {"type": "response_item", "payload": {
        "type": "message", "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text",
                     "text": text}],
    }}


def event(kind, text):
    return {"type": "event_msg", "payload": {"type": kind, "message": text}}


class CodexSessionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        sessions_dir = root / "sessions"
        sessions_dir.mkdir()
        index = root / "session_index.jsonl"
        index.write_text("")
        for name, value in (("CODEX_SESSIONS", sessions_dir), ("CODEX_INDEX", index)):
            patcher = patch.object(session_manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def scan(self, entries):
        path = session_manager.CODEX_SESSIONS / "rollout-test.jsonl"
        path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        sessions = session_manager.scan_codex_sessions()
        with patch.object(session_manager, "_sessions_cache", sessions):
            detail = session_manager.load_messages(sessions[0]["id"])
        return sessions[0], detail["messages"]

    def test_response_items_populate_summary_count_and_messages(self):
        session, messages = self.scan([
            {"type": "session_meta", "payload": {
                "id": "session-1", "cwd": "/work", "timestamp": "2026-10-07T00:00:00Z"}},
            response("developer", "internal"),
            response("user", "First question"),
            {"type": "response_item", "payload": {"type": "reasoning"}},
            response("assistant", "Answer"),
            response("user", "Follow-up"),
        ])
        self.assertEqual(session["summary"], "First question")
        self.assertEqual(session["message_count"], 2)
        self.assertEqual(messages, [
            {"role": "user", "text": "First question"},
            {"role": "assistant", "text": "Answer"},
            {"role": "user", "text": "Follow-up"},
        ])

    def test_mixed_formats_do_not_duplicate_messages(self):
        session, messages = self.scan([
            {"type": "session_meta", "payload": {"id": "session-2"}},
            event("user_message", "Hello"),
            event("agent_message", "Hi"),
            response("user", "Hello"),
            response("assistant", "Hi"),
        ])
        self.assertEqual(session["message_count"], 1)
        self.assertEqual([message["text"] for message in messages], ["Hello", "Hi"])

    def test_event_only_session_remains_readable(self):
        session, messages = self.scan([
            {"type": "session_meta", "payload": {"id": "session-3"}},
            event("user_message", "Old"),
            event("agent_message", "Format"),
        ])
        self.assertEqual(session["summary"], "Old")
        self.assertEqual(session["message_count"], 1)
        self.assertEqual([message["text"] for message in messages], ["Old", "Format"])


if __name__ == "__main__":
    unittest.main()
