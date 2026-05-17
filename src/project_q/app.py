from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from project_q.config import AppConfig
from project_q.services.agents import AgentService
from project_q.services.agent_runner import AgentRunnerService
from project_q.services.artifacts import ArtifactValidationService
from project_q.services.audit import AuditService
from project_q.services.code_repair import CodeRepairService
from project_q.services.conversation import ConversationService
from project_q.services.context import ContextService
from project_q.services.diagnostics import SelfDiagnosticsService
from project_q.services.knowledge import KnowledgeWorkService
from project_q.services.learning import LearningLabService
from project_q.services.memory import MemoryService
from project_q.services.plan_executor import PlanExecutorService
from project_q.services.project_dispatch import ProjectDispatchService
from project_q.services.policy import PolicyService
from project_q.services.reasoner import ReasonerService
from project_q.services.routine_runner import RoutineRunnerService
from project_q.services.routines import RoutineService
from project_q.services.self_repair import SelfRepairService
from project_q.services.settings import SettingsService
from project_q.services.tasks import TaskService
from project_q.services.training import TrainingService
from project_q.services.vault import VaultService
from project_q.storage import Database
from project_q.tools.registry import ToolRegistry
from project_q.tools.diagnostics import DiagnosticsAutoRepairTool, DiagnosticsRunSelfCheckTool
from project_q.tools.codegen import ProjectGeneratorTool, WebsiteGeneratorTool
from project_q.tools.knowledge import KnowledgeAnswerTool
from project_q.tools.project import ProjectPlanBuildTool
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
    artifacts: ArtifactValidationService
    code_repair: CodeRepairService
    knowledge: KnowledgeWorkService
    memory: MemoryService
    tasks: TaskService
    agents: AgentService
    routines: RoutineService
    settings: SettingsService
    policy: PolicyService
    vault: VaultService
    context: ContextService
    reasoner: ReasonerService
    executor: PlanExecutorService
    agent_runner: AgentRunnerService
    routine_runner: RoutineRunnerService
    conversations: ConversationService
    diagnostics: SelfDiagnosticsService
    self_repair: SelfRepairService
    learning: LearningLabService
    training: TrainingService
    dispatches: ProjectDispatchService
    tools: ToolRegistry

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


def create_application(config: AppConfig | None = None) -> ProjectQApplication:
    resolved_config = config or AppConfig.discover()
    resolved_config.data_root.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    build_id = "build-" + started_at.replace("-", "").replace(":", "")
    runtime_file = resolved_config.data_root / "runtime.json"

    db = Database(resolved_config.db_path)
    audit = AuditService(db)
    artifacts = ArtifactValidationService()
    memory = MemoryService(db)
    tasks = TaskService(db)
    agents = AgentService(db)
    routines = RoutineService(db)
    settings = SettingsService(db)
    policy = PolicyService(settings)
    vault = VaultService(db)
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
    )
    tools.register(DiagnosticsRunSelfCheckTool(diagnostics))
    tools.register(DiagnosticsAutoRepairTool(self_repair))
    training = TrainingService(db, memory, tasks, audit, resolved_config.data_root)
    tools.register(TrainingExportDatasetTool(training))
    tools.register(TrainingCapabilityPlanTool(training))
    tools.register(TrainingPrepareLoraJobTool(training))
    tools.register(KnowledgeAnswerTool(knowledge))
    context = ContextService(db, memory, tasks, agents, routines, settings, tools, resolved_config.workspace_root)
    reasoner = ReasonerService(settings, vault, audit)
    executor = PlanExecutorService(memory, tasks, agents, routines, tools, policy, audit, artifacts, code_repair)
    agent_runner = AgentRunnerService(db, agents, context, reasoner, executor, audit)
    routine_runner = RoutineRunnerService(db, routines, agent_runner, tools, policy, audit)
    conversations = ConversationService(db, memory, tasks, agents, context, reasoner, executor)
    learning = LearningLabService(db, memory, tasks, agents, routines, settings, tools, audit, diagnostics)

    return ProjectQApplication(
        config=resolved_config,
        started_at=started_at,
        build_id=build_id,
        process_id=os.getpid(),
        runtime_file=runtime_file,
        db=db,
        audit=audit,
        artifacts=artifacts,
        code_repair=code_repair,
        knowledge=knowledge,
        memory=memory,
        tasks=tasks,
        agents=agents,
        routines=routines,
        settings=settings,
        policy=policy,
        vault=vault,
        context=context,
        reasoner=reasoner,
        executor=executor,
        agent_runner=agent_runner,
        routine_runner=routine_runner,
        conversations=conversations,
        diagnostics=diagnostics,
        self_repair=self_repair,
        learning=learning,
        training=training,
        dispatches=dispatches,
        tools=tools,
    )
