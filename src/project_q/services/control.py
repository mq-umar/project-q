from __future__ import annotations

from typing import Any

from project_q.models import SettingsUpdate, utc_now


class ControlService:
    def __init__(
        self,
        settings_service,
        audit_service,
        learning_service=None,
        sync_service=None,
    ) -> None:
        self.settings_service = settings_service
        self.audit_service = audit_service
        self.learning_service = learning_service
        self.sync_service = sync_service

    def attach_learning(self, learning_service) -> None:
        self.learning_service = learning_service

    def status(self) -> dict[str, Any]:
        settings = self.settings_service.get_all()
        return {
            "active": bool(settings.get("kill_switch_active", False)),
            "reason": settings.get("kill_switch_reason", "") or "",
            "activated_at": settings.get("kill_switch_activated_at", "") or "",
            "source": settings.get("kill_switch_source", "") or "",
        }

    def activate(self, *, reason: str = "", source: str = "dashboard") -> dict[str, Any]:
        clean_reason = str(reason or "").strip()[:500] or "Emergency stop activated"
        clean_source = str(source or "dashboard").strip()[:100] or "dashboard"
        activated_at = utc_now()

        if self.learning_service is not None:
            self.learning_service.stop()

        self.settings_service.update(
            SettingsUpdate(
                kill_switch_active=True,
                kill_switch_reason=clean_reason,
                kill_switch_activated_at=activated_at,
                kill_switch_source=clean_source,
            )
        )
        self.audit_service.log(
            action_type="kill_switch_activate",
            action_tier=3,
            tool_name="control_plane",
            outcome="completed",
            approved_by_owner=True,
            input_sources=[clean_source],
            metadata={
                "reason": clean_reason,
                "source": clean_source,
                "activated_at": activated_at,
            },
        )
        status = self.status()
        if self.sync_service is not None:
            self.sync_service.append(
                resource_type="control",
                resource_id="kill_switch",
                operation="activated",
                payload=status,
            )
        return status

    def resume(self, *, reason: str = "", source: str = "dashboard") -> dict[str, Any]:
        clean_reason = str(reason or "").strip()[:500] or "Owner resumed Project Q"
        clean_source = str(source or "dashboard").strip()[:100] or "dashboard"
        resumed_at = utc_now()

        self.settings_service.update(
            SettingsUpdate(
                kill_switch_active=False,
                kill_switch_reason="",
                kill_switch_activated_at="",
                kill_switch_source="",
            )
        )
        self.audit_service.log(
            action_type="kill_switch_resume",
            action_tier=3,
            tool_name="control_plane",
            outcome="completed",
            approved_by_owner=True,
            input_sources=[clean_source],
            metadata={
                "reason": clean_reason,
                "source": clean_source,
                "resumed_at": resumed_at,
            },
        )
        status = self.status()
        if self.sync_service is not None:
            self.sync_service.append(
                resource_type="control",
                resource_id="kill_switch",
                operation="resumed",
                payload=status,
            )
        return status
