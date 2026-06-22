from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from project_q.services.workflow_graph import FailurePolicy, NodeStatus, WorkflowGraph, WorkflowState


class WorkflowOrchestratorService:
    def __init__(
        self,
        workflow_service,
        node_backend,
        *,
        poll_interval_seconds: float = 0.5,
        agent_runner=None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.workflow_service = workflow_service
        self.node_backend = node_backend
        self.agent_runner = agent_runner
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._handles: dict[str, Any] = {}
        self._handle_runs: dict[str, str] = {}
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._iteration_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.reconcile()
        try:
            self.workflow_service.prune_history()
        except Exception:  # noqa: BLE001
            pass
        self._thread = threading.Thread(
            target=self._run_loop,
            name="project-q-workflow-orchestrator",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(2.0, self.poll_interval_seconds * 4))
        self._thread = None
        run_ids = set(self._handle_runs.values())
        for node_run_id, handle in list(self._handles.items()):
            try:
                handle.cancel()
            except Exception:  # noqa: BLE001
                pass
            finally:
                handle.close()
            try:
                node = self.workflow_service.get_node_run(node_run_id)
                if node["status"] not in {"succeeded", "failed", "skipped", "cancelled"}:
                    self.workflow_service.mark_node_terminal(
                        node_run_id,
                        status="cancelled",
                        error="application shutdown cancelled the workflow worker",
                    )
            except (KeyError, ValueError):
                pass
        self._handles.clear()
        self._handle_runs.clear()
        for run_id in run_ids:
            try:
                run = self.workflow_service.get_run(run_id, include_events=False)
                if run["status"] not in {"completed", "failed", "cancelled"}:
                    self.workflow_service.request_cancel(
                        run_id,
                        reason="application shutdown",
                    )
                    self._cancel_run(
                        self.workflow_service.get_run(run_id, include_events=False)
                    )
            except (KeyError, ValueError):
                pass

    def submit(self, workflow_id: str, *, owner_approved: bool) -> dict[str, Any]:
        run = self.workflow_service.start_run(
            workflow_id,
            owner_approved=owner_approved,
        )
        self._wake_event.set()
        return run

    def retry(self, run_id: str, *, owner_approved: bool | None = None) -> dict[str, Any]:
        previous = self.workflow_service.get_run(run_id, include_events=False)
        approved = previous["owner_approved"] if owner_approved is None else owner_approved
        run = self.workflow_service.start_run(
            previous["workflow_id"],
            owner_approved=bool(approved),
            retry_of_run_id=run_id,
        )
        self._wake_event.set()
        return run

    def cancel(self, run_id: str, *, reason: str = "") -> dict[str, Any]:
        run = self.workflow_service.request_cancel(run_id, reason=reason)
        self._wake_event.set()
        return run

    def cancel_all(self, *, reason: str) -> list[str]:
        cancelled: list[str] = []
        for run in self.workflow_service.list_runs(
            statuses=("queued", "running", "cancelling"),
            limit=500,
        ):
            self.cancel(run["id"], reason=reason)
            cancelled.append(run["id"])
        self.run_once()
        return cancelled

    def reconcile(self) -> None:
        for run in self.workflow_service.list_runs(
            statuses=("running", "cancelling"),
            limit=500,
        ):
            for node in self.workflow_service.get_run(run["id"], include_events=False)["nodes"]:
                if node["status"] != "running" or node["id"] in self._handles:
                    continue
                process_id = node["process_id"]
                if process_id and self.node_backend.pid_is_running(process_id):
                    continue
                self.workflow_service.mark_node_terminal(
                    node["id"],
                    status="failed",
                    error="workflow worker process was not running during restart reconciliation",
                )

    def run_once(self) -> None:
        if not self._iteration_lock.acquire(blocking=False):
            return
        try:
            self._collect_finished_handles()
            self._reconcile_orphan_agent_runs()
            self._reconcile_untracked_workers()
            for summary in self.workflow_service.list_runs(
                statuses=("queued", "running", "cancelling"),
                limit=500,
            ):
                run = self.workflow_service.get_run(summary["id"], include_events=False)
                if run["status"] == "cancelling" or run["cancel_requested"]:
                    self._cancel_run(run)
                    continue
                self._advance_run(run)
        finally:
            self._iteration_lock.release()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                pass
            self._wake_event.wait(self.poll_interval_seconds)
            self._wake_event.clear()

    def _collect_finished_handles(self) -> None:
        for node_run_id, handle in list(self._handles.items()):
            node = self.workflow_service.get_node_run(node_run_id)
            if node["status"] == "running" and self._node_timed_out(node):
                try:
                    handle.cancel()
                    self.workflow_service.mark_node_terminal(
                        node_run_id,
                        status="failed",
                        error=(
                            "workflow node timed out after "
                            f"{node['node_snapshot']['timeout_seconds']} seconds"
                        ),
                    )
                finally:
                    handle.close()
                    self._handles.pop(node_run_id, None)
                    self._handle_runs.pop(node_run_id, None)
                continue
            return_code = handle.poll()
            if return_code is None:
                continue
            try:
                node = self.workflow_service.get_node_run(node_run_id)
                if node["status"] == "running":
                    self.workflow_service.mark_node_terminal(
                        node_run_id,
                        status="failed",
                        error=f"workflow worker exited with code {return_code} without recording a terminal result",
                    )
            finally:
                handle.close()
                self._handles.pop(node_run_id, None)
                self._handle_runs.pop(node_run_id, None)

    def _reconcile_untracked_workers(self) -> None:
        for run in self.workflow_service.list_runs(
            statuses=("running", "cancelling"),
            limit=500,
        ):
            details = self.workflow_service.get_run(run["id"], include_events=False)
            for node in details["nodes"]:
                if node["status"] != "running" or node["id"] in self._handles:
                    continue
                process_id = node["process_id"]
                if self._node_timed_out(node):
                    if process_id:
                        self.node_backend.cancel_pid(process_id)
                    self.workflow_service.mark_node_terminal(
                        node["id"],
                        status="failed",
                        error=(
                            "workflow node timed out after "
                            f"{node['node_snapshot']['timeout_seconds']} seconds"
                        ),
                    )
                    continue
                if process_id and self.node_backend.pid_is_running(process_id):
                    continue
                self.workflow_service.mark_node_terminal(
                    node["id"],
                    status="failed",
                    error="workflow worker exited without a persisted terminal result",
                )

    def _reconcile_orphan_agent_runs(self) -> None:
        """Fail agent_runs left 'running' after their owning workflow node ended."""
        if self.agent_runner is None:
            return
        try:
            running = self.agent_runner.list_running_workflow_node_runs()
        except Exception:  # noqa: BLE001
            return
        for entry in running:
            node_run_id = entry["workflow_node_run_id"]
            try:
                node = self.workflow_service.get_node_run(node_run_id)
            except (KeyError, ValueError):
                node = None
            if node is not None and node["status"] not in {
                "succeeded",
                "failed",
                "skipped",
                "cancelled",
            }:
                continue
            status = "cancelled" if node and node["status"] == "cancelled" else "failed"
            try:
                self.agent_runner.fail_run_for_workflow_node(
                    node_run_id,
                    status=status,
                    error="owning workflow node terminated before the agent run finalized",
                )
            except Exception:  # noqa: BLE001
                continue

    def _cancel_run(self, run: dict[str, Any]) -> None:
        for node in run["nodes"]:
            if node["status"] in {"succeeded", "failed", "skipped", "cancelled"}:
                continue
            handle = self._handles.pop(node["id"], None)
            self._handle_runs.pop(node["id"], None)
            if handle is not None:
                try:
                    handle.cancel()
                finally:
                    handle.close()
            elif node["status"] == "running" and node["process_id"]:
                self.node_backend.cancel_pid(node["process_id"])
            self.workflow_service.mark_node_terminal(
                node["id"],
                status="cancelled",
                error=run["cancel_reason"] or "workflow cancelled",
            )
        self.workflow_service.set_run_terminal(
            run["id"],
            status="cancelled",
            result={"reason": run["cancel_reason"] or "workflow cancelled"},
        )

    def _advance_run(self, run: dict[str, Any]) -> None:
        graph = WorkflowGraph(
            run["definition_snapshot"]["nodes"],
            parallelism=run["definition_snapshot"]["parallelism"],
        )
        statuses = {
            node["node_key"]: self._graph_status(node["status"])
            for node in run["nodes"]
        }
        state = WorkflowState.restore(graph, statuses)
        nodes_by_key = {node["node_key"]: node for node in run["nodes"]}

        for key, graph_status in state.statuses.items():
            node = nodes_by_key[key]
            if graph_status is NodeStatus.SKIPPED and node["status"] == "pending":
                self.workflow_service.mark_node_terminal(
                    node["id"],
                    status="skipped",
                    error="dependency or fail-fast policy prevented execution",
                )

        run = self.workflow_service.get_run(run["id"], include_events=False)
        nodes_by_key = {node["node_key"]: node for node in run["nodes"]}
        statuses = {
            key: self._graph_status(node["status"])
            for key, node in nodes_by_key.items()
        }
        state = WorkflowState.restore(graph, statuses)
        if state.is_complete:
            self._finalize_run(run, graph)
            return

        running_agent_ids = {
            node["node_snapshot"].get("agent_id")
            for active in self.workflow_service.list_runs(
                statuses=("running",),
                limit=500,
            )
            for node in self.workflow_service.get_run(active["id"], include_events=False)["nodes"]
            if node["status"] == "running" and node["node_snapshot"].get("kind") == "agent"
        }
        for key in state.ready_keys:
            node = nodes_by_key[key]
            snapshot = node["node_snapshot"]
            agent_id = snapshot.get("agent_id") if snapshot.get("kind") == "agent" else None
            if agent_id and agent_id in running_agent_ids:
                continue
            self._dispatch(run, node)
            if agent_id:
                running_agent_ids.add(agent_id)

    def _dispatch(self, run: dict[str, Any], node: dict[str, Any]) -> None:
        if node["node_snapshot"].get("kind") == "approval":
            self.workflow_service.mark_node_waiting_approval(node["id"])
            return
        claimed = self.workflow_service.mark_node_running(node["id"], process_id=None)
        handle = None
        try:
            handle = self.node_backend.start(run["id"], node["id"])
            self.workflow_service.set_node_process_id(node["id"], handle.pid)
        except Exception as exc:  # noqa: BLE001
            if handle is not None:
                try:
                    handle.cancel()
                finally:
                    handle.close()
            self.workflow_service.mark_node_terminal(
                node["id"],
                status="failed",
                error=f"failed to launch workflow worker: {exc}",
            )
            return
        self._handles[claimed["id"]] = handle
        self._handle_runs[claimed["id"]] = run["id"]

    def _finalize_run(self, run: dict[str, Any], graph: WorkflowGraph) -> None:
        failed_fail_fast = any(
            node["status"] == "failed"
            and graph[node["node_key"]].failure_policy is FailurePolicy.FAIL
            for node in run["nodes"]
        )
        status = "failed" if failed_fail_fast else "completed"
        self.workflow_service.set_run_terminal(
            run["id"],
            status=status,
            result={
                "nodes": {
                    node["node_key"]: {
                        "status": node["status"],
                        "result": node["result"],
                        "error": node["error"],
                    }
                    for node in run["nodes"]
                }
            },
            error="one or more fail-fast nodes failed" if failed_fail_fast else "",
        )

    @staticmethod
    def _graph_status(status: str) -> NodeStatus:
        mapping = {
            "pending": NodeStatus.PENDING,
            "running": NodeStatus.RUNNING,
            "waiting_approval": NodeStatus.RUNNING,
            "succeeded": NodeStatus.SUCCEEDED,
            "failed": NodeStatus.FAILED,
            "skipped": NodeStatus.SKIPPED,
            "cancelled": NodeStatus.SKIPPED,
        }
        try:
            return mapping[status]
        except KeyError as exc:
            raise ValueError(f"unsupported node status: {status}") from exc

    @staticmethod
    def _node_timed_out(node: dict[str, Any]) -> bool:
        started_at = node.get("started_at")
        if not started_at:
            return False
        timeout_seconds = int(node.get("node_snapshot", {}).get("timeout_seconds", 1800))
        try:
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return (datetime.now(UTC) - started.astimezone(UTC)).total_seconds() >= timeout_seconds
