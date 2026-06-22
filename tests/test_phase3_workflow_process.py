from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from project_q.services.workflow_process import SubprocessNodeBackend


class WorkflowProcessBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.backend = SubprocessNodeBackend(
            python_executable=Path("C:/Python/python.exe"),
            workspace_root=self.root / "workspace",
            data_root=self.root / "data",
            db_path=self.root / "data" / "custom.db",
            cancel_grace_seconds=0.01,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_start_uses_fixed_worker_module_without_shell(self) -> None:
        process = MagicMock()
        process.pid = 42
        with patch("project_q.services.workflow_process.subprocess.Popen", return_value=process) as popen:
            handle = self.backend.start("workflowrun_123", "node_abc")

        command = popen.call_args.args[0]
        self.assertEqual(
            command[:3],
            ["C:/Python/python.exe", "-m", "project_q.workflow_worker"],
        )
        self.assertIn("--run-id", command)
        self.assertIn("workflowrun_123", command)
        self.assertIn("--node-run-id", command)
        self.assertIn("node_abc", command)
        self.assertIn("--data-root", command)
        self.assertIn((self.root / "data").resolve().as_posix(), command)
        self.assertIn("--db-path", command)
        self.assertIn((self.root / "data" / "custom.db").resolve().as_posix(), command)
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.kwargs["env"]["PROJECT_Q_WORKER_MODE"], "1")
        self.assertIn("phase3-orchestration", popen.call_args.kwargs["env"]["PYTHONPATH"])
        self.assertIn("src", popen.call_args.kwargs["env"]["PYTHONPATH"])
        self.assertTrue(handle.stdout_path.is_relative_to((self.root / "data").resolve()))
        self.assertTrue(handle.stderr_path.is_relative_to((self.root / "data").resolve()))
        handle.close()

    def test_start_rejects_untrusted_identifiers(self) -> None:
        for value in ("", "../escape", "a/b", "x y", "a" * 129):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.backend.start(value, "node_abc")

    def test_cancel_terminates_then_kills_unresponsive_worker(self) -> None:
        process = MagicMock()
        process.pid = 42
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="worker", timeout=0.01),
            subprocess.TimeoutExpired(cmd="worker", timeout=0.01),
            9,
        ]
        with patch(
            "project_q.services.workflow_process.subprocess.Popen",
            return_value=process,
        ):
            handle = self.backend.start("workflowrun_123", "node_abc")

        with patch("project_q.services.workflow_process.os.name", "posix"), patch(
            "project_q.services.workflow_process.os.kill"
        ) as kill:
            handle.cancel()

        process.terminate.assert_called_once_with()
        self.assertEqual(kill.call_args.args, (42, __import__("signal").SIGTERM))
        process.kill.assert_called_once_with()
        self.assertEqual(handle.poll(), 9)
        handle.close()

    def test_pid_helpers_are_bounded_to_the_requested_process_on_posix(self) -> None:
        with patch("project_q.services.workflow_process.os.name", "posix"), patch(
            "project_q.services.workflow_process.os.kill"
        ) as kill:
            self.assertTrue(self.backend.pid_is_running(4321))
            self.backend.cancel_pid(4321)

        self.assertEqual(kill.call_args_list[0].args, (4321, 0))
        self.assertEqual(kill.call_args_list[1].args, (4321, __import__("signal").SIGTERM))

    def test_cancel_pid_terminates_windows_process_tree(self) -> None:
        with patch("project_q.services.workflow_process.os.name", "nt"), patch(
            "project_q.services.workflow_process.subprocess.run"
        ) as run:
            self.backend.cancel_pid(4321)

        self.assertEqual(
            run.call_args.args[0],
            ["taskkill.exe", "/PID", "4321", "/T", "/F"],
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_pid_is_running_uses_windows_tasklist(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='"python.exe","4321","Console","1","42 K"\r\n',
            stderr="",
        )
        with patch("project_q.services.workflow_process.os.name", "nt"), patch(
            "project_q.services.workflow_process.subprocess.run",
            return_value=completed,
        ) as run:
            self.assertTrue(self.backend.pid_is_running(4321))

        self.assertEqual(
            run.call_args.args[0],
            ["tasklist.exe", "/FI", "PID eq 4321", "/FO", "CSV", "/NH"],
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_pid_is_running_reports_false_when_windows_tasklist_has_no_match(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='INFO: No tasks are running which match the specified criteria.\r\n',
            stderr="",
        )
        with patch("project_q.services.workflow_process.os.name", "nt"), patch(
            "project_q.services.workflow_process.subprocess.run",
            return_value=completed,
        ):
            self.assertFalse(self.backend.pid_is_running(4321))


if __name__ == "__main__":
    unittest.main()
