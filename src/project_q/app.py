from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project_q.config import AppConfig
from project_q.services.agents import AgentService
from project_q.services.agent_runner import AgentRunnerService
from project_q.services.artifacts import ArtifactValidationService
from project_q.services.approvals import ApprovalService
from project_q.services.audit import AuditService
from project_q.services.code_repair import CodeRepairService
from project_q.services.conversation import ConversationService
from project_q.services.companion import CompanionService
from project_q.services.companion_auth import CompanionAuthService
from project_q.services.companion_protocol import CompanionProtocolService
from project_q.services.context import ContextService
from project_q.services.control import ControlService
from project_q.services.diagnostics import SelfDiagnosticsService
from project_q.services.knowledge import KnowledgeWorkService
from project_q.services.learning import LearningLabService
from project_q.services.memory import MemoryService
from project_q.services.owner_auth import OwnerAuthService
from project_q.services.plan_executor import PlanExecutorService
from project_q.services.project_dispatch import ProjectDispatchService
from project_q.services.policy import PolicyService
from project_q.services.reasoner import ReasonerService
from project_q.services.relay import (
    CompanionRelayBridgeService,
    CompanionRelayCommandDispatcher,
    RelayHTTPClient,
    RelayProvisioningService,
)
from project_q.services.research import WebResearchService
from project_q.services.routine_runner import RoutineRunnerService
from project_q.services.routines import RoutineService
from project_q.services.self_repair import SelfRepairService
from project_q.services.settings import SettingsService
from project_q.services.sync import SyncEventService
from project_q.services.tasks import TaskService
from project_q.services.training import TrainingService
from project_q.services.trust import TrustBoundaryService
from project_q.services.vault import VaultService
from project_q.services.voice import VoiceService
from project_q.storage import Database
from project_q.tools.registry import ToolRegistry
from project_q.tools.diagnostics import DiagnosticsAutoRepairTool, DiagnosticsRunSelfCheckTool
from project_q.tools.codegen import ProjectGeneratorTool, WebsiteGeneratorTool
from project_q.tools.knowledge import KnowledgeAnswerTool
from project_q.tools.project import ProjectPlanBuildTool
from project_q.tools.research import ResearchWebTool
from project_q.tools.security import SecurityScanExternalContentTool
from project_q.tools.training import TrainingCapabilityPlanTool, TrainingExportDatasetTool, TrainingPrepareLoraJobTool
from project_q.models import utc_now


@dataclass(slots=True)
class ProjectQApplication:
    config: AppConfig
    started_at: str
    build_id: str
    process_id: int
    runtime_file: Path
    db: Database
    audit: AuditService
    owner_auth: OwnerAuthService
    artifacts: ArtifactValidationService
    code_repair: CodeRepairService
    knowledge: KnowledgeWorkService
    research: WebResearchService
    memory: MemoryService
    tasks: TaskService
    agents: AgentService
    routines: RoutineService
    settings: SettingsService
    policy: PolicyService
    control: ControlService
    companion: CompanionService
    companion_protocol: CompanionProtocolService
    companion_auth: CompanionAuthService
    sync: SyncEventService
    approvals: ApprovalService
    vault: VaultService
    trust: TrustBoundaryService
    context: ContextService
    reasoner: ReasonerService
    executor: PlanExecutorService
    agent_runner: AgentRunnerService
    routine_runner: RoutineRunnerService
    conversations: ConversationService
    voice: VoiceService
    diagnostics: SelfDiagnosticsService
    self_repair: SelfRepairService
    learning: LearningLabService
    training: TrainingService
    dispatches: ProjectDispatchService
    tools: ToolRegistry
    scheduler: Any
    relay_provisioner: Any
    relay_bridge: Any

    def runtime_metadata(self) -> dict[str, str | int]:
        return {
            "build_id": self.build_id,
            "started_at": self.started_at,
            "pid": self.process_id,
            "runtime_file": str(self.runtime_file),
        }

    def write_runtime_metadata(self) -> None:
        self.runtime_file.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_file.write_text(json.dumps(self.runtime_metadata(), indent=2), encoding="utf-8")

    def clear_runtime_metadata(self) -> None:
        if not self.runtime_file.exists():
            return
        try:
            current = json.loads(self.runtime_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = {}
        if int(current.get("pid", 0) or 0) == self.process_id:
            self.runtime_file.unlink(missing_ok=True)

    def ensure_relay_bridge(self, windows_token: str) -> None:
        if self.relay_provisioner is None:
            return
        if self.relay_bridge is not None:
            return
        dispatcher = CompanionRelayCommandDispatcher(self)
        self.relay_bridge = CompanionRelayBridgeService(
            db=self.db,
            protocol_service=self.companion_protocol,
            relay_client=self.relay_provisioner.relay_client,
            windows_token=windows_token,
            windows_device_id=self.companion_protocol.windows_device_id(),
            command_handler=dispatcher.handle,
        )
        self.relay_bridge.start()

    def shutdown_services(self) -> None:
        if self.relay_bridge is not None:
            self.relay_bridge.stop()
        if self.scheduler is not None:
            stop = getattr(self.scheduler, "stop", None)
            if callable(stop):
                stop()


def create_application(config: AppConfig | None = None) -> ProjectQApplication:
    resolved_config = config or AppConfig.discover()
    resolved_config.data_root.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    build_id = "build-" + started_at.replace("-", "").replace(":", "")
    runtime_file = resolved_config.data_root / "runtime.json"

    db = Database(resolved_config.db_path)
    sync = SyncEventService(db)
    audit = AuditService(db, sync)
    owner_auth = OwnerAuthService(db, audit)
    artifacts = ArtifactValidationService()
    settings = SettingsService(db)
    policy = PolicyService(settings)
    memory = MemoryService(db, settings, sync)
    tasks = TaskService(db, sync)
    agents = AgentService(db, sync)
    routines = RoutineService(db, sync)
    control = ControlService(settings, audit, sync_service=sync)
    vault = VaultService(db)
    companion_protocol = CompanionProtocolService(db, vault, audit)
    companion_auth = CompanionAuthService(db, companion_protocol)
    companion = CompanionService(
        db,
        vault,
        memory,
        audit,
        protocol_service=companion_protocol,
    )
    trust = TrustBoundaryService(audit)
    code_repair = CodeRepairService(settings, audit)
    knowledge = KnowledgeWorkService(settings, audit)
    dispatches = ProjectDispatchService(db, resolved_config.workspace_root)
    tools = ToolRegistry(resolved_config.workspace_root, resolved_config.data_root, settings)
    tools.register(WebsiteGeneratorTool(resolved_config.workspace_root, dispatch_service=dispatches))
    tools.register(ProjectGeneratorTool(resolved_config.workspace_root, dispatch_service=dispatches))
    tools.register(ProjectPlanBuildTool(dispatches))
    self_repair = SelfRepairService(memory, tasks, audit)
    diagnostics = SelfDiagnosticsService(
        db,
        memory,
        tasks,
        settings,
        tools,
        audit,
        artifacts,
        resolved_config.workspace_root,
        resolved_config.data_root,
        self_repair,
        trust,
        policy,
    )
    tools.register(DiagnosticsRunSelfCheckTool(diagnostics))
    tools.register(DiagnosticsAutoRepairTool(self_repair))
    training = TrainingService(db, memory, tasks, audit, resolved_config.data_root)
    tools.register(TrainingExportDatasetTool(training))
    tools.register(TrainingCapabilityPlanTool(training))
    tools.register(TrainingPrepareLoraJobTool(training))
    tools.register(KnowledgeAnswerTool(knowledge))
    tools.register(SecurityScanExternalContentTool(trust))
    research = WebResearchService(tools.get("browser.inspect_page"), trust, audit)
    tools.register(ResearchWebTool(research))
    approvals = ApprovalService(db, vault, tools, policy, audit, sync)
    context = ContextService(db, memory, tasks, agents, routines, settings, tools, resolved_config.workspace_root)
    reasoner = ReasonerService(settings, vault, audit)
    executor = PlanExecutorService(
        memory,
        tasks,
        agents,
        routines,
        tools,
        policy,
        audit,
        artifacts,
        code_repair,
        approvals,
    )
    agent_runner = AgentRunnerService(db, agents, context, reasoner, executor, audit)
    routine_runner = RoutineRunnerService(db, routines, agent_runner, tools, policy, audit)
    scheduler = None
    if settings.get_all().get("scheduler_enabled", True):
        try:
            from project_q.services.scheduler import RoutineSchedulerService
            scheduler = RoutineSchedulerService(routines, routine_runner)
            scheduler.start()
        except Exception:
            scheduler = None
    conversations = ConversationService(
        db,
        memory,
        tasks,
        agents,
        context,
        reasoner,
        executor,
        sync,
    )
    voice = VoiceService(settings, conversations, audit)
    learning = LearningLabService(db, memory, tasks, agents, routines, settings, tools, audit, diagnostics)
    control.attach_learning(learning)
    relay_provisioner = None
    if resolved_config.relay_base_url and resolved_config.relay_bootstrap_token:
        relay_client = RelayHTTPClient(resolved_config.relay_base_url)
        relay_provisioner = RelayProvisioningService(
            relay_base_url=resolved_config.relay_base_url,
            bootstrap_token=resolved_config.relay_bootstrap_token,
            relay_client=relay_client,
            vault_service=vault,
        )

    return ProjectQApplication(
        config=resolved_config,
        started_at=started_at,
        build_id=build_id,
        process_id=os.getpid(),
        runtime_file=runtime_file,
        db=db,
        audit=audit,
        owner_auth=owner_auth,
        artifacts=artifacts,
        code_repair=code_repair,
        knowledge=knowledge,
        research=research,
        memory=memory,
        tasks=tasks,
        agents=agents,
        routines=routines,
        settings=settings,
        policy=policy,
        control=control,
        companion=companion,
        companion_protocol=companion_protocol,
        companion_auth=companion_auth,
        sync=sync,
        approvals=approvals,
        vault=vault,
        trust=trust,
        context=context,
        reasoner=reasoner,
        executor=executor,
        agent_runner=agent_runner,
        routine_runner=routine_runner,
        conversations=conversations,
        voice=voice,
        diagnostics=diagnostics,
        self_repair=self_repair,
        learning=learning,
        training=training,
        dispatches=dispatches,
        tools=tools,
        scheduler=scheduler,
        relay_provisioner=relay_provisioner,
        relay_bridge=None,
    )
