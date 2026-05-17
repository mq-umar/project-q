from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def configured_roots(
    workspace_root: Path,
    data_root: Path | None = None,
    settings_service: Any | None = None,
) -> list[Path]:
    roots = [workspace_root.resolve()]
    if data_root is not None:
        roots.append(data_root.resolve())

    if settings_service is not None:
        settings = settings_service.get_all()
        for raw_root in settings.get("file_access_roots", []) or []:
            normalized = str(raw_root).strip()
            if not normalized:
                continue
            expanded = os.path.expandvars(os.path.expanduser(normalized))
            roots.append(Path(expanded).resolve())

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            deduped.append(root)
            seen.add(key)
    return deduped


def resolve_allowed_path(
    raw_path: str | Path,
    *,
    workspace_root: Path,
    data_root: Path | None = None,
    settings_service: Any | None = None,
    must_exist: bool = False,
) -> Path:
    path_text = str(raw_path or ".").strip() or "."
    expanded = os.path.expandvars(os.path.expanduser(path_text))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve()
    roots = configured_roots(workspace_root, data_root, settings_service)

    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise PermissionError("path is outside allowed roots")
    if must_exist and not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved
