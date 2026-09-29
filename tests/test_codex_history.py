from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
import shutil

from app.codex.history import CodexHistoryService
from app.projects.paths import normalize_path


class CodexHistoryTests(unittest.TestCase):
    def test_discovers_and_deduplicates_existing_session_projects(self):
        tmp = Path(tempfile.mkdtemp(prefix="remote-codex-discovery-"))
        sessions = tmp / "sessions" / "2026" / "09"
        sessions.mkdir(parents=True)
        project_dir = tmp / "project"
        project_dir.mkdir()
        other_dir = tmp / "other"
        other_dir.mkdir()
        records = [
            {"type": "session_meta", "timestamp": "2026-09-01T00:00:00Z", "payload": {"cwd": str(project_dir)}},
            {"type": "session_meta", "timestamp": "2026-09-02T00:00:00Z", "payload": {"cwd": str(project_dir)}},
            {"type": "session_meta", "timestamp": "2026-09-03T00:00:00Z", "payload": {"cwd": str(other_dir)}},
        ]
        for index, record in enumerate(records):
            (sessions / f"session-{index}.jsonl").write_text(
                json.dumps(record) + "\n", encoding="utf-8"
            )

        original_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(tmp)
        try:
            result = CodexHistoryService().discover_projects()
        finally:
            if original_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = original_home
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(result["scanned_sessions"], 3)
        by_path = {item["path"]: item for item in result["projects"]}
        self.assertEqual(len(by_path), 2)
        self.assertEqual(by_path[str(normalize_path(project_dir))]["session_count"], 2)
        self.assertEqual(by_path[str(normalize_path(other_dir))]["session_count"], 1)

    def test_reads_matching_project_rollout(self):
        tmp = Path(tempfile.mkdtemp(prefix="remote-codex-history-"))
        sessions = tmp / "sessions" / "2026" / "09" / "25"
        sessions.mkdir(parents=True)
        project_dir = tmp / "project"
        project_dir.mkdir()
        records = [
            {"type": "session_meta", "payload": {"cwd": str(project_dir)}},
            {"type": "response_item", "timestamp": "2026-09-25T01:02:03Z", "payload": {
                "type": "message", "role": "user", "id": "u1",
                "content": [{"type": "input_text", "text": "hello codex"}],
            }},
            {"type": "response_item", "timestamp": "2026-09-25T01:02:04Z", "payload": {
                "type": "message", "role": "assistant", "id": "a1",
                "content": [{"type": "output_text", "text": "ready"}],
            }},
            {"type": "response_item", "timestamp": "2026-09-25T01:02:05Z", "payload": {
                "type": "message", "role": "user", "id": "u2",
                "content": [{"type": "input_text", "text": "<environment_context>hidden</environment_context>"}],
            }},
        ]
        rollout = sessions / "rollout.jsonl"
        rollout.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

        original_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(tmp)
        try:
            result = CodexHistoryService().list_messages({
                "id": "prj_test", "normalized_path": str(project_dir), "limit": 10,
            })
        finally:
            if original_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = original_home
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(result["matched_sessions"], 1)
        self.assertEqual(result["scanned_sessions"], 1)
        self.assertEqual(
            [(item["role"], item["text"]) for item in result["messages"]],
            [("user", "hello codex"), ("assistant", "ready")],
        )
        self.assertEqual(
            [item["body"] for item in result["messages"]],
            ["hello codex", "ready"],
        )

    def test_filters_history_by_selected_thread(self):
        tmp = Path(tempfile.mkdtemp(prefix="remote-codex-thread-history-"))
        sessions = tmp / "sessions" / "2026" / "09" / "25"
        sessions.mkdir(parents=True)
        project_dir = tmp / "project"
        project_dir.mkdir()
        rollouts = [
            ("b78a2d8c-1111-4222-8333-111111111111", "from thread one"),
            ("b78a2d8c-2222-4222-8333-222222222222", "from thread two"),
        ]
        for thread_id, text in rollouts:
            records = [
                {"type": "session_meta", "payload": {"cwd": str(project_dir), "id": thread_id}},
                {"type": "response_item", "timestamp": "2026-09-25T01:02:03Z", "payload": {
                    "type": "message", "role": "assistant", "id": thread_id,
                    "content": [{"type": "output_text", "text": text}],
                }},
            ]
            (sessions / f"rollout-2026-09-25T01-00-00-{thread_id}.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

        original_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(tmp)
        try:
            result = CodexHistoryService().list_messages({
                "id": "prj_test",
                "normalized_path": str(project_dir),
                "codex_thread_id": rollouts[1][0],
            }, thread_id=rollouts[1][0])
        finally:
            if original_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = original_home
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(result["thread_id"], rollouts[1][0])
        self.assertEqual([item["text"] for item in result["messages"]], ["from thread two"])

    def test_thread_filter_rejects_non_matching_rollout_names(self):
        service = CodexHistoryService()

        self.assertEqual(service._thread_id(Path("rollout-2026-09-25T01-00-00-not-a-thread.jsonl")), "")

    def test_reads_legacy_agent_message_body_fields(self):
        service = CodexHistoryService()

        self.assertEqual(
            service._parse_message({
                "type": "agent_message",
                "body": "legacy reply",
            }),
            {
                "role": "assistant",
                "text": "legacy reply",
                "body": "legacy reply",
                "message_id": "",
            },
        )
        self.assertEqual(
            service._parse_message({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "body": "nested reply"}],
            })["text"],
            "nested reply",
        )

    def test_reads_event_messages_without_duplicate_response_items(self):
        tmp = Path(tempfile.mkdtemp(prefix="remote-codex-event-history-"))
        sessions = tmp / "sessions" / "2026" / "09" / "25"
        sessions.mkdir(parents=True)
        project_dir = tmp / "project"
        project_dir.mkdir()
        records = [
            {"type": "session_meta", "payload": {"cwd": str(project_dir)}},
            {"type": "response_item", "timestamp": "2026-09-25T01:02:03.100Z", "payload": {
                "type": "message", "role": "user", "id": "response-user",
                "content": [{"type": "input_text", "text": "hello"}],
            }},
            {"type": "event_msg", "timestamp": "2026-09-25T01:02:03.200Z", "payload": {
                "type": "item_completed", "item": {
                    "type": "UserMessage", "id": "event-user",
                    "content": [{"type": "Text", "text": "hello"}],
                },
            }},
            {"type": "event_msg", "timestamp": "2026-09-25T01:02:04.100Z", "payload": {
                "type": "item_completed", "item": {
                    "type": "AgentMessage", "id": "event-assistant",
                    "content": [{"type": "Text", "text": "event-only reply"}],
                },
            }},
            {"type": "response_item", "timestamp": "2026-09-25T01:02:05.100Z", "payload": {
                "type": "message", "role": "assistant", "id": "response-assistant",
                "content": [{"type": "output_text", "text": "ready"}],
            }},
            {"type": "event_msg", "timestamp": "2026-09-25T01:02:05.200Z", "payload": {
                "type": "item_completed", "item": {
                    "type": "AgentMessage", "id": "response-assistant",
                    "content": [{"type": "Text", "text": "ready"}],
                },
            }},
        ]
        rollout = sessions / "rollout.jsonl"
        rollout.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

        original_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(tmp)
        try:
            result = CodexHistoryService().list_messages({
                "id": "prj_test", "normalized_path": str(project_dir), "limit": 10,
            })
        finally:
            if original_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = original_home
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(
            [(item["role"], item["text"]) for item in result["messages"]],
            [
                ("user", "hello"),
                ("assistant", "event-only reply"),
                ("assistant", "ready"),
            ],
        )
