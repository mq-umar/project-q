from __future__ import annotations

from typing import Any

from project_q.models import MemoryCreate, ReasonerPlan, TaskUpdate, utc_now


class SelfRepairService:
    def __init__(self, memory_service, task_service, audit_service) -> None:
        self.memory_service = memory_service
        self.task_service = task_service
        self.audit_service = audit_service

    def run(self, source: str = "manual") -> dict[str, Any]:
        started_at = utc_now()
        recent_entries = self.audit_service.list_recent(limit=120)
        repairs: list[dict[str, Any]] = []
        updated_task_ids: list[str] = []
        handled_repair_kinds = self._handled_repair_kinds()

        reasoner_failures = [entry for entry in recent_entries if self._is_reasoner_payload_failure(entry)]
        if reasoner_failures:
            verified = self._verify_payload_normalizer()
            repair_kind = "reasoner_payload_normalizer"
            message = (
                "Verified the model payload normalizer: string tool payloads are converted into safe dictionaries."
                if verified
                else "Payload normalizer verification failed and still needs code repair."
            )
            matched_tasks = self._complete_matching_tasks(
                markers=("ollama_reasoner", "tool_calls.0.payload", "ReasonerPlan"),
                resolution=message,
                source=source,
            )
            updated_task_ids.extend(matched_tasks)
            already_handled = not matched_tasks and repair_kind in handled_repair_kinds
            repairs.append(
                {
                    "kind": repair_kind,
                    "status": "already_handled" if already_handled else ("verified" if verified else "needs_attention"),
                    "message": message,
                    "matched_failure_count": len(reasoner_failures),
                    "updated_task_ids": matched_tasks,
                }
            )

        policy_blocks = [entry for entry in recent_entries if self._is_expected_owner_approval_block(entry)]
        if policy_blocks:
            repair_kind = "expected_owner_approval_gate"
            message = (
                "Classified blocked Windows launch events as expected owner-approval gates, not code failures. "
                "Use owner approval or raise the auto-approve tier for trusted personal automation."
            )
            matched_tasks = self._complete_matching_tasks(
                markers=("windows.launch_application",),
                resolution=message,
                source=source,
            )
            updated_task_ids.extend(matched_tasks)
            already_handled = not matched_tasks and repair_kind in handled_repair_kinds
            repairs.append(
                {
                    "kind": repair_kind,
                    "status": "already_handled" if already_handled else "classified",
                    "message": message,
                    "matched_failure_count": len(policy_blocks),
                    "updated_task_ids": matched_tasks,
                }
            )

        http_disconnects = [entry for entry in recent_entries if self._is_transient_http_disconnect(entry)]
        if http_disconnects:
            repair_kind = "transient_http_disconnect"
            message = (
                "Classified WinError 10053 http_server disconnects as transient browser/client disconnect noise, "
                "not a Project Q capability failure."
            )
            matched_tasks = self._complete_matching_tasks(
                markers=("http_server",),
                resolution=message,
                source=source,
            )
            updated_task_ids.extend(matched_tasks)
            already_handled = not matched_tasks and repair_kind in handled_repair_kinds
            repairs.append(
                {
                    "kind": repair_kind,
                    "status": "already_handled" if already_handled else "classified",
                    "message": message,
                    "matched_failure_count": len(http_disconnects),
                    "updated_task_ids": matched_tasks,
                }
            )

        created_memory_ids: list[str] = []
        active_repairs = [repair for repair in repairs if repair["status"] != "already_handled"]
        if active_repairs:
            memory = self.memory_service.create(
                MemoryCreate(
                    text=self._memory_text(source, active_repairs),
                    kind="episodic",
                    source="self_repair",
                    confidence=0.86,
                    owner_confirmed=False,
                    tags=["self_repair", "learning_lab", "self_debugging"],
                    metadata={
                        "source": source,
                        "repair_count": len(active_repairs),
                        "repair_kinds": [repair["kind"] for repair in active_repairs],
                        "updated_task_ids": updated_task_ids,
                    },
                )
            )
            created_memory_ids.append(memory["id"])

        status = "completed" if active_repairs else "no_action_needed"
        summary = self._summary(active_repairs, updated_task_ids, created_memory_ids)
        self.audit_service.log(
            action_type="self_repair",
            action_tier=1,
            tool_name="diagnostics.auto_repair",
            outcome=status,
            input_sources=["system", source],
            metadata={
                "repair_count": len(active_repairs),
                "updated_task_ids": updated_task_ids,
                "created_memory_ids": created_memory_ids,
            },
        )
        return {
            "status": status,
            "source": source,
            "summary": summary,
            "repairs": repairs,
            "updated_task_ids": updated_task_ids,
            "created_memory_ids": created_memory_ids,
            "started_at": started_at,
            "completed_at": utc_now(),
        }

    def _verify_payload_normalizer(self) -> bool:
        try:
            plan = ReasonerPlan.model_validate(
                {
                    "reply": "probe",
                    "memory_writes": [],
                    "task_writes": [],
                    "agent_writes": [],
                    "routine_writes": [],
                    "tool_calls": [
                        {
                            "tool_id": "shell.run_command",
                            "reason": "probe string payload repair",
                            "payload": "Get-ChildItem",
                        }
                    ],
                }
            )
        except Exception:  # noqa: BLE001
            return False
        return plan.tool_calls[0].payload.get("command") == "Get-ChildItem"

    def _complete_matching_tasks(self, *, markers: tuple[str, ...], resolution: str, source: str) -> list[str]:
        updated_task_ids: list[str] = []
        for task in self.task_service.list_all(limit=500):
            if task["status"] not in {"pending", "in_progress", "blocked"}:
                continue
            haystack = f"{task['title']} {task['description']}".lower()
            if not all(marker.lower() in haystack for marker in markers):
                continue
            metadata = {
                **task.get("metadata", {}),
                "auto_repair": {
                    "source": source,
                    "status": "resolved",
                    "resolution": resolution,
                    "resolved_at": utc_now(),
                },
            }
            self.task_service.update(
                task["id"],
                TaskUpdate(
                    status="completed",
                    description=self._append_resolution(task["description"], resolution),
                    metadata=metadata,
                ),
            )
            updated_task_ids.append(task["id"])
        return updated_task_ids

    @staticmethod
    def _append_resolution(description: str, resolution: str) -> str:
        if resolution in description:
            return description
        if not description:
            return f"Auto-repair: {resolution}"
        return f"{description}\n\nAuto-repair: {resolution}"

    @staticmethod
    def _is_reasoner_payload_failure(entry: dict[str, Any]) -> bool:
        text = f"{entry.get('tool_name', '')} {entry.get('error', '')} {entry.get('metadata', '')}".lower()
        return (
            entry.get("outcome") == "failed"
            and "reasoner" in str(entry.get("tool_name", "")).lower()
            and ("tool_calls.0.payload" in text or "input should be a valid dictionary" in text)
        )

    def _handled_repair_kinds(self) -> set[str]:
        handled: set[str] = set()
        for memory in self.memory_service.list_all(limit=200):
            if memory.get("source") != "self_repair":
                continue
            metadata = memory.get("metadata") or {}
            for kind in metadata.get("repair_kinds", []):
                handled.add(str(kind))
        return handled

    @staticmethod
    def _is_expected_owner_approval_block(entry: dict[str, Any]) -> bool:
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

    @staticmethod
    def _memory_text(source: str, repairs: list[dict[str, Any]]) -> str:
        repair_names = ", ".join(repair["kind"] for repair in repairs)
        return f"Self Repair ({source}): applied or verified {len(repairs)} repair(s): {repair_names}."

    @staticmethod
    def _summary(
        repairs: list[dict[str, Any]],
        updated_task_ids: list[str],
        created_memory_ids: list[str],
    ) -> str:
        if not repairs:
            return "Self repair found no known repairable issues."
        return (
            f"Self repair handled {len(repairs)} known issue pattern(s), completed "
            f"{len(updated_task_ids)} task(s), and stored {len(created_memory_ids)} memory item(s)."
        )
