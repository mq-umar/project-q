from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str


class PolicyService:
    def __init__(self, settings_service) -> None:
        self.settings_service = settings_service

    def authorize_tool(self, *, tier: int, owner_approved: bool, trusted_routine: bool = False) -> PolicyDecision:
        settings = self.settings_service.get_all()
        auto_approve_tier = int(settings.get("auto_approve_tier", 1))
        if tier <= auto_approve_tier:
            return PolicyDecision(True, "allowed by owner auto-approval tier")
        if trusted_routine and tier <= 2:
            return PolicyDecision(True, "allowed by trusted routine policy")
        if owner_approved:
            return PolicyDecision(True, "allowed by explicit owner approval")
        return PolicyDecision(False, f"tier {tier} requires owner approval")
