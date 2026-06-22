from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.models import SettingsUpdate
from project_q.services.control import ControlService
from project_q.services.settings import SettingsService
from project_q.storage import Database


class FakeAuditService:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log(self, **payload) -> None:
        self.events.append(payload)


class FakeWorkflowOrchestrator:
    def __init__(self, settings: SettingsService) -> None:
        self.settings = settings
        self.saw_kill_switch_active = False
        self.reasons: list[str] = []

    def cancel_all(self, *, reason: str) -> list[str]:
        self.saw_kill_switch_active = bool(
            self.settings.get_all().get("kill_switch_active", False)
        )
        self.reasons.append(reason)
        return []


class ControlPlaneTests(unittest.TestCase):
    def test_kill_switch_latches_before_cancelling_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "project_q.db")
            settings = SettingsService(db)
            orchestrator = FakeWorkflowOrchestrator(settings)
            control = ControlService(
                settings,
                FakeAuditService(),
                workflow_orchestrator=orchestrator,
            )

            control.activate(reason="race test", source="unit")

            self.assertTrue(orchestrator.saw_kill_switch_active)
            self.assertEqual(orchestrator.reasons, ["kill switch: race test"])

    def test_kill_switch_revokes_existing_owner_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = create_application(
                AppConfig(
                    project_name="Project Q Test",
                    workspace_root=root,
                    data_root=root / ".project_q",
                    db_path=root / ".project_q" / "project_q.db",
                )
            )
            try:
                session = app.owner_auth.create_session(source="test")
                self.assertEqual(app.owner_auth.verify_session(session["token"])["id"], session["id"])

                app.control.activate(reason="stop stale sessions", source="test")

                with self.assertRaises(PermissionError):
                    app.owner_auth.verify_session(session["token"])
            finally:
                app.shutdown_services()

    def test_resume_does_not_reauthorize_old_revoked_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = create_application(
                AppConfig(
                    project_name="Project Q Test",
                    workspace_root=root,
                    data_root=root / ".project_q",
                    db_path=root / ".project_q" / "project_q.db",
                )
            )
            try:
                session = app.owner_auth.create_session(source="test")
                app.control.activate(reason="stop stale sessions", source="test")
                app.control.resume(reason="new verified session", source="test")
                app.settings.update(SettingsUpdate(kill_switch_active=False))

                with self.assertRaises(PermissionError):
                    app.owner_auth.verify_session(session["token"])
            finally:
                app.shutdown_services()


if __name__ == "__main__":
    unittest.main()
