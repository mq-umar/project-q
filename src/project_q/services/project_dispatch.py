from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from project_q.models import utc_now
from project_q.storage import Database


class ProjectDispatchService:
    def __init__(self, db: Database, workspace_root: Path) -> None:
        self.db = db
        self.workspace_root = workspace_root.resolve()

    def plan(self, instruction: str, target_dir: str = "") -> dict[str, Any]:
        clean_instruction = instruction.strip()
        if not clean_instruction:
            raise ValueError("project dispatch requires an instruction")

        task_type = self.classify(clean_instruction)
        project_name = self.project_name(clean_instruction, task_type)
        project_path = str(self._target_path(target_dir, task_type, project_name))
        acceptance_criteria = self.acceptance_criteria(task_type)
        summary = self.summary_for(task_type, project_name)
        refined_prompt = self.refined_prompt(clean_instruction, task_type, project_name, acceptance_criteria)
        return self._insert(
            task_type=task_type,
            project_name=project_name,
            project_path=project_path,
            original_prompt=clean_instruction,
            refined_prompt=refined_prompt,
            status="planned",
            summary=summary,
            artifacts=[],
            acceptance_criteria=acceptance_criteria,
            completed_at=None,
        )

    def record_completed(
        self,
        *,
        task_type: str,
        project_name: str,
        original_prompt: str,
        project_path: str,
        artifacts: list[dict[str, Any]],
        acceptance_criteria: list[str] | None = None,
        summary: str = "",
    ) -> dict[str, Any]:
        criteria = acceptance_criteria or self.acceptance_criteria(task_type)
        return self._insert(
            task_type=task_type,
            project_name=project_name,
            project_path=project_path,
            original_prompt=original_prompt,
            refined_prompt=self.refined_prompt(original_prompt, task_type, project_name, criteria),
            status="completed",
            summary=summary or self.summary_for(task_type, project_name),
            artifacts=artifacts,
            acceptance_criteria=criteria,
            completed_at=utc_now(),
        )

    def list_all(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM project_dispatches
                ORDER BY
                    CASE status
                        WHEN 'running' THEN 0
                        WHEN 'planned' THEN 1
                        WHEN 'blocked' THEN 2
                        ELSE 3
                    END,
                    updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get(self, dispatch_id: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM project_dispatches WHERE id = ?", (dispatch_id,)).fetchone()
        if row is None:
            raise KeyError(dispatch_id)
        return self._row_to_dict(row)

    def classify(self, instruction: str) -> str:
        lowered = instruction.lower()
        if any(marker in lowered for marker in ("landing page", "website", "web site", "homepage", "static site")):
            return "landing_page"
        if any(marker in lowered for marker in ("api", "endpoint", "backend", "server")):
            return "api"
        if any(marker in lowered for marker in ("bug", "fix", "error", "debug", "troubleshoot")):
            return "bug_fix"
        if "refactor" in lowered or "clean up" in lowered or "cleanup" in lowered:
            return "refactor"
        if any(marker in lowered for marker in ("app", "dashboard", "portal", "crm", "saas", "login")):
            return "fullstack_app"
        if any(marker in lowered for marker in ("script", "python", "powershell", "automation", "tool")):
            return "script_tool"
        if any(marker in lowered for marker in ("research", "compare", "find out", "investigate")):
            return "research_report"
        return "general_project"

    def project_name(self, instruction: str, task_type: str) -> str:
        clean = re.sub(r"\b(can you|please|for me|me|a|an|the|with|and|that|this)\b", " ", instruction, flags=re.I)
        tokens = re.findall(r"[A-Za-z0-9]+", clean)
        stop = {
            "build",
            "create",
            "make",
            "generate",
            "scaffold",
            "code",
            "write",
            "prepare",
            "app",
            "application",
            "website",
            "site",
            "project",
        }
        kept = [token for token in tokens if token.lower() not in stop]
        if not kept:
            return self._default_name(task_type)
        title_tokens = [self._title_token(token) for token in kept[:5]]
        name = " ".join(title_tokens).strip()
        if task_type == "fullstack_app" and not any(word in name.lower() for word in ("app", "dashboard", "portal")):
            name += " App"
        if task_type == "script_tool" and "script" not in name.lower():
            name += " Script"
        return name[:80]

    def acceptance_criteria(self, task_type: str) -> list[str]:
        templates = {
            "landing_page": [
                "Responsive layout works on desktop and mobile.",
                "Accessible semantic HTML with clear sections and navigation.",
                "SEO-friendly title, description, and readable copy.",
                "Includes README instructions for opening and editing the site.",
            ],
            "fullstack_app": [
                "Responsive, user-friendly interface with clear primary workflows.",
                "Data model and persistence plan are documented before implementation.",
                "Authentication and permissions are explicitly handled when needed.",
                "Core happy path and edge cases have tests or smoke checks.",
                "README explains setup, run, and verification steps.",
            ],
            "api": [
                "Endpoints, payloads, validation rules, and errors are documented.",
                "Input validation rejects malformed or unsafe requests.",
                "Core endpoints have automated tests or smoke checks.",
                "README explains local run and API usage.",
            ],
            "script_tool": [
                "Uses pathlib or safe platform-neutral paths instead of raw Windows backslashes.",
                "Accepts clear inputs or sensible defaults.",
                "Handles missing files, empty results, and permission errors gracefully.",
                "Includes a smoke-testable entry point and usage notes.",
            ],
            "bug_fix": [
                "Root cause is documented before changing behavior.",
                "A regression test or reproducible verification is added.",
                "The fix is scoped to the failing behavior.",
            ],
            "refactor": [
                "Behavior is preserved with tests or smoke checks.",
                "Responsibilities become clearer without broad unrelated rewrites.",
                "Risky changes are isolated and auditable.",
            ],
            "research_report": [
                "Sources and assumptions are separated from conclusions.",
                "Findings are summarized into actionable next steps.",
                "Open questions and confidence level are recorded.",
            ],
            "general_project": [
                "Goal, deliverables, risks, and next actions are clarified.",
                "Work is tracked as a dispatch instead of disappearing in chat.",
                "Verification steps are defined before completion.",
            ],
        }
        return templates.get(task_type, templates["general_project"])

    def refined_prompt(
        self,
        instruction: str,
        task_type: str,
        project_name: str,
        acceptance_criteria: list[str],
    ) -> str:
        criteria = "\n".join(f"- {item}" for item in acceptance_criteria)
        return (
            f"Project: {project_name}\n"
            f"Type: {task_type}\n"
            f"Owner request: {instruction}\n\n"
            "Build it as a practical, audited Project Q deliverable.\n"
            "Acceptance criteria:\n"
            f"{criteria}"
        )

    def summary_for(self, task_type: str, project_name: str) -> str:
        labels = {
            "landing_page": "Website build",
            "fullstack_app": "Application build",
            "api": "API build",
            "script_tool": "Script/tool build",
            "bug_fix": "Bug-fix dispatch",
            "refactor": "Refactor dispatch",
            "research_report": "Research dispatch",
            "general_project": "General project dispatch",
        }
        return f"{labels.get(task_type, 'Project dispatch')} prepared for {project_name}."

    def _insert(
        self,
        *,
        task_type: str,
        project_name: str,
        project_path: str,
        original_prompt: str,
        refined_prompt: str,
        status: str,
        summary: str,
        artifacts: list[dict[str, Any]],
        acceptance_criteria: list[str],
        completed_at: str | None,
    ) -> dict[str, Any]:
        dispatch_id = self.db.make_id("dispatch")
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO project_dispatches (
                    id, task_type, project_name, project_path, original_prompt,
                    refined_prompt, status, summary, artifacts_json,
                    acceptance_criteria_json, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dispatch_id,
                    task_type,
                    project_name,
                    project_path,
                    original_prompt,
                    refined_prompt,
                    status,
                    summary,
                    self.db.dumps(artifacts),
                    self.db.dumps(acceptance_criteria),
                    now,
                    now,
                    completed_at,
                ),
            )
        return self.get(dispatch_id)

    def _target_path(self, raw_target: str, task_type: str, project_name: str) -> Path:
        if raw_target.strip():
            candidate = Path(raw_target.strip())
        else:
            folder = "generated_sites" if task_type == "landing_page" else "generated_projects"
            candidate = Path(folder) / self._slug(project_name)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace_root / candidate).resolve()
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise PermissionError("Project dispatch paths must stay inside the workspace.")
        return resolved

    @staticmethod
    def _default_name(task_type: str) -> str:
        return {
            "landing_page": "New Website",
            "fullstack_app": "New App",
            "api": "New API",
            "script_tool": "New Script",
            "bug_fix": "Bug Fix",
            "refactor": "Refactor",
            "research_report": "Research Report",
        }.get(task_type, "Project Q Build")

    @staticmethod
    def _title_token(token: str) -> str:
        upper = token.upper()
        if upper in {"AI", "API", "CRM", "SEO", "IT", "UI", "UX", "QA"}:
            return upper
        return token.capitalize()

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug or "project"

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_type": row["task_type"],
            "project_name": row["project_name"],
            "project_path": row["project_path"],
            "original_prompt": row["original_prompt"],
            "refined_prompt": row["refined_prompt"],
            "status": row["status"],
            "summary": row["summary"],
            "artifacts": self.db.loads(row["artifacts_json"]),
            "acceptance_criteria": self.db.loads(row["acceptance_criteria_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }
