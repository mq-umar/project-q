"""Regression tests for the plugin-system security hardening (2026-06-26)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from project_q.services.plugins import PluginManager, PluginManifest, _safe_format


class PluginHardeningTests(unittest.TestCase):
    def test_safe_format_blocks_attribute_traversal(self) -> None:
        # A traversal template must not leak object internals and must not raise.
        out = _safe_format("x={q.__class__.__mro__}", {"q": "hi"})
        self.assertNotIn("class", out)
        self.assertNotIn("object", out)
        self.assertEqual(out, "x=")  # traversal field resolves to empty

    def test_safe_format_blocks_index_traversal_and_never_raises(self) -> None:
        self.assertEqual(_safe_format("{q[0]}", {"q": "abc"}), "")
        # Malformed/odd templates must never raise.
        self.assertIsInstance(_safe_format("{unclosed", {"q": "x"}), str)
        self.assertEqual(_safe_format("hello {name}", {"name": "world"}), "hello world")

    def test_reserved_windows_id_is_rejected(self) -> None:
        for reserved in ("con", "nul", "com1", "lpt9"):
            with self.assertRaises(ValidationError):
                PluginManifest.model_validate(
                    {"id": reserved, "name": "x", "type": "shell", "command": "echo hi"}
                )

    def test_remove_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = PluginManager(Path(tmp) / "plugins")
            self.assertFalse(mgr.remove("../../etc/passwd"))
            self.assertFalse(mgr.remove("a/b"))
            self.assertIsNone(mgr.get_tool(".."))

    def test_list_redacts_header_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = PluginManager(Path(tmp) / "plugins")
            mgr.install(
                {
                    "id": "tok-1",
                    "name": "Token plugin",
                    "type": "http",
                    "method": "GET",
                    "url": "https://api.example.com/x",
                    "headers": {"Authorization": "Bearer SECRET-TOKEN"},
                }
            )
            listed = mgr.list()
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["headers"]["Authorization"], "***")
            self.assertNotIn("SECRET-TOKEN", str(listed))


if __name__ == "__main__":
    unittest.main()
