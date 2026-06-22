from __future__ import annotations

import shutil
import sqlite3
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from project_q.models import AgentCreate, AgentUpdate
from project_q.services.agent_runner import AgentRunnerService
from project_q.services.agents import AgentService, BUILT_IN_TEMPLATES
from project_q.storage import Database


class FakeContextService:
    def build(self, instruction: str) -> dict:
        return {"instruction": instruction}


class FakeExecutorService:
    def __init__(self) -> None:
        self.last_limits = {}

    def execute(
        self,
        *,
        plan,
        owner_approved: bool,
        input_sources: list[str],
        max_tool_calls: int | None = None,
        allowed_tool_ids: list[str] | None = None,
    ) -> dict:
        del plan, owner_approved, input_sources
        self.last_limits = {
            "max_tool_calls": max_tool_calls,
            "allowed_tool_ids": allowed_tool_ids,
        }
        return {
            "created_task_ids": [],
            "created_memory_ids": [],
            "created_agent_ids": [],
            "executed_tools": [],
            "blocked_tools": [],
            "usage": {"tool_calls": 0},
        }

    def compose_reply(self, reply, executed_tools, blocked_tools, warning):
        del executed_tools, blocked_tools, warning
        return reply


class FakeAuditService:
    def log(self, **kwargs) -> str:
        del kwargs
        return "audit-1"


class ImmediateReasoner:
    def plan(self, *, user_message: str, context: dict):
        del user_message, context
        return SimpleNamespace(
            plan=SimpleNamespace(reply="completed"),
            mode="local",
            model_name="test-model",
            warning="",
        )


class RacingReasoner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls = 0
        self.first_started = threading.Event()
        self.release_first = threading.Event()

    def plan(self, *, user_message: str, context: dict):
        del user_message, context
        with self._lock:
            self._calls += 1
            call_number = self._calls
        if call_number == 1:
            self.first_started.set()
            if not self.release_first.wait(timeout=10):
                raise TimeoutError("first run was not released")
            return SimpleNamespace(
                plan=SimpleNamespace(reply="first completed"),
                mode="local",
                model_name="test-model",
                warning="",
            )
        raise RuntimeError("second run failed")


class Phase3AgentDefinitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "project_q.db"
        self.db = Database(self.db_path)
        self.agents = AgentService(self.db)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _runner(self, reasoner=None, executor=None) -> AgentRunnerService:
        return AgentRunnerService(
            self.db,
            self.agents,
            FakeContextService(),
            reasoner or ImmediateReasoner(),
            executor or FakeExecutorService(),
            FakeAuditService(),
        )

    def test_custom_agent_updates_create_immutable_definition_versions(self) -> None:
        created = self.agents.create(
            AgentCreate(name="Planner", goal="Plan the first release", status="active")
        )

        self.assertIn("definition_id", created)
        self.assertEqual(created["definition_version"], 1)
        updated = self.agents.update(
            created["id"],
            AgentUpdate(goal="Plan the second release"),
        )

        self.assertEqual(updated["definition_id"], created["definition_id"])
        self.assertEqual(updated["definition_version"], 2)
        with self.db.connection() as conn:
            versions = conn.execute(
                """
                SELECT version, goal
                FROM agent_definition_versions
                WHERE definition_id = ?
                ORDER BY version
                """,
                (created["definition_id"],),
            ).fetchall()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    UPDATE agent_definition_versions
                    SET goal = 'mutated'
                    WHERE definition_id = ? AND version = 1
                    """,
                    (created["definition_id"],),
                )

        self.assertEqual(
            [(row["version"], row["goal"]) for row in versions],
            [(1, "Plan the first release"), (2, "Plan the second release")],
        )

    def test_only_changed_definition_content_creates_a_version(self) -> None:
        created = self.agents.create(
            AgentCreate(name="Budget Agent", goal="Stay bounded", status="active")
        )

        status_only = self.agents.update(
            created["id"],
            AgentUpdate(status="draft"),
        )
        same_goal = self.agents.update(
            created["id"],
            AgentUpdate(goal="Stay bounded"),
        )
        changed_budget = self.agents.update(
            created["id"],
            AgentUpdate(time_budget_minutes=60),
        )

        self.assertEqual(status_only["definition_version"], 1)
        self.assertEqual(same_goal["definition_version"], 1)
        self.assertEqual(changed_budget["definition_version"], 2)
        self.assertEqual(changed_budget["budget"]["time_budget_minutes"], 60)
        with self.db.connection() as conn:
            version_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM agent_definition_versions
                WHERE definition_id = ?
                """,
                (created["definition_id"],),
            ).fetchone()[0]
        self.assertEqual(version_count, 2)

    def test_builtin_templates_are_persisted_and_spawn_pinned_agents(self) -> None:
        templates = self.agents.list_templates()

        self.assertEqual({item["id"] for item in templates}, set(BUILT_IN_TEMPLATES))
        self.assertTrue(all(item.get("definition_id") for item in templates))
        self.assertTrue(all(item.get("definition_version") == 1 for item in templates))

        spawned = self.agents.spawn_from_template("research")
        research = next(item for item in templates if item["id"] == "research")
        self.assertEqual(spawned["definition_id"], research["definition_id"])
        self.assertEqual(spawned["definition_version"], 1)

        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT source, definition_key, current_version
                FROM agent_definitions
                WHERE id = ?
                """,
                (research["definition_id"],),
            ).fetchone()
        self.assertEqual(dict(row), {
            "source": "builtin",
            "definition_key": "research",
            "current_version": 1,
        })

    def test_legacy_custom_agents_are_backfilled_to_version_one(self) -> None:
        legacy_path = self.root / "legacy.db"
        with sqlite3.connect(legacy_path) as conn:
            conn.execute(
                """
                CREATE TABLE agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    agent_type TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tools_json TEXT NOT NULL DEFAULT '[]',
                    memory_scope TEXT NOT NULL,
                    time_budget_minutes INTEGER NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO agents (
                    id, name, agent_type, goal, status, tools_json, memory_scope,
                    time_budget_minutes, notes, created_at, updated_at
                ) VALUES (
                    'agent_legacy', 'Legacy Agent', 'general', 'Preserve me',
                    'active', '["knowledge.answer"]', 'task-local', 45, 'legacy',
                    '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z'
                )
                """
            )

        migrated = Database(legacy_path)
        with migrated.connection() as conn:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertIn("agent_definition_versions", tables)
            agent = conn.execute(
                """
                SELECT definition_id, definition_version, definition_version_id,
                       root_agent_id, depth, budget_json
                FROM agents
                WHERE id = 'agent_legacy'
                """
            ).fetchone()
            version = conn.execute(
                """
                SELECT version, name, goal, time_budget_minutes
                FROM agent_definition_versions
                WHERE definition_id = ?
                """,
                (agent["definition_id"],),
            ).fetchone()

        self.assertEqual(agent["definition_version"], 1)
        self.assertTrue(agent["definition_version_id"])
        self.assertEqual(agent["root_agent_id"], "agent_legacy")
        self.assertEqual(agent["depth"], 0)
        self.assertEqual(migrated.loads(agent["budget_json"])["time_budget_minutes"], 45)
        self.assertEqual(
            dict(version),
            {
                "version": 1,
                "name": "Legacy Agent",
                "goal": "Preserve me",
                "time_budget_minutes": 45,
            },
        )

    def test_hierarchy_budget_and_success_contract_are_snapshotted_on_runs(self) -> None:
        root = self.agents.create(
            AgentCreate(
                name="Supervisor",
                goal="Coordinate the work",
                status="active",
                budget={"max_tool_calls": 20, "max_tokens": 12_000},
                success_contract={"required_artifacts": ["report.md"], "min_score": 0.8},
            )
        )
        child = self.agents.create(
            AgentCreate(
                name="Researcher",
                goal="Produce the report",
                status="active",
                parent_agent_id=root["id"],
                budget={"max_tool_calls": 5},
                success_contract={"required_artifacts": ["report.md"]},
            )
        )

        self.assertIn("root_agent_id", root)
        self.assertIn("parent_agent_id", child)
        self.assertEqual(root["root_agent_id"], root["id"])
        self.assertIsNone(root["parent_agent_id"])
        self.assertEqual(root["depth"], 0)
        self.assertEqual(child["parent_agent_id"], root["id"])
        self.assertEqual(child["root_agent_id"], root["id"])
        self.assertEqual(child["depth"], 1)

        run = self._runner().run(child["id"], owner_approved=False)

        self.assertEqual(run["definition_id"], child["definition_id"])
        self.assertEqual(run["definition_version"], child["definition_version"])
        self.assertEqual(run["definition_snapshot"]["goal"], "Produce the report")
        self.assertEqual(run["parent_agent_id"], root["id"])
        self.assertEqual(run["root_agent_id"], root["id"])
        self.assertEqual(run["depth"], 1)
        self.assertEqual(run["budget_snapshot"], child["budget"])
        self.assertEqual(run["success_contract_snapshot"], child["success_contract"])
        self.assertEqual(run["usage"]["tool_calls"], 0)
        self.assertFalse(run["evaluation"]["passed"])
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["started_at"])
        self.assertTrue(run["completed_at"])
        self.assertTrue(run["updated_at"])

    def test_agent_budget_and_tool_whitelist_are_enforced_by_executor_contract(self) -> None:
        executor = FakeExecutorService()
        agent = self.agents.create(
            AgentCreate(
                name="Bounded Agent",
                goal="Stay inside limits",
                status="active",
                tools=["diagnostics.run_self_check"],
                budget={"max_tool_calls": 1, "max_tokens": 500},
            )
        )

        run = self._runner(executor=executor).run(agent["id"], owner_approved=False)

        self.assertEqual(executor.last_limits["max_tool_calls"], 1)
        self.assertEqual(
            executor.last_limits["allowed_tool_ids"],
            ["diagnostics.run_self_check"],
        )
        self.assertEqual(run["usage"]["tool_calls"], 0)
        self.assertTrue(run["evaluation"]["passed"])

    def test_agent_runner_rejects_draft_or_blocked_agents_without_creating_runs(self) -> None:
        for status in ("draft", "blocked"):
            agent = self.agents.create(
                AgentCreate(
                    name=f"{status.title()} Agent",
                    goal="This agent must not run yet.",
                    status=status,
                )
            )

            with self.assertRaises(PermissionError):
                self._runner().run(agent["id"], owner_approved=False)

            self.assertEqual(self._runner().list_runs(agent["id"]), [])

    def test_success_contract_marks_completed_work_as_failed_when_output_is_missing(self) -> None:
        agent = self.agents.create(
            AgentCreate(
                name="Contract Agent",
                goal="Return an approved report",
                status="active",
                success_contract={
                    "required_reply_terms": ["approved"],
                    "required_tool_ids": ["diagnostics.run_self_check"],
                },
            )
        )

        run = self._runner().run(agent["id"], owner_approved=False)

        self.assertEqual(run["status"], "failed")
        self.assertFalse(run["evaluation"]["passed"])
        self.assertFalse(run["evaluation"]["checks"]["required_reply_terms"])
        self.assertFalse(run["evaluation"]["checks"]["required_tool_ids"])

    def test_store_run_returns_the_exact_inserted_row(self) -> None:
        agent = self.agents.create(
            AgentCreate(name="Exact Lookup", goal="Return this run", status="active")
        )
        runner = self._runner()

        with patch.object(
            runner,
            "list_runs",
            side_effect=AssertionError("inserted runs must not be recovered by list ordering"),
        ):
            run = runner._store_run(
                agent_id=agent["id"],
                goal=agent["goal"],
                reply="exact reply",
                outcome="completed",
                reasoning_mode="local",
                model_name="test-model",
                created_task_ids=[],
                created_memory_ids=[],
                created_agent_ids=[],
                executed_tools=[],
                blocked_tools=[],
                warning="",
            )

        self.assertEqual(run["reply"], "exact reply")
        self.assertEqual(runner.get_run(run["id"])["id"], run["id"])

    def test_overlapping_runs_derive_status_without_cross_run_races(self) -> None:
        agent = self.agents.create(
            AgentCreate(name="Concurrent Agent", goal="Run twice", status="active")
        )
        reasoner = RacingReasoner()
        runner = self._runner(reasoner)
        errors: list[BaseException] = []

        def execute_run() -> None:
            try:
                runner.run(agent["id"], owner_approved=False)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        first = threading.Thread(target=execute_run)
        second = threading.Thread(target=execute_run)
        first.start()
        self.assertTrue(reasoner.first_started.wait(timeout=5))
        second.start()
        second.join(timeout=10)

        self.assertFalse(second.is_alive())
        self.assertEqual(self.agents.get(agent["id"])["status"], "running")

        reasoner.release_first.set()
        first.join(timeout=10)

        self.assertFalse(first.is_alive())
        self.assertEqual(self.agents.get(agent["id"])["status"], "active")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        outcomes = {run["outcome"] for run in runner.list_runs(agent["id"])}
        self.assertEqual(outcomes, {"completed", "failed"})


if __name__ == "__main__":
    unittest.main()
