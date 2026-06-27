"""Proactive post-build suggestions.

Heuristic (no-LLM) follow-up suggestions after a generated project/website is
built — e.g. a missing favicon, no tests, or no README. Adapted for Project Q
from the personal-use-licensed "JARVIS" assistant by Ethan Rogers; rewritten to
Project Q's neutral tone, dict return shape, and never-raise contract.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_WEB_INDICATORS = frozenset(
    {
        "package.json", "index.html", "index.tsx", "index.jsx",
        "App.tsx", "App.jsx", "vite.config.ts", "next.config.js",
    }
)
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_README_NAMES = ("README.md", "readme.md", "README", "README.txt")
_FAVICONS = (
    "favicon.ico", "favicon.png", "favicon.svg",
    "public/favicon.ico", "public/favicon.png", "public/favicon.svg",
    "src/assets/favicon.ico",
)


class SuggestionsService:
    """Heuristic, owner-facing follow-up suggestions after a project build."""

    def suggest_for_project(
        self, project_path: str, task_type: str = "build"
    ) -> list[dict[str, Any]]:
        """Return heuristic follow-up suggestions for a freshly-built project.

        Pure filesystem checks (no LLM, no network). Never raises.
        """
        try:
            path = Path(project_path)
            if not path.exists() or not path.is_dir():
                return []
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for check in (self._check_favicon, self._check_tests, self._check_readme):
            try:
                suggestion = check(path, task_type)
            except OSError:
                suggestion = None
            if suggestion:
                out.append(suggestion)
        return out

    @staticmethod
    def _is_web_project(path: Path) -> bool:
        try:
            entries = {e.name for e in path.iterdir() if not e.name.startswith(".")}
        except OSError:
            return False
        return bool(entries & _WEB_INDICATORS)

    def _check_favicon(self, path: Path, task_type: str) -> dict[str, Any] | None:
        if task_type not in ("build", "feature") or not self._is_web_project(path):
            return None
        if any((path / f).exists() for f in _FAVICONS):
            return None
        return {
            "text": "This web project has no favicon. Add one?",
            "action_type": "favicon",
            "task": "Add a favicon to the project",
            "working_dir": str(path),
        }

    def _check_tests(self, path: Path, task_type: str) -> dict[str, Any] | None:
        if task_type not in ("build", "feature", "fix"):
            return None
        entries = {e.name.lower() for e in path.iterdir()}
        if entries & _TEST_DIRS:
            return None
        for child in path.iterdir():
            if child.name.startswith(".") or child.name == "node_modules":
                continue
            if "test" in child.name.lower() or "spec" in child.name.lower():
                return None
            if child.is_dir():
                try:
                    if any(
                        "test" in g.name.lower() or "spec" in g.name.lower()
                        for g in child.iterdir()
                    ):
                        return None
                except OSError:
                    continue
        return {
            "text": "There are no tests yet. Generate some?",
            "action_type": "tests",
            "task": "Write tests for the project",
            "working_dir": str(path),
        }

    def _check_readme(self, path: Path, task_type: str) -> dict[str, Any] | None:
        if task_type not in ("build", "feature"):
            return None
        if any((path / name).exists() for name in _README_NAMES):
            return None
        file_count = sum(
            1 for e in path.iterdir()
            if not e.name.startswith(".") and e.name != "node_modules"
        )
        if file_count < 3:
            return None
        return {
            "text": "This project has no README. Create one?",
            "action_type": "readme",
            "task": "Create a README.md for the project",
            "working_dir": str(path),
        }
