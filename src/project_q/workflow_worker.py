from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from project_q.app import create_application
from project_q.config import AppConfig


def _cancel_requested(app, run_id: str) -> bool:
    run = app.workflows.get_run(run_id, include_events=False)
    return bool(run["cancel_requested"]) or app.control.status()["active"]


def execute_node(app, node_run_id: str) -> int:
    node = app.workflows.get_node_run(node_run_id)
    run = app.workflows.get_run(node["workflow_run_id"], include_events=False)
    if node["status"] != "running":
        raise ValueError("workflow node must be running before worker execution")
    snapshot = node["node_snapshot"]
    try:
        if _cancel_requested(app, run["id"]):
            app.workflows.mark_node_terminal(
                node_run_id,
                status="cancelled",
                error="workflow cancellation was requested before execution",
            )
            return 2

        kind = snapshot["kind"]
        if kind == "delay":
            result = _run_delay(app, run["id"], snapshot)
        elif kind == "tool":
            result = _run_tool(app, run, snapshot)
        elif kind == "agent":
            result = _run_agent(app, run, node, snapshot)
        else:
            raise ValueError(f"unsupported workflow node kind: {kind}")

        if _cancel_requested(app, run["id"]):
            app.workflows.mark_node_terminal(
                node_run_id,
                status="cancelled",
                error="workflow cancellation was requested during execution",
            )
            return 2
        app.workflows.mark_node_terminal(
            node_run_id,
            status="succeeded",
            result=result if isinstance(result, dict) else {"result": result},
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        current = app.workflows.get_node_run(node_run_id)
        if current["status"] not in {"succeeded", "failed", "skipped", "cancelled"}:
            app.workflows.mark_node_terminal(
                node_run_id,
                status="failed",
                error=str(exc),
            )
        app.audit.log(
            action_type="workflow_node",
            action_tier=1,
            tool_name="workflow_worker",
            outcome="failed",
            approved_by_owner=run["owner_approved"],
            input_sources=["owner", "workflow"],
            metadata={
                "workflow_run_id": run["id"],
                "node_run_id": node_run_id,
                "node_key": node["node_key"],
            },
            error=str(exc),
        )
        return 1


def _run_delay(app, run_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    seconds = int(snapshot["delay_seconds"])
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _cancel_requested(app, run_id):
            raise RuntimeError("workflow cancelled during delay")
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    return {"delay_seconds": seconds}


def _run_tool(app, run: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    if snapshot.get("requires_owner_approval") and not run["owner_approved"]:
        raise PermissionError("workflow node requires explicit owner approval")
    tool_id = snapshot["tool_id"]
    tool = app.tools.get(tool_id)
    decision = app.policy.authorize_tool(
        tier=tool.definition.tier,
        owner_approved=run["owner_approved"],
        input_sources=["owner", "workflow"],
        tool_id=tool_id,
    )
    if not decision.allowed:
        raise PermissionError(decision.reason)
    result = tool.execute(snapshot.get("payload", {}))
    app.audit.log(
        action_type="workflow_tool",
        action_tier=tool.definition.tier,
        tool_name=tool_id,
        outcome="completed",
        approved_by_owner=run["owner_approved"],
        input_sources=["owner", "workflow"],
        metadata={"workflow_run_id": run["id"], "node_key": snapshot["key"]},
    )
    return result


def _run_agent(
    app,
    run: dict[str, Any],
    node: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    if snapshot.get("requires_owner_approval") and not run["owner_approved"]:
        raise PermissionError("workflow node requires explicit owner approval")
    return app.agent_runner.run(
        snapshot["agent_id"],
        owner_approved=run["owner_approved"],
        workflow_run_id=run["id"],
        workflow_node_run_id=node["id"],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--node-run-id", required=True)
    args = parser.parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    data_root = Path(args.data_root).expanduser().resolve()
    db_path = Path(args.db_path).expanduser().resolve()
    config = AppConfig(
        project_name="Project Q Worker",
        workspace_root=workspace,
        data_root=data_root,
        db_path=db_path,
        worker_mode=True,
    )
    app = create_application(config)
    try:
        node = app.workflows.get_node_run(args.node_run_id)
        if node["workflow_run_id"] != args.run_id:
            raise ValueError("node run does not belong to the requested workflow run")
        return execute_node(app, args.node_run_id)
    finally:
        app.shutdown_services()


if __name__ == "__main__":
    raise SystemExit(main())
