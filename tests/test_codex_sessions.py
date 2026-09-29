from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.codex.sessions import CodexSessionService


class CodexSessionTests(unittest.TestCase):
    def test_session_list_uses_session_label_not_chat_message(self):
        tmp = Path(tempfile.mkdtemp(prefix="remote-codex-sessions-"))
        sessions = tmp / "sessions" / "2026" / "09" / "25"
        sessions.mkdir(parents=True)
        project_dir = tmp / "project"
        project_dir.mkdir()
        thread_id = "b78a2d8c-1111-4222-8333-111111111111"
        records = [
            {
                "type": "session_meta",
                "timestamp": "2026-09-25T01:02:03Z",
                "payload": {"id": thread_id, "cwd": str(project_dir)},
            },
            {
                "type": "response_item",
                "timestamp": "2026-09-25T01:02:04Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": "<environment_context><cwd>D:/work</cwd></environment_context>",
                    }],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-09-25T01:02:05Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "修一下登录页"}],
                },
            },
        ]
        (sessions / f"rollout-2026-09-25T01-00-00-{thread_id}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

        original_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(tmp)
        try:
            sessions_result = CodexSessionService().sessions_for_project(project_dir)
        finally:
            if original_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = original_home
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(len(sessions_result), 1)
        self.assertEqual(sessions_result[0].thread_id, thread_id)
        self.assertEqual(sessions_result[0].title, "会话 · 01:02")
