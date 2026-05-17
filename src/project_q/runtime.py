from __future__ import annotations

import os
import shutil
from pathlib import Path


def discover_node_executable() -> str:
    override = os.environ.get("PROJECT_Q_NODE_PATH")
    if override and Path(override).exists():
        return override

    bundled = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
        / "bin"
        / "node.exe"
    )
    if bundled.exists():
        return str(bundled)

    discovered = shutil.which("node")
    if discovered:
        return discovered

    return ""


def discover_node_modules_path() -> str:
    override = os.environ.get("PROJECT_Q_NODE_MODULES")
    if override and Path(override).exists():
        return override

    bundled = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
        / "node_modules"
    )
    if bundled.exists():
        return str(bundled)

    return ""
