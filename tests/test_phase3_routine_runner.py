from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.models import AgentCreate, RoutineCreate
from project_q.services.scheduler import RoutineSchedulerService


class RoutineRunnerPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.app = create_application(
            AppConfig(
                project_name="Project Q Test",
                workspace_root=root,
                data_root=root / ".project_q",
                db_path=root / ".project_q" / "project_q.db",
            )
        )
        self.routine = self.app.routines.create(
            RoutineCreate(name="Persistence test", goal="Return the exact inserted run")
        )

    def tearDown(self) -> None:
        self.app.shutdown_services()
        self.temp_dir.cleanup()

    def test_store_run_returns_the_inserted_row_without_latest_run_lookup(self) -> None:
        def fail_if_called(*_args, **_kwargs):
            raise AssertionError("run insertion must not use a racy latest-run lookup")

        self.app.routine_runner.list_runs = fail_if_called

        record = self.app.routine_runner._store_run(
            routine_id=self.routine["id"],
            goal=self.routine["goal"],
            reply="completed",
            outcome="completed",
            step_results=[],
            blocked_steps=[],
            warning="",
        )

        self.assertEqual(record["routine_id"], self.routine["id"])
        self.assertEqual(record["reply"], "completed")
        self.assertEqual(record["outcome"], "completed")

    def test_trusted_routine_does_not_convert_agent_step_to_owner_approval(self) -> None:
        captured: dict[str, object] = {}

        def fake_agent_run(agent_id: str, *, owner_approved: bool):
            captured["agent_id"] = agent_id
            captured["owner_approved"] = owner_approved
            return {"id": "agentrun_test", "outcome": "completed", "reply": "done"}

        self.app.agent_runner.run = fake_agent_run
        agent = self.app.agents.create(
            AgentCreate(
                name="Trusted routine agent",
                agent_type="research",
                goal="Attempt protected work only if explicitly approved.",
                status="active",
            )
        )
        routine = self.app.routines.create(
            RoutineCreate(
                name="Trusted agent wrapper",
                goal="Run the agent without owner approval escalation.",
                trusted=True,
                steps=[
                    {
                        "step_type": "agent",
                        "label": "Agent step",
                        "agent_id": agent["id"],
                    }
                ],
            )
        )

        run = self.app.routine_runner.run(routine["id"], owner_approved=False)

        self.assertEqual(run["outcome"], "completed")
        self.assertEqual(captured["agent_id"], agent["id"])
        self.assertIs(captured["owner_approved"], False)


class RoutineSchedulerTrustTests(unittest.TestCase):
    def test_scheduler_runs_due_routines_without_synthesizing_owner_approval(self) -> None:
        class FakeRoutineService:
            def list_all(self, limit: int = 500):
                return [
                    {
                        "id": "routine_scheduled",
                        "name": "Scheduled",
                        "trigger_type": "schedule",
                        "status": "active",
                        "metadata": {"schedule_interval_minutes": 5},
                        "last_run_at": None,
                    }
                ]

        class FakeRoutineRunner:
            def __init__(self) -> None:
                self.calls: list[tuple[str, bool]] = []

            def run(self, routine_id: str, *, owner_approved: bool):
                self.calls.append((routine_id, owner_approved))

        runner = FakeRoutineRunner()
        scheduler = RoutineSchedulerService(FakeRoutineService(), runner)

        scheduler._tick()

        self.assertEqual(runner.calls, [("routine_scheduled", False)])


if __name__ == "__main__":
    unittest.main()
