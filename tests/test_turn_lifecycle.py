import asyncio
from contextlib import suppress
import tempfile
import unittest
from pathlib import Path
import shutil

from app.codex.gateway import FakeCodexGateway, SandboxMode, ThreadHandle
from app.codex.turns import TurnBusyError
from app.codex.titles import SessionTitleService
from app.codex.turns import TurnSupervisor
from app.events.fanout import EventFanout
from app.events.store import EventStore
from app.projects.manager import ProjectService
from app.storage.database import Database


class TurnLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-codex-turn-"))
        self.db = Database(self.tmp / "turns.db3")
        await self.db.connect()
        self.events = EventStore(self.db)
        self.fanout = EventFanout()
        self.projects = ProjectService(self.db, [self.tmp])
        self.project = await self.projects.create("demo", str(self.tmp))
        self.titles = SessionTitleService(self.db, FakeCodexGateway())
        self.supervisor = TurnSupervisor(
            self.db, FakeCodexGateway(), self.events, self.fanout,
            self.projects, self.titles,
        )

    async def _wait_turn(self, turn_id: str) -> None:
        running = next(
            item for item in self.supervisor._running.values() if item.id == turn_id
        )
        await running.task

    async def asyncTearDown(self):
        await self.supervisor.shutdown()
        await self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_turn_runs_stores_events_and_recovers_state(self):
        turn_id = await self.supervisor.start(
            self.project, "demo", SandboxMode.READ_ONLY
        )
        project_id = self.project["id"]
        await self._wait_turn(turn_id)
        turn = await self.db.fetch_one(
            "SELECT * FROM turns WHERE id = ?", (turn_id,)
        )
        self.assertEqual(turn["status"], "completed")
        current = await self.projects.get(project_id)
        self.assertIsNotNone(current.get("current_session_id"))
        session = await self.db.fetch_one("SELECT title FROM codex_sessions")
        self.assertEqual(session["title"], "demo")
        records = await self.events.replay(project_id)
        self.assertEqual(records[0].type, "turn.started")
        self.assertEqual(records[-1].type, "turn.status")

    async def test_interrupt_sets_turn_status(self):
        turn_id = await self.supervisor.start(
            self.project, "demo", SandboxMode.READ_ONLY
        )
        project_id = self.project["id"]
        await asyncio.sleep(0.01)
        self.assertEqual(
            await self.supervisor.interrupt(project_id, turn_id), turn_id
        )
        await self._wait_turn(turn_id)
        turn = await self.db.fetch_one(
            "SELECT * FROM turns WHERE id = ?", (turn_id,)
        )
        self.assertEqual(turn["status"], "interrupted")

    async def test_same_project_sessions_run_independently(self):
        project_id = self.project["id"]
        first_thread = "thr_session_a"
        second_thread = "thr_session_b"
        first_session = await self.projects.create_session_from_prompt(
            project_id, "first", thread_id=first_thread
        )
        second_session = await self.projects.create_session_from_prompt(
            project_id, "second", thread_id=second_thread
        )

        first_turn = await self.supervisor.start(
            self.project, "first prompt", SandboxMode.READ_ONLY,
            session_id=first_session,
        )
        second_turn = await self.supervisor.start(
            self.project, "second prompt", SandboxMode.READ_ONLY,
            session_id=second_session,
        )

        self.assertEqual(len(self.supervisor._running), 2)
        self.assertEqual(
            {first_thread, second_thread},
            set(self.supervisor.gateway.resumed),
        )
        with suppress(TurnBusyError):
            await self.supervisor.start(
                self.project, "duplicate", SandboxMode.READ_ONLY,
                session_id=second_session,
            )
            self.fail("same session turn should be busy")

        await asyncio.gather(
            self._wait_turn(first_turn),
            self._wait_turn(second_turn),
        )

    async def test_running_turns_snapshot_is_scoped_to_session(self):
        project_id = self.project["id"]
        first_session = await self.projects.create_session_from_prompt(
            project_id, "first", thread_id="thr_running_first"
        )
        second_session = await self.projects.create_session_from_prompt(
            project_id, "second", thread_id="thr_running_second"
        )

        first_turn = await self.supervisor.start(
            self.project, "first prompt", SandboxMode.READ_ONLY,
            session_id=first_session,
        )
        second_turn = await self.supervisor.start(
            self.project, "second prompt", SandboxMode.READ_ONLY,
            session_id=second_session,
        )

        snapshot = {
            (item["turn_id"], item["session_id"])
            for item in self.supervisor.running_turns()
        }
        self.assertEqual(
            snapshot,
            {
                (first_turn, first_session),
                (second_turn, second_session),
            },
        )

        await asyncio.gather(
            self._wait_turn(first_turn),
            self._wait_turn(second_turn),
        )
        self.assertEqual(self.supervisor.running_turns(), [])

    async def test_new_session_does_not_reuse_previous_thread(self):
        project_id = self.project["id"]
        first_session = await self.projects.create_session_from_prompt(
            project_id, "first", thread_id="thr_previous"
        )
        first_turn = await self.supervisor.start(
            self.project,
            "first prompt",
            SandboxMode.READ_ONLY,
            session_id=first_session,
        )
        await self._wait_turn(first_turn)
        resumed_before = len(self.supervisor.gateway.resumed)
        self.project = await self.projects.new_thread(project_id)

        second_turn = await self.supervisor.start(
            self.project,
            "second prompt",
            SandboxMode.READ_ONLY,
        )
        self.assertNotIn(
            "thr_previous",
            self.supervisor.gateway.resumed[resumed_before:],
        )
        second = await self.projects.get(project_id)
        second_session = second["current_session_id"]
        self.assertIsNotNone(second_session)
        self.assertNotEqual(second_session, first_session)

        await self._wait_turn(second_turn)
        session = await self.projects.get_session(project_id, second_session)
        self.assertNotEqual(session["codex_thread_id"], "thr_previous")

    async def test_parallel_turns_do_not_repoint_current_session(self):
        project_id = self.project["id"]
        selected_session = await self.projects.create_session_from_prompt(
            project_id, "selected", thread_id="thr_selected"
        )
        background_session = await self.projects.create_session_from_prompt(
            project_id, "background", thread_id="thr_background"
        )
        await self.projects.select_session(project_id, selected_session)
        foreground_project = await self.projects.get(project_id)
        background_project = {
            **foreground_project,
            "current_session_id": background_session,
            "codex_thread_id": "thr_background",
        }

        background_turn = await self.supervisor.start(
            background_project,
            "background prompt",
            SandboxMode.READ_ONLY,
            session_id=background_session,
        )
        foreground_turn = await self.supervisor.start(
            foreground_project,
            "selected prompt",
            SandboxMode.READ_ONLY,
            session_id=selected_session,
        )
        await self._wait_turn(background_turn)

        current = await self.projects.get(project_id)
        self.assertEqual(current["current_session_id"], selected_session)
        self.assertEqual(current["codex_thread_id"], "thr_selected")

        await self._wait_turn(foreground_turn)

    async def test_background_new_turn_keeps_selected_session(self):
        project_id = self.project["id"]
        selected_session = await self.projects.create_session_from_prompt(
            project_id, "selected", thread_id="thr_selected"
        )
        await self.projects.select_session(project_id, selected_session)
        foreground_project = await self.projects.get(project_id)
        background_project = {
            **foreground_project,
            "current_session_id": None,
            "codex_thread_id": None,
        }

        background_turn = await self.supervisor.start(
            background_project,
            "background new prompt",
            SandboxMode.READ_ONLY,
        )

        current = await self.projects.get(project_id)
        self.assertEqual(current["current_session_id"], selected_session)
        await self._wait_turn(background_turn)
        current = await self.projects.get(project_id)
        self.assertEqual(current["current_session_id"], selected_session)
        self.assertEqual(current["codex_thread_id"], "thr_selected")

    async def test_hung_turn_times_out_and_releases_global_slot(self):
        class HungGateway:
            def __init__(self):
                self.interrupted = []

            async def start_thread(self, cwd, sandbox):
                return ThreadHandle(
                    thread_id=f"thr_hung_{len(self.interrupted) + 1}",
                    cwd=cwd,
                )

            async def resume_thread(self, thread_id, cwd):
                return ThreadHandle(thread_id=thread_id, cwd=cwd)

            async def run_turn(self, handle, prompt, **kwargs):
                yield {"type": "turn.started", "thread_id": handle.thread_id}
                await asyncio.Event().wait()

            async def interrupt(self, handle):
                self.interrupted.append(handle.thread_id)

            async def close(self, handle):
                pass

        supervisor = TurnSupervisor(
            self.db,
            HungGateway(),
            self.events,
            self.fanout,
            self.projects,
            self.titles,
            max_running=1,
            timeout_sec=0.02,
        )
        turn_id = await supervisor.start(
            self.project, "hung prompt", SandboxMode.READ_ONLY
        )
        registered = list(supervisor._running.values())

        for running in registered:
            await asyncio.wait_for(running.task, timeout=2)

        for running in registered:
            await running.task
        self.assertEqual(supervisor._running, {})
        self.assertTrue(supervisor.gateway.interrupted)
        turn = await self.db.fetch_one("SELECT * FROM turns WHERE id = ?", (turn_id,))
        self.assertEqual(turn["status"], "failed")
        self.assertEqual(turn["error"], "turn timed out")

        records = await self.events.replay(self.project["id"])
        self.assertIn(
            "turn.failed",
            [record.type for record in records if record.kind == "codex.event"],
        )

        retry_turn_id = await supervisor.start(
            self.project, "retry prompt", SandboxMode.READ_ONLY
        )
        self.assertNotEqual(retry_turn_id, turn_id)
        await supervisor.shutdown()
        await asyncio.sleep(0.01)
