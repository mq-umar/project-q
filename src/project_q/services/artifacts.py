from __future__ import annotations

import re
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any


class ArtifactValidationService:
    def validate_written_file(self, path: str | Path) -> dict[str, Any]:
        target = Path(path)
        suffix = target.suffix.lower()
        if suffix != ".py":
            return {
                "status": "skipped",
                "kind": suffix or "unknown",
                "message": "No validator configured for this file type.",
            }

        original = target.read_text(encoding="utf-8")
        syntax = self._compile_python(original, target)
        if syntax["status"] == "ok":
            normalized = self._repair_workspace_placeholders(original)
            normalized = self._repair_fstring_path_separators(normalized)
            if normalized != original:
                normalized_syntax = self._compile_python(normalized, target)
                if normalized_syntax["status"] == "ok":
                    target.write_text(normalized, encoding="utf-8")
                    normalized_syntax["status"] = "repaired"
                    normalized_syntax["message"] = self._build_semantic_repair_message(original, normalized)
                    return normalized_syntax
            return syntax

        repaired = self._repair_windows_path_literals(original)
        repaired = self._repair_workspace_placeholders(repaired)
        if repaired != original:
            repaired = self._repair_fstring_path_separators(repaired)
            repaired_syntax = self._compile_python(repaired, target)
            if repaired_syntax["status"] == "ok":
                target.write_text(repaired, encoding="utf-8")
                repaired_syntax["status"] = "repaired"
                repaired_syntax["message"] = "Repaired invalid Windows path literals and verified Python syntax."
                return repaired_syntax

        return syntax

    def _compile_python(self, content: str, path: Path) -> dict[str, Any]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                compile(content, str(path), "exec")
        except SyntaxError as exc:
            return {
                "status": "invalid",
                "kind": "python",
                "message": exc.msg,
                "line": exc.lineno,
                "offset": exc.offset,
                "text": (exc.text or "").rstrip(),
            }
        return {
            "status": "ok",
            "kind": "python",
            "message": "Python syntax validated successfully.",
        }

    def _repair_windows_path_literals(self, content: str) -> str:
        pattern = re.compile(r"(?P<quote>['\"])(?P<path>[A-Za-z]:\\[^'\"]*)(?P=quote)")

        def replace(match: re.Match[str]) -> str:
            quote = match.group("quote")
            path = match.group("path")
            normalized = path.replace("\\", "/")
            return f"{quote}{normalized}{quote}"

        return pattern.sub(replace, content)

    def _repair_fstring_path_separators(self, content: str) -> str:
        return content.replace("\\{", "/{")

    def _repair_workspace_placeholders(self, content: str) -> str:
        repaired = content
        patterns = [
            r"'(?:\{workspace_root\}|workspace_root)'",
            r'"(?:\{workspace_root\}|workspace_root)"',
            r"'(?:\{repo_root\}|repo_root)'",
            r'"(?:\{repo_root\}|repo_root)"',
        ]
        for pattern in patterns:
            repaired = re.sub(pattern, "Path(__file__).resolve().parents[1]", repaired)

        if repaired != content and "Path(__file__).resolve().parents[1]" in repaired and "from pathlib import Path" not in repaired:
            if repaired.startswith("#!"):
                lines = repaired.splitlines()
                lines.insert(1, "from pathlib import Path")
                repaired = "\n".join(lines) + ("\n" if repaired.endswith("\n") else "")
            else:
                repaired = "from pathlib import Path\n" + repaired
        return repaired

    def _build_semantic_repair_message(self, original: str, normalized: str) -> str:
        if "workspace_root" in original or "repo_root" in original:
            return "Repaired unresolved workspace placeholder paths and verified Python syntax."
        return "Repaired suspicious Windows-style f-string path separators and verified Python syntax."

    def smoke_test_written_file(self, path: str | Path) -> dict[str, Any]:
        target = Path(path)
        if target.suffix.lower() != ".py":
            return {
                "status": "skipped",
                "kind": "python",
                "message": "Runtime smoke test is only enabled for Python files.",
            }

        content = target.read_text(encoding="utf-8")
        if "__main__" not in content:
            return {
                "status": "skipped",
                "kind": "python",
                "message": "Runtime smoke test skipped because the file does not declare a main entrypoint.",
            }

        if target.parent.name.lower() != "notes":
            return {
                "status": "skipped",
                "kind": "python",
                "message": "Runtime smoke test currently runs only for note-style generated utility scripts.",
            }

        if any(marker in content for marker in ("subprocess.", "os.remove", "shutil.rmtree", "Path.unlink", "socket.")):
            return {
                "status": "skipped",
                "kind": "python",
                "message": "Runtime smoke test skipped because the file uses potentially destructive operations.",
            }

        cwd = self._discover_workspace_root(target)
        completed = subprocess.run(
            [sys.executable, str(target)],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "Python smoke test failed."
            return {
                "status": "runtime_invalid",
                "kind": "python",
                "message": message,
                "stdout": completed.stdout[-1000:],
                "stderr": completed.stderr[-1000:],
            }

        return {
            "status": "runtime_ok",
            "kind": "python",
            "message": "Python runtime smoke test passed.",
            "stdout": completed.stdout[-1000:],
            "stderr": completed.stderr[-1000:],
        }

    def _discover_workspace_root(self, target: Path) -> Path:
        for candidate in [target.parent, *target.parents]:
            if (candidate / ".project_q").exists():
                return candidate
        return target.parent
