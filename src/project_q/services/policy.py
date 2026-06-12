from __future__ import annotations

from dataclasses import dataclass

# Tools that require internet access — blocked when network_policy == "local_only"
_INTERNET_TOOLS: frozenset[str] = frozenset({
    "browser.inspect_page",
    "browser.run_actions",
    "browser.complete_goal",
    "communications.email_draft",
    "knowledge.answer",
})


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str


class PolicyService:
    def __init__(self, settings_service) -> None:
        self.settings_service = settings_service

    def authorize_tool(
        self,
        *,
        tier: int,
        owner_approved: bool,
        trusted_routine: bool = False,
        input_sources: list[str] | None = None,
        tool_id: str | None = None,
    ) -> PolicyDecision:
        settings = self.settings_service.get_all()

        # Network policy enforcement
        if tool_id and tool_id in _INTERNET_TOOLS:
            network_policy = settings.get("network_policy", "selected_services")
            if network_policy == "local_only":
                return PolicyDecision(
                    False,
                    f"network_policy=local_only blocks internet tool: {tool_id}",
                )

        if settings.get("kill_switch_active", False):
            reason = settings.get("kill_switch_reason", "") or "Emergency stop activated"
            return PolicyDecision(False, f"kill switch active: {reason}")
        sources = self._normalized_sources(input_sources)
        if tier > 0 and self._has_external_source(sources) and not self._has_authoritative_source(sources):
            return PolicyDecision(False, "external content cannot authorize tool execution")
        if owner_approved and not self._has_owner_authority(sources):
            return PolicyDecision(False, "external content cannot authorize owner approval")
        auto_approve_tier = int(settings.get("auto_approve_tier", 1))
        if tier <= auto_approve_tier:
            return PolicyDecision(True, "allowed by owner auto-approval tier")
        if trusted_routine and tier <= 2:
            return PolicyDecision(True, "allowed by trusted routine policy")
        if owner_approved:
            return PolicyDecision(True, "allowed by explicit owner approval")
        return PolicyDecision(False, f"tier {tier} requires owner approval")

    @staticmethod
    def _normalized_sources(input_sources: list[str] | None) -> set[str]:
        if input_sources is None:
            return {"owner"}
        return {str(source).strip().lower() for source in input_sources if str(source).strip()}

    @staticmethod
    def _has_external_source(sources: set[str]) -> bool:
        external_markers = {
            "external", "external_content", "zone_3_external",
            "web", "web_page", "email", "browser", "document", "pdf",
        }
        return any(source in external_markers or source.startswith("external_") for source in sources)

    @staticmethod
    def _has_authoritative_source(sources: set[str]) -> bool:
        authority_markers = {
            "owner", "owner_session", "dashboard", "system",
            "routine", "routines", "trusted_routine",
        }
        return bool(sources & authority_markers)

    @staticmethod
    def _has_owner_authority(sources: set[str]) -> bool:
        owner_markers = {"owner", "owner_session", "dashboard", "trusted_routine"}
        return bool(sources & owner_markers)
