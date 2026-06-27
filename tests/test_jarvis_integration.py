"""Tests for the jarvis-main ports: proactive suggestions + A/B experiments."""
from __future__ import annotations

import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.services.suggestions import SuggestionsService


class SuggestionsTests(unittest.TestCase):
    def test_web_project_missing_assets_gets_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "package.json").write_text("{}")
            (p / "index.html").write_text("<html></html>")
            (p / "main.js").write_text("//")
            (p / "style.css").write_text("/* */")
            types = {s["action_type"] for s in SuggestionsService().suggest_for_project(str(p), "build")}
            self.assertEqual(types, {"favicon", "tests", "readme"})

    def test_no_suggestions_when_assets_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "package.json").write_text("{}")
            (p / "index.html").write_text("<html></html>")
            (p / "favicon.ico").write_text("x")
            (p / "README.md").write_text("# x")
            (p / "tests").mkdir()
            self.assertEqual(SuggestionsService().suggest_for_project(str(p), "build"), [])

    def test_missing_dir_is_safe(self) -> None:
        nope = str(Path(tempfile.gettempdir()) / "pq_no_project_xyz")
        self.assertEqual(SuggestionsService().suggest_for_project(nope, "build"), [])


class ExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="AB Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8906,
        )
        self.app = create_application(config)

    def tearDown(self) -> None:
        sd = getattr(self.app, "shutdown_services", None)
        if callable(sd):
            try:
                sd()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def test_services_wired_on_app(self) -> None:
        self.assertIsNotNone(self.app.suggestions)
        self.assertIsNotNone(self.app.experiments)

    def test_assign_requires_variants(self) -> None:
        with self.assertRaises(ValueError):
            self.app.experiments.assign("x", [])

    def test_assign_record_stats_winner(self) -> None:
        exp = self.app.experiments
        a = exp.assign("greeting", ["v1", "v2"])
        self.assertIn(a["variant"], ("v1", "v2"))
        self.assertTrue(exp.record(a["experiment_id"], True))
        self.assertFalse(exp.record(a["experiment_id"], False))  # idempotent
        # Build a clear winner: v1 always passes, v2 always fails.
        for _ in range(25):
            exp.record(exp.assign("exp", ["v1"])["experiment_id"], True)
        for _ in range(25):
            exp.record(exp.assign("exp", ["v2"])["experiment_id"], False)
        stats = exp.stats("exp")
        self.assertEqual(stats["v1"]["passed"], 25)
        self.assertEqual(stats["v2"]["failed"], 25)
        self.assertGreater(stats["v1"]["success_rate"], stats["v2"]["success_rate"])
        self.assertEqual(exp.winner("exp"), "v1")
        self.assertIsNone(exp.winner("greeting"))  # not enough data


if __name__ == "__main__":
    unittest.main()
