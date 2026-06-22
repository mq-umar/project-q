from __future__ import annotations

import unittest
from pathlib import Path


class WorkflowUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        static = Path(__file__).resolve().parents[1] / "src" / "project_q" / "static"
        cls.html = (static / "index.html").read_text(encoding="utf-8")
        cls.javascript = (static / "app.js").read_text(encoding="utf-8")
        cls.css = (static / "styles.css").read_text(encoding="utf-8")

    def test_workflow_navigation_and_operational_controls_exist(self) -> None:
        for identifier in (
            'data-section="workflows"',
            'id="section-workflows"',
            'id="workflowForm"',
            'id="workflowName"',
            'id="workflowParallelism"',
            'id="workflowNodes"',
            'id="workflowList"',
            'id="workflowRunList"',
            'id="workflowRunDetail"',
        ):
            self.assertIn(identifier, self.html)

    def test_workflow_javascript_uses_real_api_and_accessible_progress(self) -> None:
        for fragment in (
            'fetchJSON("/api/workflows")',
            'fetchJSON("/api/workflow-runs?limit=50")',
            "/api/workflow-runs/${runId}/cancel",
            "/api/workflow-runs/${runId}/retry",
            "/nodes/${nodeId}/decision",
            "approve-workflow-node-button",
            "reject-workflow-node-button",
            "<progress",
            "aria-live",
            "EventSource",
        ):
            self.assertIn(fragment, self.javascript + self.html)

    def test_workflow_layout_has_desktop_and_mobile_rules(self) -> None:
        for selector in (
            ".workflow-layout",
            ".workflow-dag-list",
            ".workflow-run-grid",
            ".workflow-node-row",
        ):
            self.assertIn(selector, self.css)
        self.assertIn("@media", self.css)


if __name__ == "__main__":
    unittest.main()
