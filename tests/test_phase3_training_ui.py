from __future__ import annotations

import unittest
from pathlib import Path


class TrainingUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        static = Path(__file__).resolve().parents[1] / "src" / "project_q" / "static"
        cls.html = (static / "index.html").read_text(encoding="utf-8")
        cls.javascript = (static / "app.js").read_text(encoding="utf-8")
        cls.css = (static / "styles.css").read_text(encoding="utf-8")

    def test_training_lab_exposes_job_status_and_safe_lifecycle_controls(self) -> None:
        for fragment in (
            'id="trainingJobList"',
            'id="trainingJobDetail"',
            "audit-training-job-button",
            "promote-training-job-button",
            "rollback-training-job-button",
        ):
            self.assertIn(fragment, self.html + self.javascript)

    def test_training_lab_uses_authenticated_lifecycle_endpoints(self) -> None:
        for fragment in (
            'fetchJSON("/api/training/jobs")',
            "/api/training/jobs/${jobId}/audit",
            "/api/training/jobs/${jobId}/promote",
            "/api/training/jobs/${jobId}/rollback",
        ):
            self.assertIn(fragment, self.javascript)

    def test_training_job_layout_has_stable_desktop_and_mobile_rules(self) -> None:
        self.assertIn(".training-job-row", self.css)
        self.assertIn(".training-job-metrics", self.css)
        self.assertIn("@media", self.css)


if __name__ == "__main__":
    unittest.main()
