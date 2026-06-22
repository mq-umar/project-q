from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _validated_id(value: str, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if not _SAFE_ID.fullmatch(cleaned):
        raise ValueError(f"{field_name} contains unsupported characters")
    return cleaned


@dataclass(slots=True)
class SubprocessNodeHandle:
    process: subprocess.Popen
    stdout_path: Path
    stderr_path: Path
    _stdout: BinaryIO
    _stderr: BinaryIO
    cancel_grace_seconds: float
    max_log_bytes: int
    _closed: bool = False
    _return_code: int | None = None

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    def poll(self) -> int | None:
        if self._return_code is not None:
            return self._return_code
        return_code = self.process.poll()
        if return_code is not None:
            self._return_code = int(return_code)
            self._truncate_logs()
        return self._return_code

    def cancel(self) -> int:
        return_code = self.process.poll()
        if return_code is None:
            self.process.terminate()
            try:
                return_code = self.process.wait(timeout=self.cancel_grace_seconds)
            except subprocess.TimeoutExpired:
                _cancel_process_tree(self.process.pid)
                try:
                    return_code = self.process.wait(timeout=self.cancel_grace_seconds)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    return_code = self.process.wait(timeout=self.cancel_grace_seconds)
        self._return_code = int(return_code)
        self.close()
        return self._return_code

    def close(self) -> None:
        if self._closed:
            return
        self._truncate_logs()
        self._stdout.close()
        self._stderr.close()
        process_handle = getattr(self.process, "_handle", None)
        if self._return_code is not None and process_handle is not None:
            close = getattr(process_handle, "Close", None)
            if callable(close):
                close()
        self._closed = True

    def _truncate_logs(self) -> None:
        self._stdout.flush()
        self._stderr.flush()
        for path in (self.stdout_path, self.stderr_path):
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if size <= self.max_log_bytes:
                continue
            with path.open("rb") as handle:
                handle.seek(-self.max_log_bytes, os.SEEK_END)
                tail = handle.read()
            with path.open("wb") as handle:
                handle.write(tail)


class SubprocessNodeBackend:
    def __init__(
        self,
        *,
        python_executable: Path,
        workspace_root: Path,
        data_root: Path,
        db_path: Path,
        cancel_grace_seconds: float = 5.0,
        max_log_bytes: int = 1024 * 1024,
    ) -> None:
        if cancel_grace_seconds <= 0:
            raise ValueError("cancel_grace_seconds must be positive")
        if max_log_bytes < 1024:
            raise ValueError("max_log_bytes must be at least 1024")
        self.python_executable = Path(python_executable)
        self.workspace_root = Path(workspace_root).resolve()
        self.data_root = Path(data_root).resolve()
        self.db_path = Path(db_path).resolve()
        self.cancel_grace_seconds = float(cancel_grace_seconds)
        self.max_log_bytes = int(max_log_bytes)

    def start(self, run_id: str, node_run_id: str) -> SubprocessNodeHandle:
        safe_run_id = _validated_id(run_id, "run_id")
        safe_node_run_id = _validated_id(node_run_id, "node_run_id")
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        log_dir = self.data_root / "workflow_logs" / safe_run_id
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = (log_dir / f"{safe_node_run_id}.out.log").resolve()
        stderr_path = (log_dir / f"{safe_node_run_id}.err.log").resolve()
        if not stdout_path.is_relative_to(self.data_root) or not stderr_path.is_relative_to(self.data_root):
            raise ValueError("workflow log path escaped the data root")

        stdout_handle = stdout_path.open("wb")
        stderr_handle = stderr_path.open("wb")
        command = [
            self.python_executable.as_posix(),
            "-m",
            "project_q.workflow_worker",
            "--workspace",
            self.workspace_root.as_posix(),
            "--data-root",
            self.data_root.as_posix(),
            "--db-path",
            self.db_path.as_posix(),
            "--run-id",
            safe_run_id,
            "--node-run-id",
            safe_node_run_id,
        ]
        environment = os.environ.copy()
        source_root = Path(__file__).resolve().parents[2]
        inherited_pythonpath = environment.get("PYTHONPATH", "")
        environment.update(
            {
                "PROJECT_Q_WORKER_MODE": "1",
                "PROJECT_Q_WORKSPACE_ROOT": str(self.workspace_root),
                "PYTHONUTF8": "1",
                "PYTHONPATH": os.pathsep.join(
                    item
                    for item in (str(source_root), inherited_pythonpath)
                    if item
                ),
            }
        )
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                command,
                cwd=self.workspace_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                creationflags=creation_flags,
            )
        except Exception:
            stdout_handle.close()
            stderr_handle.close()
            raise
        return SubprocessNodeHandle(
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            _stdout=stdout_handle,
            _stderr=stderr_handle,
            cancel_grace_seconds=self.cancel_grace_seconds,
            max_log_bytes=self.max_log_bytes,
        )

    @staticmethod
    def pid_is_running(process_id: int | None) -> bool:
        if process_id is None:
            return False
        if os.name == "nt":
            return _windows_pid_is_running(int(process_id))
        try:
            os.kill(int(process_id), 0)
        except (OSError, ValueError):
            return False
        return True

    @staticmethod
    def cancel_pid(process_id: int | None) -> None:
        if process_id is None:
            return
        if os.name == "nt":
            _cancel_process_tree(int(process_id))
            return
        try:
            os.kill(int(process_id), signal.SIGTERM)
        except ProcessLookupError:
            return


def _cancel_process_tree(process_id: int | None) -> None:
    if process_id is None:
        return
    if os.name != "nt":
        try:
            os.kill(int(process_id), signal.SIGTERM)
        except ProcessLookupError:
            return
        return
    try:
        subprocess.run(
            ["taskkill.exe", "/PID", str(int(process_id)), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return


def _windows_pid_is_running(process_id: int) -> bool:
    try:
        completed = subprocess.run(
            ["tasklist.exe", "/FI", f"PID eq {int(process_id)}", "/FO", "CSV", "/NH"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    if completed.returncode != 0:
        return False
    needle = f'"{int(process_id)}"'
    for line in str(completed.stdout or "").splitlines():
        clean = line.strip()
        if not clean or clean.lower().startswith("info:"):
            continue
        if needle in clean:
            return True
    return False
