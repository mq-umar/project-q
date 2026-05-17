from __future__ import annotations

import base64
import json
import re
import subprocess
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from project_q.tools.access import configured_roots, resolve_allowed_path
from project_q.tools.base import ToolDefinition


SKIPPED_SEARCH_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".project_q",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "$Recycle.Bin",
    "System Volume Information",
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "AppData",
}

TEXT_EXTENSIONS = {
    ".bat",
    ".cfg",
    ".css",
    ".csv",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".ps1",
    ".py",
    ".rs",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

DOCUMENT_EXTENSIONS = {".docx", ".rtf"}

FILE_QUERY_STOPWORDS = {
    "a",
    "about",
    "all",
    "and",
    "called",
    "document",
    "file",
    "files",
    "find",
    "for",
    "from",
    "in",
    "is",
    "me",
    "my",
    "of",
    "open",
    "option",
    "please",
    "reveal",
    "search",
    "select",
    "show",
    "that",
    "the",
    "to",
    "up",
}


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_powershell_json(script: str, *, timeout_seconds: int = 10) -> dict[str, Any]:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "PowerShell command failed")
    stdout = completed.stdout.strip()
    return json.loads(stdout) if stdout else {}


def _open_or_reveal_path(path: Path, mode: str, *, dry_run: bool = False) -> dict[str, Any]:
    normalized_mode = "open" if mode == "open" else "reveal"
    if dry_run:
        return {
            "opened": True,
            "mode": normalized_mode,
            "path": str(path),
            "dry_run": True,
        }
    path_literal = json.dumps(str(path))
    if normalized_mode == "open":
        script = f"""
        $path = {path_literal}
        Start-Process -FilePath $path | Out-Null
        @{{ opened = $true; mode = "open"; path = $path }} | ConvertTo-Json -Compress
        """
    else:
        script = f"""
        $path = {path_literal}
        Start-Process explorer.exe -ArgumentList "/select,`"$path`"" | Out-Null
        @{{ opened = $true; mode = "reveal"; path = $path }} | ConvertTo-Json -Compress
        """
    return _run_powershell_json(script, timeout_seconds=10)


class FileChoiceStore:
    def __init__(self, data_root: Path) -> None:
        self.root = data_root / "file_choices"

    def save(self, *, instruction: str, query: str, action: str, options: list[dict[str, Any]]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        choice_id = "choice_" + uuid.uuid4().hex[:12]
        record = {
            "id": choice_id,
            "instruction": instruction,
            "query": query,
            "action": action,
            "options": options,
            "created_at": _utc_now(),
        }
        (self.root / f"{choice_id}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record

    def load(self, choice_id: str | None = None) -> dict[str, Any]:
        if choice_id:
            target = self.root / f"{choice_id}.json"
        else:
            choices = sorted(self.root.glob("choice_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
            if not choices:
                raise KeyError("no pending file choices")
            target = choices[0]
        if not target.exists():
            raise KeyError(choice_id or "latest")
        return json.loads(target.read_text(encoding="utf-8"))


class FilesystemListTool:
    definition = ToolDefinition(
        tool_id="filesystem.list_directory",
        name="List Directory",
        description="List files in an allowed directory",
        tier=0,
    )

    def __init__(self, workspace_root: Path, data_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.data_root = data_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = self._resolve_path(payload.get("path", "."))
        entries = []
        for item in sorted(target.iterdir(), key=lambda current: (current.is_file(), current.name.lower())):
            entries.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "is_dir": item.is_dir(),
                }
            )
        return {"path": str(target), "entries": entries}

    def _resolve_path(self, raw_path: str) -> Path:
        return resolve_allowed_path(
            raw_path,
            workspace_root=self.workspace_root,
            data_root=self.data_root,
            settings_service=self.settings_service,
            must_exist=True,
        )


class FilesystemReadTool:
    definition = ToolDefinition(
        tool_id="filesystem.read_file",
        name="Read File",
        description="Read a text file from an allowed directory",
        tier=0,
    )

    def __init__(self, workspace_root: Path, data_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.data_root = data_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = self._resolve_path(payload["path"])
        content = target.read_text(encoding="utf-8")
        return {"path": str(target), "content": content}

    def _resolve_path(self, raw_path: str) -> Path:
        return resolve_allowed_path(
            raw_path,
            workspace_root=self.workspace_root,
            data_root=self.data_root,
            settings_service=self.settings_service,
            must_exist=True,
        )


class FilesystemWriteTool:
    definition = ToolDefinition(
        tool_id="filesystem.write_file",
        name="Write File",
        description="Write a text file in an allowed directory",
        tier=2,
    )

    def __init__(self, workspace_root: Path, data_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.data_root = data_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = self._resolve_path(payload["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload.get("content", ""), encoding="utf-8")
        return {"path": str(target), "bytes_written": len(payload.get("content", "").encode("utf-8"))}

    def _resolve_path(self, raw_path: str) -> Path:
        return resolve_allowed_path(
            raw_path,
            workspace_root=self.workspace_root,
            data_root=self.data_root,
            settings_service=self.settings_service,
        )


class FilesystemSearchTool:
    definition = ToolDefinition(
        tool_id="filesystem.search_files",
        name="Search Files",
        description="Search file names and optional text content inside allowed roots",
        tier=0,
    )

    def __init__(self, workspace_root: Path, data_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.data_root = data_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        root = self._resolve_root(payload.get("root", "."))
        include_content = bool(payload.get("include_content", False))
        max_results = max(1, min(int(payload.get("max_results", 50)), 200))
        max_files = max(100, min(int(payload.get("max_files", 25000)), 200000))
        extensions = self._normalize_extensions(payload.get("extensions"))

        lowered_query = query.lower()
        results: list[dict[str, Any]] = []
        scanned_files = 0
        skipped_errors = 0

        for current in self._walk(root):
            if len(results) >= max_results or scanned_files >= max_files:
                break
            try:
                if current.is_dir():
                    continue
                scanned_files += 1
                if extensions and current.suffix.lower() not in extensions:
                    continue

                name_match = lowered_query in current.name.lower()
                content_match = None
                if include_content and self._looks_readable(current):
                    content_match = self._content_preview(current, lowered_query)

                if not name_match and content_match is None:
                    continue

                results.append(
                    {
                        "name": current.name,
                        "path": str(current),
                        "match_type": "content" if content_match is not None else "filename",
                        "size_bytes": current.stat().st_size,
                        "preview": content_match or current.name,
                    }
                )
            except (OSError, UnicodeDecodeError):
                skipped_errors += 1

        return {
            "root": str(root),
            "query": query,
            "include_content": include_content,
            "result_count": len(results),
            "scanned_files": scanned_files,
            "skipped_errors": skipped_errors,
            "results": results,
        }

    def _resolve_root(self, raw_root: str) -> Path:
        root = resolve_allowed_path(
            raw_root,
            workspace_root=self.workspace_root,
            data_root=self.data_root,
            settings_service=self.settings_service,
            must_exist=True,
        )
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        return root

    def _walk(self, root: Path):
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                children = sorted(current.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if child.name in SKIPPED_SEARCH_DIRS:
                        continue
                    stack.append(child)
                else:
                    yield child

    def _looks_readable(self, path: Path) -> bool:
        if path.suffix.lower() in TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS:
            return True
        try:
            with path.open("rb") as handle:
                sample = handle.read(2048)
        except OSError:
            return False
        return b"\x00" not in sample

    def _content_preview(self, path: Path, lowered_query: str) -> str | None:
        if path.stat().st_size > 512 * 1024:
            return None
        if path.suffix.lower() == ".docx":
            text = _read_docx_text(path)
            lowered_text = text.lower()
            if lowered_query not in lowered_text:
                return None
            index = max(lowered_text.find(lowered_query), 0)
            start = max(index - 80, 0)
            return text[start : start + 240].strip()
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for index, line in enumerate(handle, start=1):
                if lowered_query in line.lower():
                    return f"line {index}: {line.strip()[:240]}"
        return None

    def _normalize_extensions(self, raw_extensions: Any) -> set[str]:
        if not raw_extensions:
            return set()
        if isinstance(raw_extensions, str):
            candidates = [item.strip() for item in raw_extensions.split(",")]
        else:
            candidates = [str(item).strip() for item in raw_extensions]
        return {item.lower() if item.startswith(".") else f".{item.lower()}" for item in candidates if item}


def _read_docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            document = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    except (KeyError, OSError, zipfile.BadZipFile):
        return ""
    text = re.sub(r"<[^>]+>", " ", document)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )
    return re.sub(r"\s+", " ", text).strip()


def _tokenize_file_request(value: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[A-Za-z0-9]+", value.lower()):
        if len(token) < 2 or token in FILE_QUERY_STOPWORDS:
            continue
        tokens.append(token)
    deduped: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token not in seen:
            deduped.append(token)
            seen.add(token)
    return deduped


class FilesystemResolveFileRequestTool:
    definition = ToolDefinition(
        tool_id="filesystem.resolve_file_request",
        name="Resolve File Request",
        description="Resolve fuzzy file requests, ask for confirmation on ambiguity, and open/reveal confident matches",
        tier=1,
    )

    def __init__(self, workspace_root: Path, data_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.data_root = data_root
        self.settings_service = settings_service
        self.choice_store = FileChoiceStore(data_root)

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        instruction = str(payload.get("instruction", "")).strip()
        query = str(payload.get("query", "") or self._query_from_instruction(instruction)).strip()
        if not query:
            raise ValueError("query or instruction is required")
        action = self._normalize_action(str(payload.get("action", "reveal")))
        root = self._resolve_root(payload.get("root", "."))
        max_results = max(1, min(int(payload.get("max_results", 10)), 50))
        max_files = max(100, min(int(payload.get("max_files", 50000)), 200000))
        dry_run = bool(payload.get("dry_run", False))

        candidates = self._rank_candidates(
            root=root,
            query=query,
            instruction=instruction,
            max_results=max_results,
            max_files=max_files,
        )
        if not candidates:
            return {
                "status": "not_found",
                "root": str(root),
                "query": query,
                "instruction": instruction,
                "message": f"I couldn't find a matching file for `{query}` in the allowed roots.",
                "options": [],
            }

        top = candidates[0]
        second_score = candidates[1]["score"] if len(candidates) > 1 else -1
        specific_terms = [term for term in _tokenize_file_request(instruction) if term not in _tokenize_file_request(query)]
        clear_winner = len(candidates) == 1 or (specific_terms and top["score"] >= second_score + 20 and top["score"] >= 35)

        if clear_winner:
            action_result = _open_or_reveal_path(Path(top["path"]), action, dry_run=dry_run)
            return {
                "status": "resolved",
                "root": str(root),
                "query": query,
                "instruction": instruction,
                "selected": top,
                "action": action,
                "action_result": action_result,
                "options": candidates,
                "message": f"I found the best match and {action}ed it: {top['name']}",
            }

        choice_record = self.choice_store.save(
            instruction=instruction,
            query=query,
            action=action,
            options=candidates,
        )
        return {
            "status": "needs_confirmation",
            "root": str(root),
            "query": query,
            "instruction": instruction,
            "choice_id": choice_record["id"],
            "options": candidates,
            "message": "Which file did you mean? Reply with `open option 1` or `reveal option 1`.",
        }

    def _resolve_root(self, raw_root: str) -> Path:
        root = resolve_allowed_path(
            raw_root,
            workspace_root=self.workspace_root,
            data_root=self.data_root,
            settings_service=self.settings_service,
            must_exist=True,
        )
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        return root

    def _rank_candidates(
        self,
        *,
        root: Path,
        query: str,
        instruction: str,
        max_results: int,
        max_files: int,
    ) -> list[dict[str, Any]]:
        query_terms = _tokenize_file_request(query)
        instruction_terms = _tokenize_file_request(instruction)
        target_terms = [term for term in instruction_terms if term not in set(query_terms)]
        candidates: list[dict[str, Any]] = []
        scanned_files = 0

        for current in self._walk(root):
            if scanned_files >= max_files:
                break
            try:
                if not current.is_file():
                    continue
                scanned_files += 1
                preview = self._preview(current, query_terms + target_terms)
                score = self._score(current, preview, query_terms, target_terms)
                if score <= 0:
                    continue
                candidates.append(
                    {
                        "name": current.name,
                        "path": str(current),
                        "score": score,
                        "size_bytes": current.stat().st_size,
                        "modified_at": datetime.fromtimestamp(current.stat().st_mtime, UTC)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "preview": preview[:260] if preview else current.name,
                    }
                )
            except (OSError, UnicodeDecodeError):
                continue

        candidates.sort(key=lambda item: (-item["score"], item["name"].lower()))
        return candidates[:max_results]

    def _walk(self, root: Path):
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                children = sorted(current.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if child.name in SKIPPED_SEARCH_DIRS:
                        continue
                    stack.append(child)
                else:
                    yield child

    def _preview(self, path: Path, terms: list[str]) -> str:
        suffix = path.suffix.lower()
        if suffix == ".docx":
            return _read_docx_text(path)
        if suffix not in TEXT_EXTENSIONS or path.stat().st_size > 512 * 1024:
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
        lowered = text.lower()
        for term in terms:
            index = lowered.find(term)
            if index >= 0:
                start = max(index - 80, 0)
                return text[start : start + 260].strip()
        return text[:260].strip()

    def _score(self, path: Path, preview: str, query_terms: list[str], target_terms: list[str]) -> int:
        name = path.name.lower()
        stem = path.stem.lower()
        preview_lower = preview.lower()
        score = 0
        for term in query_terms:
            if term in stem:
                score += 25
            elif term in name:
                score += 18
            if term in preview_lower:
                score += 8
        for term in target_terms:
            if term in stem:
                score += 35
            elif term in name:
                score += 24
            if term in preview_lower:
                score += 30
        if query_terms and stem == " ".join(query_terms):
            score += 10
        if path.suffix.lower() in {".docx", ".pdf", ".rtf", ".txt"}:
            score += 3
        return score

    def _query_from_instruction(self, instruction: str) -> str:
        patterns = (
            r"(?:search|scan|look through)\s+(?:my\s+)?files\s+(?:for|about|matching)?\s*(.+)$",
            r"find\s+(?:my\s+)?files?\s+(?:for|about|called|named|matching)?\s*(.+)$",
            r"open\s+(?:the\s+)?(?:file\s+)?(.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match and match.group(1).strip():
                return match.group(1).strip(" .")
        return instruction

    def _normalize_action(self, action: str) -> str:
        lowered = action.lower().strip()
        if lowered in {"open", "launch"}:
            return "open"
        return "reveal"


class FilesystemOpenFileChoiceTool:
    definition = ToolDefinition(
        tool_id="filesystem.open_file_choice",
        name="Open File Choice",
        description="Open or reveal a previously offered file-search choice",
        tier=1,
    )

    def __init__(self, workspace_root: Path, data_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.data_root = data_root
        self.settings_service = settings_service
        self.choice_store = FileChoiceStore(data_root)

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        choice_id = str(payload.get("choice_id") or "").strip() or None
        selection = int(payload.get("selection", 1))
        mode = "open" if str(payload.get("mode", "reveal")).lower().strip() == "open" else "reveal"
        dry_run = bool(payload.get("dry_run", False))
        record = self.choice_store.load(choice_id)
        options = record.get("options", [])
        if selection < 1 or selection > len(options):
            raise ValueError("selection is outside the available options")
        selected = options[selection - 1]
        path = resolve_allowed_path(
            selected["path"],
            workspace_root=self.workspace_root,
            data_root=self.data_root,
            settings_service=self.settings_service,
            must_exist=True,
        )
        action_result = _open_or_reveal_path(path, mode, dry_run=dry_run)
        return {
            "status": "opened",
            "choice_id": record["id"],
            "selection": selection,
            "selected": selected,
            "mode": mode,
            "action_result": action_result,
        }


class FilesystemAllowedRootsTool:
    definition = ToolDefinition(
        tool_id="filesystem.allowed_roots",
        name="Allowed File Roots",
        description="Show workspace, data, and owner-configured file access roots",
        tier=0,
    )

    def __init__(self, workspace_root: Path, data_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.data_root = data_root
        self.settings_service = settings_service

    def execute(self, _payload: dict[str, Any]) -> dict[str, Any]:
        roots = configured_roots(self.workspace_root, self.data_root, self.settings_service)
        return {"roots": [str(root) for root in roots]}
