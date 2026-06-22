from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.models import RoutineCreate, RoutineUpdate
from project_q.server import ProjectQHandler


class RoutineVersionTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.app.shutdown_services()
        self.temp_dir.cleanup()

    def test_create_update_and_rollback_keep_immutable_versions(self) -> None:
        routine = self.app.routines.create(
            RoutineCreate(
                name="Morning review",
                goal="Summarize inbox",
                notes="first version",
            )
        )

        status_only = self.app.routines.update(
            routine["id"],
            RoutineUpdate(status="running"),
        )
        changed = self.app.routines.update(
            routine["id"],
            RoutineUpdate(goal="Summarize inbox and tasks", notes="second version"),
        )
        versions = self.app.routines.list_versions(routine["id"])

        self.assertEqual(routine["version"], 1)
        self.assertEqual(status_only["version"], 1)
        self.assertEqual(changed["version"], 2)
        self.assertEqual([item["version"] for item in versions], [2, 1])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.app.db.connection() as conn:
                conn.execute(
                    "UPDATE routine_versions SET goal = 'tampered' WHERE routine_id = ? AND version = 1",
                    (routine["id"],),
                )

        with self.assertRaises(PermissionError):
            self.app.routines.rollback(routine["id"], version=1, owner_confirmed=False)

        rolled_back = self.app.routines.rollback(
            routine["id"],
            version=1,
            owner_confirmed=True,
        )

        self.assertEqual(rolled_back["goal"], "Summarize inbox")
        self.assertEqual(rolled_back["notes"], "first version")
        self.assertEqual(rolled_back["version"], 3)
        versions = self.app.routines.list_versions(routine["id"])
        self.assertEqual([item["version"] for item in versions], [3, 2, 1])
        self.assertEqual(versions[0]["rollback_of_version"], 1)

    def test_routine_runs_snapshot_exact_definition_version(self) -> None:
        routine = self.app.routines.create(
            RoutineCreate(
                name="Snapshot routine",
                goal="Use the original goal",
                notes="original notes",
            )
        )
        run = self.app.routine_runner.run(routine["id"], owner_approved=False)

        self.app.routines.update(
            routine["id"],
            RoutineUpdate(goal="Use the changed goal", notes="changed notes"),
        )
        stored = self.app.routine_runner.list_runs(routine["id"])[0]

        self.assertEqual(run["routine_version"], 1)
        self.assertEqual(stored["routine_version"], 1)
        self.assertEqual(stored["routine_snapshot"]["goal"], "Use the original goal")
        self.assertEqual(stored["routine_snapshot"]["notes"], "original notes")
        self.assertEqual(self.app.routines.get(routine["id"])["version"], 2)


class RoutineVersionApiTests(unittest.TestCase):
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
        handler = type("RoutineVersionApiHandler", (ProjectQHandler,), {"app": self.app})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        response = urllib.request.urlopen(f"{self.base_url}/", timeout=10)
        self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.app.shutdown_services()
        self.temp_dir.cleanup()

    def request(self, method: str, path: str, payload=None, *, owner=True):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if owner:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        response = urllib.request.urlopen(request, timeout=10)
        body = response.read().decode("utf-8")
        return response.status, json.loads(body) if body else {}

    def test_versions_and_rollback_api_require_owner_and_restore_definition(self) -> None:
        _, routine = self.request(
            "POST",
            "/api/routines",
            {
                "name": "API rollback routine",
                "goal": "First goal",
                "notes": "v1",
            },
        )
        self.request(
            "PUT",
            f"/api/routines/{routine['id']}",
            {"goal": "Second goal", "notes": "v2"},
        )

        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request(
                "GET",
                f"/api/routines/{routine['id']}/versions",
                owner=False,
            )
        self.assertEqual(captured.exception.code, 401)

        status, versions = self.request("GET", f"/api/routines/{routine['id']}/versions")
        self.assertEqual(status, 200)
        self.assertEqual([item["version"] for item in versions["items"]], [2, 1])

        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request(
                "POST",
                f"/api/routines/{routine['id']}/rollback",
                {"version": 1, "owner_confirmed": False},
            )
        self.assertEqual(captured.exception.code, 403)

        status, rolled_back = self.request(
            "POST",
            f"/api/routines/{routine['id']}/rollback",
            {"version": 1, "owner_confirmed": True},
        )

        self.assertEqual(status, 200)
        self.assertEqual(rolled_back["goal"], "First goal")
        self.assertEqual(rolled_back["notes"], "v1")
        self.assertEqual(rolled_back["version"], 3)


if __name__ == "__main__":
    unittest.main()
