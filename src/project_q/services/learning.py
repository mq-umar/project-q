from __future__ import annotations

import threading
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from project_q.models import MemoryCreate, SettingsUpdate, TaskCreate, utc_now
from project_q.storage import Database


class LearningLabService:
    def __init__(
        self,
        db: Database,
        memory_service,
        task_service,
        agent_service,
        routine_service,
        settings_service,
        tool_registry,
        audit_service,
        diagnostics_service=None,
    ) -> None:
        self.db = db
        self.memory_service = memory_service
        self.task_service = task_service
        self.agent_service = agent_service
        self.routine_service = routine_service
        self.settings_service = settings_service
        self.tool_registry = tool_registry
        self.audit_service = audit_service
        self.diagnostics_service = diagnostics_service
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: str | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()
            started_at = self._started_at
        settings = self.settings_service.get_all()
        runs = self.list_runs(limit=1)
        return {
            "running": running,
            "started_at": started_at,
            "settings": {
                "learning_enabled": settings.get("learning_enabled", False),
                "learning_interval_seconds": settings.get("learning_interval_seconds", 300),
                "learning_max_cycles_per_start": settings.get("learning_max_cycles_per_start", 12),
            },
            "last_run": runs[0] if runs else None,
        }

    def start(self, max_cycles: int | None = None, interval_seconds: int | None = None) -> dict[str, Any]:
        settings = self.settings_service.get_all()
        resolved_max_cycles = max_cycles or int(settings.get("learning_max_cycles_per_start", 12))
        resolved_interval = interval_seconds or int(settings.get("learning_interval_seconds", 300))
        already_running = False
        thread_to_start: threading.Thread | None = None
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                already_running = True
                started_at = self._started_at
            else:
                self._stop_event = threading.Event()
                self._started_at = utc_now()
                started_at = self._started_at
                thread_to_start = threading.Thread(
                    target=self._loop,
                    kwargs={
                        "max_cycles": resolved_max_cycles,
                        "interval_seconds": resolved_interval,
                    },
                    daemon=True,
                    name="ProjectQLearningLab",
                )
                self._thread = thread_to_start
        if already_running:
            return self.status()
        self.settings_service.update(SettingsUpdate(learning_enabled=True))
        if thread_to_start is not None:
            thread_to_start.start()
        return {
            "running": True,
            "started_at": started_at,
            "settings": {
                "learning_enabled": True,
                "learning_interval_seconds": resolved_interval,
                "learning_max_cycles_per_start": resolved_max_cycles,
            },
            "last_run": self.list_runs(limit=1)[0] if self.list_runs(limit=1) else None,
            "max_cycles": resolved_max_cycles,
            "interval_seconds": resolved_interval,
        }

    def stop(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None:
            thread.join(timeout=2)
        self.settings_service.update(SettingsUpdate(learning_enabled=False))
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None
                self._started_at = None
        return self.status()

    def run_once(self, reason: str = "manual", mode: str = "run_once") -> dict[str, Any]:
        run_id = self.db.make_id("learn")
        started_at = utc_now()
        findings = self._collect_findings(reason)
        created_memory_ids = self._write_learning_memory(findings, reason)
        created_task_ids = self._write_follow_up_tasks(findings)
        summary = self._summarize(findings, created_task_ids, created_memory_ids)
        completed_at = utc_now()

        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO learning_runs (
                    id, mode, status, reason, summary, findings_json,
                    created_task_ids_json, created_memory_ids_json,
                    warning, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    mode,
                    "completed",
                    reason,
                    summary,
                    self.db.dumps(findings),
                    self.db.dumps(created_task_ids),
                    self.db.dumps(created_memory_ids),
                    "",
                    started_at,
                    completed_at,
                ),
            )

        self.audit_service.log(
            action_type="learning_cycle",
            action_tier=0,
            tool_name="learning_lab",
            outcome="completed",
            input_sources=["system", "local_state"],
            metadata={
                "run_id": run_id,
                "finding_count": len(findings),
                "created_task_ids": created_task_ids,
                "created_memory_ids": created_memory_ids,
            },
        )
        return self.get_run(run_id)

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM learning_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM learning_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._row_to_dict(row)

    def _loop(self, max_cycles: int, interval_seconds: int) -> None:
        try:
            for cycle_number in range(1, max_cycles + 1):
                if self._stop_event.is_set():
                    break
                self.run_once(reason=f"background cycle {cycle_number}", mode="continuous")
                if cycle_number >= max_cycles:
                    break
                if self._stop_event.wait(interval_seconds):
                    break
        finally:
            with self._lock:
                self._thread = None
                self._started_at = None
            self.settings_service.update(SettingsUpdate(learning_enabled=False))

    def _collect_findings(self, reason: str) -> list[dict[str, Any]]:
        settings = self.settings_service.get_all()
        tools = self.tool_registry.describe_all()
        tasks = self.task_service.list_all(limit=500)
        agents = self.agent_service.list_all(limit=500)
        routines = self.routine_service.list_all(limit=500)
        memories = self.memory_service.list_all(limit=500)
        audit_entries = self.audit_service.list_recent(limit=200)
        failed_entries = [entry for entry in audit_entries if entry["outcome"] in {"failed", "blocked"}]

        findings: list[dict[str, Any]] = [
            {
                "kind": "capability_snapshot",
                "message": (
                    f"Learning cycle '{reason}' saw {len(tools)} tools, {len(tasks)} tasks, "
                    f"{len(agents)} agents, {len(routines)} routines, and {len(memories)} memories."
                ),
                "severity": "info",
            }
        ]

        # ---- Task completion rate (last 24 h) ----
        cutoff_24h = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        tasks_24h = [t for t in tasks if (t.get("created_at") or "") >= cutoff_24h]
        completed_24h = [t for t in tasks_24h if t.get("status") == "completed"]
        if tasks_24h:
            rate = len(completed_24h) / len(tasks_24h)
            findings.append(
                {
                    "kind": "task_metric",
                    "message": (
                        f"Task completion rate in the last 24 h: "
                        f"{len(completed_24h)}/{len(tasks_24h)} ({rate:.0%})."
                    ),
                    "severity": "info" if rate >= 0.5 else "medium",
                    "confidence": 0.9,
                    "type": "task_metric",
                    "text": f"Task completion rate last 24h: {rate:.2f}",
                }
            )

        # ---- Tool failure patterns ----
        audit_24h = [e for e in audit_entries if (e.get("timestamp") or "") >= cutoff_24h]
        fail_counts: Counter[str] = Counter(
            e["tool_name"] for e in audit_24h if e["outcome"] in {"failed", "blocked"}
        )
        for tool_name, count in fail_counts.most_common(5):
            if count >= 3:
                findings.append(
                    {
                        "kind": "tool_failure_pattern",
                        "message": f"Tool '{tool_name}' failed or was blocked {count} times in the last 24 h.",
                        "severity": "high" if count >= 5 else "medium",
                        "task_title": f"Investigate repeated failures for tool '{tool_name}'",
                        "type": "tool_failure_pattern",
                        "text": f"Tool {tool_name} failed {count} times in 24h",
                        "confidence": min(0.6 + 0.08 * count, 0.95),
                    }
                )

        # ---- Agent success/failure patterns ----
        agent_runs_24h = [
            e for e in audit_24h if e.get("action_type") == "agent_run"
        ]
        if agent_runs_24h:
            succeeded = [e for e in agent_runs_24h if e["outcome"] == "completed"]
            success_rate = len(succeeded) / len(agent_runs_24h)
            findings.append(
                {
                    "kind": "success_pattern",
                    "message": (
                        f"Agent runs in last 24 h: {len(agent_runs_24h)} total, "
                        f"{len(succeeded)} succeeded ({success_rate:.0%})."
                    ),
                    "severity": "info" if success_rate >= 0.7 else "medium",
                    "type": "success_pattern",
                    "text": f"Agent success rate last 24h: {success_rate:.2f}",
                    "confidence": 0.85,
                }
            )

        # ---- Configuration / capability gaps ----
        if not settings.get("file_access_roots"):
            findings.append(
                {
                    "kind": "configuration_gap",
                    "message": "No owner file access roots are configured yet, so broad computer access is disabled.",
                    "severity": "medium",
                    "task_title": "Configure owner file access roots for Project Q",
                }
            )

        if failed_entries:
            latest = failed_entries[0]
            if self._is_expected_policy_block(latest):
                findings.append(
                    {
                        "kind": "expected_policy_gate",
                        "message": (
                            f"Recent {latest['tool_name']} action was blocked by owner-approval policy. "
                            "This is expected until the owner approves the action or raises the trusted automation tier."
                        ),
                        "severity": "info",
                    }
                )
            elif self._is_transient_http_disconnect(latest):
                findings.append(
                    {
                        "kind": "transient_http_disconnect",
                        "message": "Recent http_server disconnect was classified as transient browser/client noise.",
                        "severity": "info",
                    }
                )
            else:
                findings.append(
                    {
                        "kind": "recent_failure",
                        "message": f"Recent {latest['tool_name']} action ended as {latest['outcome']}: {latest['error']}",
                        "severity": "medium",
                        "task_title": f"Investigate Project Q {latest['tool_name']} {latest['outcome']} event",
                    }
                )

        if not any(tool["tool_id"] == "filesystem.search_files" for tool in tools):
            findings.append(
                {
                    "kind": "capability_gap",
                    "message": "File search tool is unavailable, limiting owner-file discovery.",
                    "severity": "high",
                }
            )

        if self.diagnostics_service is not None:
            diagnostics = self.diagnostics_service.run(source="learning_lab")
            findings.append(
                {
                    "kind": "self_diagnostics",
                    "message": diagnostics["summary"],
                    "severity": "info" if diagnostics["status"] == "completed" else "high",
                    "diagnostic_run_id": diagnostics["id"],
                }
            )
            repair = diagnostics.get("repair") or {}
            if repair.get("status") == "completed":
                findings.append(
                    {
                        "kind": "self_repair",
                        "message": repair["summary"],
                        "severity": "info",
                        "diagnostic_run_id": diagnostics["id"],
                    }
                )

        return findings

    def get_metrics(self) -> dict[str, Any]:
        """Return operational metrics computed from the audit log and tasks table."""
        cutoff_24h = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")

        # Tasks
        tasks = self.task_service.list_all(limit=10000)
        tasks_24h = [t for t in tasks if (t.get("created_at") or "") >= cutoff_24h]
        completed_24h = [t for t in tasks_24h if t.get("status") == "completed"]
        task_completion_rate = (
            len(completed_24h) / len(tasks_24h) if tasks_24h else 0.0
        )

        # Audit log
        audit_entries = self.audit_service.list_recent(limit=2000)
        audit_24h = [e for e in audit_entries if (e.get("timestamp") or "") >= cutoff_24h]

        tool_executions_24h = len(audit_24h)
        tool_failures_24h = sum(1 for e in audit_24h if e["outcome"] in {"failed", "blocked"})
        tool_failure_rate = (
            tool_failures_24h / tool_executions_24h if tool_executions_24h else 0.0
        )

        # Per-tool breakdown
        tool_total: Counter[str] = Counter(e["tool_name"] for e in audit_24h)
        tool_fail: Counter[str] = Counter(
            e["tool_name"] for e in audit_24h if e["outcome"] in {"failed", "blocked"}
        )
        top_tools = [
            {
                "tool_id": tool_id,
                "count": count,
                "failure_rate": round(tool_fail[tool_id] / count, 4) if count else 0.0,
            }
            for tool_id, count in tool_total.most_common(10)
        ]

        # Agent runs
        agent_runs_24h = [e for e in audit_24h if e.get("action_type") == "agent_run"]
        agent_successes = sum(1 for e in agent_runs_24h if e["outcome"] == "completed")
        agent_success_rate = (
            agent_successes / len(agent_runs_24h) if agent_runs_24h else 0.0
        )

        # Memory count
        memories = self.memory_service.list_all(limit=10000)

        return {
            "tasks_completed_24h": len(completed_24h),
            "tasks_created_24h": len(tasks_24h),
            "task_completion_rate": round(task_completion_rate, 4),
            "tool_executions_24h": tool_executions_24h,
            "tool_failure_rate": round(tool_failure_rate, 4),
            "top_tools": top_tools,
            "agent_runs_24h": len(agent_runs_24h),
            "agent_success_rate": round(agent_success_rate, 4),
            "memory_count": len(memories),
        }

    def reflect_on_task(self, task_id: str) -> dict[str, Any]:
        """Fetch a task, inspect related audit entries, and store a lesson memory."""
        task = self.task_service.get(task_id)
        task_title = task.get("title", task_id)
        task_status = task.get("status", "unknown")

        # Find audit entries whose metadata references this task_id
        audit_entries = self.audit_service.list_recent(limit=500)
        related = [
            e for e in audit_entries
            if task_id in str(e.get("metadata", ""))
        ]
        tools_used = list({e["tool_name"] for e in related if e.get("tool_name")})
        errors = [
            e["error"] for e in related
            if e.get("error") and e["outcome"] in {"failed", "blocked"}
        ]

        if task_status == "completed":
            tools_str = ", ".join(tools_used) if tools_used else "no recorded tools"
            text = f"Task '{task_title}' was completed successfully via {tools_str}."
            confidence = 0.85
        else:
            error_summary = "; ".join(errors[:3]) if errors else "no specific errors recorded"
            text = f"Task '{task_title}' encountered issues: {error_summary}."
            confidence = 0.7

        memory = self.memory_service.create(
            MemoryCreate(
                text=text,
                kind="semantic",
                source="learning_lab",
                confidence=confidence,
                owner_confirmed=False,
                tags=["task_reflection", task_id],
                metadata={"task_id": task_id, "task_status": task_status, "tools_used": tools_used},
            )
        )
        return {"task_id": task_id, "memory_id": memory["id"], "text": text}

    @staticmethod
    def _is_expected_policy_block(entry: dict[str, Any]) -> bool:
        text = f"{entry.get('error', '')} {entry.get('metadata', '')}".lower()
        return (
            entry.get("outcome") == "blocked"
            and entry.get("tool_name") == "windows.launch_application"
            and "requires owner approval" in text
        )

    @staticmethod
    def _is_transient_http_disconnect(entry: dict[str, Any]) -> bool:
        text = f"{entry.get('error', '')} {entry.get('metadata', '')}".lower()
        return entry.get("tool_name") == "http_server" and (
            "winerror 10053" in text or "connection was aborted" in text
        )

    def _write_learning_memory(self, findings: list[dict[str, Any]], reason: str) -> list[str]:
        summary = "; ".join(finding["message"] for finding in findings[:3])
        memory = self.memory_service.create(
            MemoryCreate(
                text=f"Learning Lab cycle ({reason}): {summary}",
                kind="semantic",
                source="learning_lab",
                confidence=0.72,
                owner_confirmed=False,
                tags=["learning_lab", "simulation"],
                metadata={"finding_count": len(findings)},
            )
        )
        return [memory["id"]]

    def _write_follow_up_tasks(self, findings: list[dict[str, Any]]) -> list[str]:
        existing_titles = {task["title"].strip().lower() for task in self.task_service.list_all(limit=500)}
        created: list[str] = []
        for finding in findings:
            title = finding.get("task_title")
            if not title or title.strip().lower() in existing_titles:
                continue
            task = self.task_service.create(
                TaskCreate(
                    title=title,
                    description=finding["message"],
                    priority=2 if finding.get("severity") in {"high", "medium"} else 4,
                    source="learning_lab",
                    metadata={"finding_kind": finding["kind"]},
                )
            )
            existing_titles.add(title.strip().lower())
            created.append(task["id"])
        return created

    def _summarize(
        self,
        findings: list[dict[str, Any]],
        created_task_ids: list[str],
        created_memory_ids: list[str],
    ) -> str:
        return (
            f"Learning Lab completed {len(findings)} simulation finding(s), "
            f"created {len(created_task_ids)} follow-up task(s), and stored "
            f"{len(created_memory_ids)} memory item(s)."
        )

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "mode": row["mode"],
            "status": row["status"],
            "reason": row["reason"],
            "summary": row["summary"],
            "findings": self.db.loads(row["findings_json"]),
            "created_task_ids": self.db.loads(row["created_task_ids_json"]),
            "created_memory_ids": self.db.loads(row["created_memory_ids_json"]),
            "warning": row["warning"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }
