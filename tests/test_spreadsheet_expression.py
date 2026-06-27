"""Tests for spreadsheet.analyze derived cross-column formulas (additive)."""
from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

import openpyxl

from project_q.app import create_application
from project_q.config import AppConfig


class SpreadsheetExpressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="Spreadsheet Expr Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8912,
        )
        self.app = create_application(config)
        self.tool = self.app.tools.get("spreadsheet.analyze")

    def tearDown(self) -> None:
        sd = getattr(self.app, "shutdown_services", None)
        if callable(sd):
            try:
                sd()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _workbook(self, rows: list[list], headers: list[str], name: str = "data.xlsx") -> str:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(headers)
        for row in rows:
            ws.append(row)
        path = self.root / name
        wb.save(path)
        return str(path)

    def test_derived_difference_sum(self) -> None:
        path = self._workbook([["a", 100, 60], ["b", 50, 20]], ["Item", "Revenue", "Cost"])
        out = self.tool.execute({"path": path, "expression": "revenue - cost", "operation": "sum"})
        self.assertEqual(out["result"], 70)  # (100-60) + (50-20)
        self.assertEqual(out["derived_count"], 2)
        self.assertEqual(out["expression"], "revenue - cost")

    def test_derived_fuzzy_header_and_average(self) -> None:
        path = self._workbook([["a", 100, 60], ["b", 60, 20]], ["Item", "Sales Amount", "Cost"])
        # "sales_amount" must fuzzy-match the "Sales Amount" header.
        out = self.tool.execute({"path": path, "expression": "sales_amount - cost", "operation": "average"})
        self.assertEqual(out["result"], 40)  # mean(40, 40)

    def test_division_and_zero_division_skipped(self) -> None:
        path = self._workbook([["a", 100, 0], ["b", 80, 40]], ["Item", "Revenue", "Cost"])
        out = self.tool.execute({"path": path, "expression": "revenue / cost", "operation": "sum"})
        self.assertEqual(out["derived_count"], 1)  # row with cost=0 skipped
        self.assertEqual(out["skipped_rows"], 1)
        self.assertEqual(out["result"], 2)  # 80/40

    def test_non_numeric_rows_skipped(self) -> None:
        path = self._workbook([["a", "n/a", 5], ["b", 30, 10]], ["Item", "Revenue", "Cost"])
        out = self.tool.execute({"path": path, "expression": "revenue - cost", "operation": "sum"})
        self.assertEqual(out["derived_count"], 1)
        self.assertEqual(out["result"], 20)

    def test_unknown_column_raises(self) -> None:
        path = self._workbook([["a", 100]], ["Item", "Revenue"])
        with self.assertRaises(ValueError):
            self.tool.execute({"path": path, "expression": "revenue - margin", "operation": "sum"})

    def test_no_code_execution(self) -> None:
        path = self._workbook([["a", 1]], ["Item", "Revenue"])
        for hostile in ("__import__('os').system('echo hi')", "revenue.__class__", "open('x')"):
            with self.assertRaises(ValueError):
                self.tool.execute({"path": path, "expression": hostile, "operation": "sum"})

    def test_default_path_unchanged_without_expression(self) -> None:
        path = self._workbook([["a", 10], ["b", 30]], ["Item", "Revenue"])
        out = self.tool.execute({"path": path, "operation": "sum", "column": "Revenue"})
        self.assertEqual(out["result"], 40)
        self.assertNotIn("expression", out)


if __name__ == "__main__":
    unittest.main()
