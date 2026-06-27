from __future__ import annotations

from dataclasses import dataclass

# Tools that require internet access — blocked when network_policy == "local_only"
_INTERNET_TOOLS: frozenset[str] = frozenset({
    "browser.inspect_page",
    "browser.run_actions",
    "browser.complete_goal",
    "communications.email_draft",
    "knowledge.answer",
    "github.list_repos",
    "github.list_issues",
    "github.create_issue",
    "slack.list_channels",
    "slack.post_message",
    "notion.search",
    "notion.create_page",
    "todoist.list_tasks",
    "todoist.create_task",
    "linear.list_issues",
    "linear.create_issue",
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
        # Tier 3 (destructive / high-risk) is NEVER auto-approved or trusted-routine
        # approved — it always requires an explicit, owner-authoritative approval.
        if tier >= 3:
            if owner_approved:
                return PolicyDecision(True, "allowed by explicit owner approval")
            # Keep this exact reason string: the approval-request flow in
            # PlanExecutorService matches on `tier {tier} requires owner approval`.
            return PolicyDecision(False, f"tier {tier} requires owner approval")
        # Effective auto-approval bar folds the §12.1 Approval Policy and Aggression
        # Level profiles on top of auto_approve_tier (always clamped to <= 2).
        approval_policy = str(settings.get("approval_policy", "ask_on_risky"))
        auto_approve_tier = self._effective_auto_approve_tier(settings, approval_policy)
        if tier <= auto_approve_tier:
            return PolicyDecision(True, "allowed by owner auto-approval tier")
        if trusted_routine and tier <= 2 and approval_policy != "always_ask":
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

    @staticmethod
    def _effective_auto_approve_tier(settings, approval_policy: str) -> int:
        """Fold §12.1 Approval Policy + Aggression Level into the auto-approval bar.

        Defaults (operator + ask_on_risky) leave the configured auto_approve_tier
        unchanged so existing behavior is preserved.
        """
        base = min(int(settings.get("auto_approve_tier", 1)), 2)
        if approval_policy in {"always_ask", "trusted_routines_only"}:
            base = min(base, 0)
        aggression = str(settings.get("aggression_level", "operator"))
        if aggression == "conservative":
            base = min(base, 0)
        elif aggression == "balanced":
            base = min(base, 1)
        elif aggression == "maximum":
            base = max(base, 2)
        return max(0, min(base, 2))
