from __future__ import annotations

from pathlib import Path
from typing import Any

from project_q.models import MemoryCreate, TaskCreate, utc_now
from project_q.storage import Database


CORE_TOOL_IDS = {
    "browser.complete_goal",
    "diagnostics.auto_repair",
    "diagnostics.run_self_check",
    "filesystem.allowed_roots",
    "filesystem.list_directory",
    "filesystem.read_file",
    "filesystem.search_files",
    "filesystem.write_file",
    "knowledge.answer",
    "code.generate_website",
    "code.generate_project",
    "project.plan_build",
    "shell.run_command",
    "training.capability_plan",
    "training.export_dataset",
    "training.prepare_lora_job",
    "windows.open_url",
}


class SelfDiagnosticsService:
    def __init__(
        self,
        db: Database,
        memory_service,
        task_service,
        settings_service,
        tool_registry,
        audit_service,
        artifact_service,
        workspace_root: Path,
        data_root: Path,
        self_repair_service=None,
    ) -> None:
        self.db = db
        self.memory_service = memory_service
        self.task_service = task_service
        self.settings_service = settings_service
        self.tool_registry = tool_registry
        self.audit_service = audit_service
        self.artifact_service = artifact_service
        self.workspace_root = workspace_root
        self.data_root = data_root
        self.self_repair_service = self_repair_service

    def run(self, source: str = "manual", auto_repair: bool = True) -> dict[str, Any]:
        run_id = self.db.make_id("diag")
        started_at = utc_now()
        checks = [
            self._check_tool_registry(),
            self._check_recent_failures(),
            self._check_code_generation_probe(run_id),
        ]
        findings = self._findings_from_checks(checks)
        created_task_ids = self._create_repair_tasks(findings)
        repair_result = self._run_auto_repair(source) if auto_repair else self._empty_repair_result(source)
        if any(repair.get("status") != "already_handled" for repair in repair_result.get("repairs", [])):
            findings.append(
                {
                    "kind": "self_repair",
                    "severity": "info",
                    "message": repair_result["summary"],
                    "repair_status": repair_result["status"],
                }
            )
        created_memory_ids = [
            *self._create_diagnostic_memory(source, checks, findings),
            *repair_result.get("created_memory_ids", []),
        ]
        status = "completed" if all(check["status"] in {"passed", "attention"} for check in checks) else "completed_with_failures"
        summary = self._summarize(checks, findings, created_task_ids, created_memory_ids, repair_result)
        completed_at = utc_now()

        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO diagnostic_runs (
                    id, source, status, summary, checks_json, findings_json,
                    created_task_ids_json, created_memory_ids_json, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source,
                    status,
                    summary,
                    self.db.dumps(checks),
                    self.db.dumps(findings),
                    self.db.dumps(created_task_ids),
                    self.db.dumps(created_memory_ids),
                    started_at,
                    completed_at,
                ),
            )

        self.audit_service.log(
            action_type="self_diagnostics",
            action_tier=0,
            tool_name="diagnostics.run_self_check",
            outcome=status,
            input_sources=["system", source],
            metadata={
                "run_id": run_id,
                "check_count": len(checks),
                "finding_count": len(findings),
                "created_task_ids": created_task_ids,
                "created_memory_ids": created_memory_ids,
                "repair": repair_result,
            },
        )
        run = self.get_run(run_id)
        run["repair"] = repair_result
        return run

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM diagnostic_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM diagnostic_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._row_to_dict(row)

    def _check_tool_registry(self) -> dict[str, Any]:
        actual_tool_ids = {tool["tool_id"] for tool in self.tool_registry.describe_all()}
        missing = sorted(CORE_TOOL_IDS - actual_tool_ids)
        status = "passed" if not missing else "failed"
        return {
            "name": "tool_registry",
            "status": status,
            "summary": "Core Project Q tools are registered." if not missing else "Core tools are missing.",
            "missing_tool_ids": missing,
            "tool_count": len(actual_tool_ids),
        }

    def _check_recent_failures(self) -> dict[str, Any]:
        recent = self.audit_service.list_recent(limit=80)
        failures = [
            entry
            for entry in recent
            if entry["outcome"] in {"failed", "blocked", "invalid", "runtime_invalid", "completed_with_failures"}
            and entry["action_type"] != "self_diagnostics"
            and not self._is_transient_http_disconnect(entry)
        ]
        failures = failures[:5]
        return {
            "name": "recent_failures",
            "status": "attention" if failures else "passed",
            "summary": "Recent failures need review." if failures else "No recent failure signals found.",
            "failures": [
                {
                    "action_type": entry["action_type"],
                    "tool_name": entry["tool_name"],
                    "outcome": entry["outcome"],
                    "error": entry["error"],
                    "timestamp": entry["timestamp"],
                    "metadata": entry["metadata"],
                }
                for entry in failures
            ],
        }

    @staticmethod
    def _is_transient_http_disconnect(entry: dict[str, Any]) -> bool:
        text = f"{entry.get('error', '')} {entry.get('metadata', '')}".lower()
        return entry.get("tool_name") == "http_server" and (
            "winerror 10053" in text or "connection was aborted" in text
        )

    def _check_code_generation_probe(self, run_id: str) -> dict[str, Any]:
        probe_dir = self.data_root / "diagnostics" / run_id / "notes"
        probe_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = probe_dir / "project_q_code_probe.py"
        artifact_path.write_text(self._code_probe_content(), encoding="utf-8")
        validation = self.artifact_service.validate_written_file(artifact_path)
        runtime = self.artifact_service.smoke_test_written_file(artifact_path)
        status = "passed"
        summary = "Generated code compiled and ran through the smoke test."
        if validation.get("status") not in {"ok", "repaired"}:
            status = "failed"
            summary = "Generated code failed syntax validation."
        elif runtime.get("status") == "runtime_invalid":
            status = "failed"
            summary = "Generated code failed runtime smoke testing."
        return {
            "name": "code_generation_probe",
            "status": status,
            "summary": summary,
            "artifact_path": str(artifact_path),
            "validation": validation,
            "runtime": runtime,
        }

    def _code_probe_content(self) -> str:
        return (
            "from pathlib import Path\n\n"
            "def summarize_workspace(root: Path) -> str:\n"
            "    py_files = sorted(path.name for path in root.rglob('*.py') if path.is_file())\n"
            "    return f'python_files={len(py_files)}'\n\n"
            "if __name__ == '__main__':\n"
            "    print(summarize_workspace(Path.cwd()))\n"
        )

    def _findings_from_checks(self, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for check in checks:
            if check["name"] == "tool_registry" and check["status"] == "failed":
                findings.append(
                    {
                        "kind": "missing_tool",
                        "severity": "high",
                        "message": f"Missing core tools: {', '.join(check['missing_tool_ids'])}",
                        "task_title": "Repair Project Q tool registry",
                    }
                )
            if check["name"] == "recent_failures":
                for failure in check.get("failures", []):
                    findings.append(
                        {
                            "kind": "recent_failure",
                            "severity": "medium",
                            "message": (
                                f"{failure['tool_name']} ended as {failure['outcome']}: "
                                f"{failure['error'] or failure['metadata']}"
                            ),
                            "task_title": f"Investigate Project Q failure: {failure['tool_name']}",
                        }
                    )
            if check["name"] == "code_generation_probe" and check["status"] == "failed":
                findings.append(
                    {
                        "kind": "code_generation_probe_failed",
                        "severity": "high",
                        "message": check["summary"],
                        "task_title": "Repair Project Q code-generation validation loop",
                    }
                )
        return findings

    def _create_repair_tasks(self, findings: list[dict[str, Any]]) -> list[str]:
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
                    priority=1 if finding["severity"] == "high" else 2,
                    source="self_diagnostics",
                    metadata={"finding_kind": finding["kind"]},
                )
            )
            existing_titles.add(title.strip().lower())
            created.append(task["id"])
        return created

    def _run_auto_repair(self, source: str) -> dict[str, Any]:
        if self.self_repair_service is None:
            return self._empty_repair_result(source)
        return self.self_repair_service.run(source=f"diagnostics:{source}")

    @staticmethod
    def _empty_repair_result(source: str) -> dict[str, Any]:
        return {
            "status": "disabled",
            "source": source,
            "summary": "Self repair is not configured for this diagnostics run.",
            "repairs": [],
            "updated_task_ids": [],
            "created_memory_ids": [],
        }

    def _create_diagnostic_memory(
        self,
        source: str,
        checks: list[dict[str, Any]],
        findings: list[dict[str, Any]],
    ) -> list[str]:
        failed_count = len([check for check in checks if check["status"] == "failed"])
        attention_count = len([check for check in checks if check["status"] == "attention"])
        memory = self.memory_service.create(
            MemoryCreate(
                text=(
                    f"Self Diagnostics ({source}): {len(checks)} checks, {failed_count} failed, "
                    f"{attention_count} attention, {len(findings)} findings."
                ),
                kind="episodic",
                source="self_diagnostics",
                confidence=0.82,
                owner_confirmed=False,
                tags=["self_diagnostics", "simulation", "self_debugging"],
                metadata={"failed_count": failed_count, "attention_count": attention_count},
            )
        )
        return [memory["id"]]

    def _summarize(
        self,
        checks: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        created_task_ids: list[str],
        created_memory_ids: list[str],
        repair_result: dict[str, Any],
    ) -> str:
        failed = len([check for check in checks if check["status"] == "failed"])
        attention = len([check for check in checks if check["status"] == "attention"])
        repair_count = len(repair_result.get("repairs", []))
        return (
            f"Self diagnostics ran {len(checks)} checks: {failed} failed, {attention} need attention, "
            f"{len(findings)} finding(s), {len(created_task_ids)} task(s), "
            f"{len(created_memory_ids)} memory item(s), {repair_count} repair(s)."
        )

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source": row["source"],
            "status": row["status"],
            "summary": row["summary"],
            "checks": self.db.loads(row["checks_json"]),
            "findings": self.db.loads(row["findings_json"]),
            "created_task_ids": self.db.loads(row["created_task_ids_json"]),
            "created_memory_ids": self.db.loads(row["created_memory_ids_json"]),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }
