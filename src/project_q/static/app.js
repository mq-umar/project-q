const state = {
  status: null,
  conversations: [],
  tasks: [],
  memories: [],
  agents: [],
  routines: [],
  dispatches: [],
  audit: [],
  settings: {},
  tools: [],
  secrets: [],
  providerModels: [],
  providerModelError: "",
  learningStatus: null,
  learningRuns: [],
  diagnosticsRuns: [],
};

const els = {
  statusPill: document.getElementById("statusPill"),
  refreshButton: document.getElementById("refreshButton"),
  conversationFeed: document.getElementById("conversationFeed"),
  chatForm: document.getElementById("chatForm"),
  chatInput: document.getElementById("chatInput"),
  chatApproval: document.getElementById("chatApproval"),
  taskForm: document.getElementById("taskForm"),
  taskTitle: document.getElementById("taskTitle"),
  taskList: document.getElementById("taskList"),
  memoryForm: document.getElementById("memoryForm"),
  memoryText: document.getElementById("memoryText"),
  memoryList: document.getElementById("memoryList"),
  agentForm: document.getElementById("agentForm"),
  agentName: document.getElementById("agentName"),
  agentGoal: document.getElementById("agentGoal"),
  agentList: document.getElementById("agentList"),
  routineForm: document.getElementById("routineForm"),
  routineName: document.getElementById("routineName"),
  routineGoal: document.getElementById("routineGoal"),
  routineSteps: document.getElementById("routineSteps"),
  routineTrusted: document.getElementById("routineTrusted"),
  routineList: document.getElementById("routineList"),
  dispatchList: document.getElementById("dispatchList"),
  auditList: document.getElementById("auditList"),
  settingsForm: document.getElementById("settingsForm"),
  ownerName: document.getElementById("ownerName"),
  aggressionLevel: document.getElementById("aggressionLevel"),
  autoApproveTier: document.getElementById("autoApproveTier"),
  notificationsEnabled: document.getElementById("notificationsEnabled"),
  providerEnabled: document.getElementById("providerEnabled"),
  providerType: document.getElementById("providerType"),
  modelName: document.getElementById("modelName"),
  ollamaModelRoutingEnabled: document.getElementById("ollamaModelRoutingEnabled"),
  ollamaGeneralModel: document.getElementById("ollamaGeneralModel"),
  ollamaCodingModel: document.getElementById("ollamaCodingModel"),
  ollamaReasoningModel: document.getElementById("ollamaReasoningModel"),
  ollamaFastModel: document.getElementById("ollamaFastModel"),
  modelBaseUrl: document.getElementById("modelBaseUrl"),
  modelSecretName: document.getElementById("modelSecretName"),
  providerTimeoutSeconds: document.getElementById("providerTimeoutSeconds"),
  browserHeadless: document.getElementById("browserHeadless"),
  browserChannel: document.getElementById("browserChannel"),
  browserExecutablePath: document.getElementById("browserExecutablePath"),
  fileAccessRoots: document.getElementById("fileAccessRoots"),
  learningIntervalSeconds: document.getElementById("learningIntervalSeconds"),
  learningMaxCyclesPerStart: document.getElementById("learningMaxCyclesPerStart"),
  learningStatusText: document.getElementById("learningStatusText"),
  learningRunList: document.getElementById("learningRunList"),
  learningRunOnceButton: document.getElementById("learningRunOnceButton"),
  learningStartButton: document.getElementById("learningStartButton"),
  learningStopButton: document.getElementById("learningStopButton"),
  diagnosticsRunButton: document.getElementById("diagnosticsRunButton"),
  diagnosticsAutoRepairButton: document.getElementById("diagnosticsAutoRepairButton"),
  diagnosticsRunList: document.getElementById("diagnosticsRunList"),
  trainingExportButton: document.getElementById("trainingExportButton"),
  trainingPrepareLoraButton: document.getElementById("trainingPrepareLoraButton"),
  refreshModelsButton: document.getElementById("refreshModelsButton"),
  providerModelStatus: document.getElementById("providerModelStatus"),
  providerModelList: document.getElementById("providerModelList"),
  toolForm: document.getElementById("toolForm"),
  toolSelect: document.getElementById("toolSelect"),
  toolPayload: document.getElementById("toolPayload"),
  toolApproval: document.getElementById("toolApproval"),
  toolResult: document.getElementById("toolResult"),
  secretForm: document.getElementById("secretForm"),
  secretName: document.getElementById("secretName"),
  secretValue: document.getElementById("secretValue"),
  secretDescription: document.getElementById("secretDescription"),
  secretList: document.getElementById("secretList"),
};

const defaultToolPayloads = {
  "filesystem.list_directory": { path: "." },
  "filesystem.read_file": { path: "README.md" },
  "filesystem.write_file": { path: "notes/example.txt", content: "Hello from Project Q" },
  "filesystem.search_files": { root: ".", query: "TODO", include_content: true, max_results: 25 },
  "filesystem.resolve_file_request": {
    root: ".",
    instruction: "find the resume for Muhammad Umar Qasim",
    query: "resume",
    action: "reveal",
    max_results: 10,
  },
  "filesystem.open_file_choice": { selection: 1, mode: "reveal" },
  "filesystem.allowed_roots": {},
  "spreadsheet.inspect": { path: "data/sales.xlsx" },
  "spreadsheet.analyze": { path: "data/sales.xlsx", operation: "sum", column: "Sales Amount" },
  "spreadsheet.write_analysis": { path: "data/sales.xlsx", operation: "average", column: "Sales Amount" },
  "diagnostics.run_self_check": { source: "manual-tool-run", auto_repair: true },
  "diagnostics.auto_repair": { source: "manual-tool-run" },
  "training.capability_plan": {},
  "training.export_dataset": { reason: "manual-tool-run", max_records: 200 },
  "training.prepare_lora_job": {
    reason: "manual-tool-run",
    base_model: "Qwen/Qwen2.5-Coder-1.5B-Instruct",
    max_records: 300,
  },
  "knowledge.answer": { instruction: "solve 2x + 3 = 11", depth: "standard" },
  "code.generate_website": { instruction: "create a website for an IT consulting company" },
  "code.generate_project": { instruction: "build a Python script that scans this repo for TODO comments" },
  "project.plan_build": { instruction: "build a CRM dashboard app with login, reports, and client notes" },
  "shell.run_command": { command: "Get-ChildItem", workdir: ".", timeout_seconds: 15 },
  "browser.inspect_page": { url: "https://example.com", include_links: true },
  "browser.run_actions": {
    url: "https://example.com",
    actions: [{ type: "extract_text", selector: "body" }],
  },
  "browser.complete_goal": {
    instruction: "find me a tutorial on how to setup this stand",
  },
  "windows.list_windows": { limit: 20 },
  "windows.launch_application": { command: "notepad.exe" },
  "windows.open_url": { url: "https://www.google.com/search?q=project+q" },
  "windows.activate_window": { window_title: "Untitled - Notepad" },
  "windows.send_keys": { window_title: "Untitled - Notepad", keys: "Hello from Project Q" },
};

const defaultRoutineSteps = [
  {
    step_type: "tool",
    label: "Inspect workspace",
    tool_id: "filesystem.list_directory",
    payload: { path: "." },
    continue_on_error: false,
    requires_owner_approval: false,
  },
];

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

async function refreshAll() {
  try {
    const [
      status,
      conversations,
      tasks,
      memories,
      agents,
      routines,
      dispatches,
      audit,
      settings,
      tools,
      secrets,
      learningStatus,
      learningRuns,
      diagnosticsRuns,
    ] = await Promise.all([
      fetchJSON("/api/status"),
      fetchJSON("/api/conversations?limit=50"),
      fetchJSON("/api/tasks"),
      fetchJSON("/api/memories"),
      fetchJSON("/api/agents"),
      fetchJSON("/api/routines"),
      fetchJSON("/api/dispatches?limit=12"),
      fetchJSON("/api/audit"),
      fetchJSON("/api/settings"),
      fetchJSON("/api/tools"),
      fetchJSON("/api/secrets"),
      fetchJSON("/api/learning/status"),
      fetchJSON("/api/learning/runs?limit=8"),
      fetchJSON("/api/diagnostics/runs?limit=8"),
    ]);

    state.status = status;
    state.conversations = conversations.items;
    state.tasks = tasks.items;
    state.memories = memories.items;
    state.agents = agents.items;
    state.routines = routines.items;
    state.dispatches = dispatches.items;
    state.audit = audit.items;
    state.settings = settings;
    state.tools = tools.items;
    state.secrets = secrets.items;
    state.learningStatus = learningStatus;
    state.learningRuns = learningRuns.items;
    state.diagnosticsRuns = diagnosticsRuns.items;
    await refreshProviderModels(settings);
    render();
  } catch (error) {
    els.statusPill.textContent = error.message;
  }
}

async function refreshProviderModels(settingsOverride = null) {
  const settings = settingsOverride || state.settings;
  if ((settings.provider_type || "ollama") !== "ollama") {
    state.providerModels = [];
    state.providerModelError = "";
    return;
  }

  try {
    const payload = await fetchJSON("/api/provider/models");
    state.providerModels = payload.items || [];
    state.providerModelError = payload.error || "";
  } catch (error) {
    state.providerModels = [];
    state.providerModelError = error.message;
  }
}

function render() {
  const reasoner = state.status.reasoner || { mode: "local" };
  const providerNote = reasoner.model_name ? ` - ${reasoner.model_name}` : "";
  const routingNote = reasoner.model_routing_enabled ? " - routed" : "";
  const runtime = state.status.runtime || {};
  const buildNote = runtime.build_id ? ` - ${runtime.build_id}` : "";
  els.statusPill.textContent = `${state.status.project} online - ${reasoner.mode}${providerNote}${routingNote}${buildNote}`;
  els.statusPill.title = runtime.started_at
    ? `Started ${runtime.started_at} (pid ${runtime.pid || "unknown"})`
    : "";

  els.conversationFeed.innerHTML = state.conversations
    .map(
      (message) => `
        <article class="message ${message.role}">
          <strong>${message.role === "assistant" ? "Q" : "Owner"}</strong><br />
          ${escapeHTML(message.content)}
        </article>
      `
    )
    .join("");

  els.taskList.innerHTML = renderItems(state.tasks, (task) => `
    <strong>${escapeHTML(task.title)}</strong>
    <div>${escapeHTML(task.description || "No description")}</div>
    <div class="meta-row">
      <span class="tag">${task.status}</span>
      <span class="tag">P${task.priority}</span>
      <span>${task.updated_at}</span>
    </div>
  `);

  els.memoryList.innerHTML = renderItems(state.memories, (memory) => `
    <strong>${escapeHTML(memory.text)}</strong>
    <div class="meta-row">
      <span class="tag">${memory.kind}</span>
      <span class="tag">${Math.round(memory.confidence * 100)}% confidence</span>
      <span>${memory.updated_at}</span>
    </div>
  `);

  els.agentList.innerHTML = renderItems(state.agents, (agent) => `
    <strong>${escapeHTML(agent.name)}</strong>
    <div>${escapeHTML(agent.goal)}</div>
    <div class="meta-row">
      <span class="tag">${agent.agent_type}</span>
      <span class="tag">${agent.status}</span>
      <span>${agent.time_budget_minutes} min budget</span>
      ${agent.last_run_at ? `<span>last run ${escapeHTML(agent.last_run_at)}</span>` : ""}
      ${agent.last_run_outcome ? `<span>${escapeHTML(agent.last_run_outcome)} via ${escapeHTML(agent.last_run_mode || "unknown")}</span>` : ""}
    </div>
    <div class="list-actions">
      <button type="button" class="secondary-button run-agent-button" data-agent-id="${agent.id}">Run Agent</button>
    </div>
  `);

  els.routineList.innerHTML = renderItems(state.routines, (routine) => `
    <strong>${escapeHTML(routine.name)}</strong>
    <div>${escapeHTML(routine.goal)}</div>
    <div class="meta-row">
      <span class="tag">${routine.trigger_type}</span>
      <span class="tag">${routine.trusted ? "trusted" : "approval-gated"}</span>
      <span>${routine.steps.length} step${routine.steps.length === 1 ? "" : "s"}</span>
      ${routine.last_run_at ? `<span>last run ${escapeHTML(routine.last_run_at)}</span>` : ""}
      ${routine.last_run_outcome ? `<span>${escapeHTML(routine.last_run_outcome)}</span>` : ""}
    </div>
    <div class="list-actions">
      <button type="button" class="secondary-button run-routine-button" data-routine-id="${routine.id}">Run Routine</button>
    </div>
  `);

  els.dispatchList.innerHTML = renderItems(state.dispatches, (dispatch) => `
    <strong>${escapeHTML(dispatch.project_name)}</strong>
    <div>${escapeHTML(dispatch.summary || dispatch.original_prompt)}</div>
    <div class="meta-row">
      <span class="tag">${escapeHTML(dispatch.task_type)}</span>
      <span class="tag">${escapeHTML(dispatch.status)}</span>
      ${dispatch.project_path ? `<span>${escapeHTML(dispatch.project_path)}</span>` : ""}
      <span>${escapeHTML(dispatch.updated_at)}</span>
    </div>
  `);

  els.auditList.innerHTML = renderItems(state.audit, (entry) => `
    <strong>${escapeHTML(entry.action_type)}</strong>
    <div>${escapeHTML(entry.tool_name)} - ${escapeHTML(entry.outcome)}</div>
    <div class="meta-row">
      <span class="tag">Tier ${entry.action_tier}</span>
      <span>${entry.timestamp}</span>
      <span>${entry.approved_by_owner ? "owner-approved" : "auto/blocked"}</span>
    </div>
  `);

  els.secretList.innerHTML = renderItems(state.secrets, (secret) => `
    <strong>${escapeHTML(secret.name)}</strong>
    <div>${escapeHTML(secret.description || "No description")}</div>
    <div class="meta-row">
      <span>${secret.updated_at}</span>
    </div>
  `);

  const learningStatus = state.learningStatus || { running: false, settings: {} };
  const learningSettings = learningStatus.settings || {};
  els.learningStatusText.textContent = learningStatus.running
    ? `Learning Lab running since ${learningStatus.started_at || "now"}. It will run short local simulations and store useful findings.`
    : "Learning Lab is stopped. Use Run Once for a quick simulation or Start for idle background cycles.";
  els.learningRunList.innerHTML = renderItems(state.learningRuns, (run) => `
    <strong>${escapeHTML(run.status)} - ${escapeHTML(run.mode)}</strong>
    <div>${escapeHTML(run.summary || "No summary")}</div>
    <div class="meta-row">
      <span class="tag">${escapeHTML(run.reason || "manual")}</span>
      <span>${escapeHTML(run.completed_at || run.started_at)}</span>
    </div>
  `);
  els.diagnosticsRunList.innerHTML = renderItems(state.diagnosticsRuns, (run) => `
    <strong>${escapeHTML(run.status)}</strong>
    <div>${escapeHTML(run.summary || "No summary")}</div>
    <div class="meta-row">
      <span class="tag">${escapeHTML(run.source || "manual")}</span>
      <span>${escapeHTML(run.completed_at || run.started_at)}</span>
    </div>
  `);

  els.ownerName.value = state.settings.owner_name || "";
  els.aggressionLevel.value = state.settings.aggression_level || "operator";
  els.autoApproveTier.value = String(state.settings.auto_approve_tier ?? 1);
  els.notificationsEnabled.checked = Boolean(state.settings.notifications_enabled);
  els.providerEnabled.checked = Boolean(state.settings.provider_enabled);
  els.providerType.value = state.settings.provider_type || "ollama";
  els.modelName.value = state.settings.model_name || "";
  els.ollamaModelRoutingEnabled.checked = Boolean(state.settings.ollama_model_routing_enabled ?? true);
  els.ollamaGeneralModel.value = state.settings.ollama_general_model || "";
  els.ollamaCodingModel.value = state.settings.ollama_coding_model || "";
  els.ollamaReasoningModel.value = state.settings.ollama_reasoning_model || "";
  els.ollamaFastModel.value = state.settings.ollama_fast_model || "";
  els.modelBaseUrl.value = state.settings.model_base_url || "";
  els.modelSecretName.value = state.settings.model_secret_name || "";
  els.providerTimeoutSeconds.value = String(state.settings.provider_timeout_seconds ?? 45);
  els.browserHeadless.checked = Boolean(state.settings.browser_headless);
  els.browserChannel.value = state.settings.browser_channel || "";
  els.browserExecutablePath.value = state.settings.browser_executable_path || "";
  els.fileAccessRoots.value = (state.settings.file_access_roots || []).join("\n");
  els.learningIntervalSeconds.value = String(
    state.settings.learning_interval_seconds || learningSettings.learning_interval_seconds || 300
  );
  els.learningMaxCyclesPerStart.value = String(
    state.settings.learning_max_cycles_per_start || learningSettings.learning_max_cycles_per_start || 12
  );
  els.providerModelStatus.textContent = providerModelStatusText();
  els.providerModelList.innerHTML = renderProviderModels();
  if (!els.routineSteps.value.trim()) {
    els.routineSteps.value = JSON.stringify(defaultRoutineSteps, null, 2);
  }

  els.toolSelect.innerHTML = state.tools
    .map((tool) => `<option value="${tool.tool_id}">${tool.tool_id} - Tier ${tool.tier}</option>`)
    .join("");

  setToolPayloadExample();
}

function renderItems(items, renderer) {
  return items
    .map((item) => `<article class="list-item">${renderer(item)}</article>`)
    .join("");
}

function renderProviderModels() {
  if (state.providerModels.length === 0) {
    return "";
  }
  return state.providerModels
    .map(
      (model) => `
        <article class="list-item">
          <strong>${escapeHTML(model.name)}</strong>
          <div class="meta-row">
            ${model.family ? `<span class="tag">${escapeHTML(model.family)}</span>` : ""}
            ${model.parameter_size ? `<span class="tag">${escapeHTML(model.parameter_size)}</span>` : ""}
            ${model.quantization_level ? `<span class="tag">${escapeHTML(model.quantization_level)}</span>` : ""}
          </div>
        </article>
      `
    )
    .join("");
}

function providerModelStatusText() {
  const providerType = state.settings.provider_type || "ollama";
  if (providerType !== "ollama") {
    return "Switch provider type to Ollama to browse local models. Disable provider to use the built-in heuristic path.";
  }
  if (state.providerModelError) {
    return `Ollama model check failed: ${state.providerModelError}`;
  }
  if (state.providerModels.length === 0) {
    return "No local Ollama models detected yet. Install Ollama and pull the verified four-model stack.";
  }
  if (state.settings.ollama_model_routing_enabled ?? true) {
    return `${state.providerModels.length} local Ollama model(s) available. Routing will use general, coding, reasoning, and fast lanes when configured.`;
  }
  return `${state.providerModels.length} local Ollama model(s) available. Single-model mode is enabled.`;
}

function setToolPayloadExample() {
  const toolId = els.toolSelect.value;
  const sample = defaultToolPayloads[toolId];
  if (sample) {
    els.toolPayload.value = JSON.stringify(sample, null, 2);
  }
}

els.refreshButton.addEventListener("click", refreshAll);
els.toolSelect.addEventListener("change", setToolPayloadExample);
els.refreshModelsButton.addEventListener("click", async () => {
  await refreshProviderModels();
  render();
});

els.learningRunOnceButton.addEventListener("click", async () => {
  els.learningRunOnceButton.disabled = true;
  try {
    const result = await fetchJSON("/api/learning/run-once", {
      method: "POST",
      body: JSON.stringify({ reason: "manual dashboard simulation" }),
    });
    els.toolResult.textContent = JSON.stringify(result, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  } finally {
    els.learningRunOnceButton.disabled = false;
  }
});

els.learningStartButton.addEventListener("click", async () => {
  els.learningStartButton.disabled = true;
  try {
    const result = await fetchJSON("/api/learning/start", {
      method: "POST",
      body: JSON.stringify({
        max_cycles: Number(els.learningMaxCyclesPerStart.value || 12),
        interval_seconds: Number(els.learningIntervalSeconds.value || 300),
      }),
    });
    els.toolResult.textContent = JSON.stringify(result, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  } finally {
    els.learningStartButton.disabled = false;
  }
});

els.learningStopButton.addEventListener("click", async () => {
  els.learningStopButton.disabled = true;
  try {
    const result = await fetchJSON("/api/learning/stop", {
      method: "POST",
      body: JSON.stringify({}),
    });
    els.toolResult.textContent = JSON.stringify(result, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  } finally {
    els.learningStopButton.disabled = false;
  }
});

els.diagnosticsRunButton.addEventListener("click", async () => {
  els.diagnosticsRunButton.disabled = true;
  try {
    const result = await fetchJSON("/api/diagnostics/run", {
      method: "POST",
      body: JSON.stringify({ source: "dashboard", auto_repair: true }),
    });
    els.toolResult.textContent = JSON.stringify(result, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  } finally {
    els.diagnosticsRunButton.disabled = false;
  }
});

els.diagnosticsAutoRepairButton.addEventListener("click", async () => {
  els.diagnosticsAutoRepairButton.disabled = true;
  try {
    const result = await fetchJSON("/api/diagnostics/auto-repair", {
      method: "POST",
      body: JSON.stringify({ source: "dashboard" }),
    });
    els.toolResult.textContent = JSON.stringify(result, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  } finally {
    els.diagnosticsAutoRepairButton.disabled = false;
  }
});

els.trainingExportButton.addEventListener("click", async () => {
  els.trainingExportButton.disabled = true;
  try {
    const result = await fetchJSON("/api/training/export", {
      method: "POST",
      body: JSON.stringify({ reason: "dashboard", max_records: 200 }),
    });
    els.toolResult.textContent = JSON.stringify(result, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  } finally {
    els.trainingExportButton.disabled = false;
  }
});

els.trainingPrepareLoraButton.addEventListener("click", async () => {
  els.trainingPrepareLoraButton.disabled = true;
  try {
    const result = await fetchJSON("/api/training/prepare-lora", {
      method: "POST",
      body: JSON.stringify({
        reason: "dashboard",
        base_model: "Qwen/Qwen2.5-Coder-1.5B-Instruct",
        max_records: 300,
      }),
    });
    els.toolResult.textContent = JSON.stringify(result, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  } finally {
    els.trainingPrepareLoraButton.disabled = false;
  }
});

els.agentList.addEventListener("click", async (event) => {
  const button = event.target.closest(".run-agent-button");
  if (!button) return;
  const agentId = button.dataset.agentId;
  if (!agentId) return;
  button.disabled = true;
  button.textContent = "Running...";
  try {
    const result = await fetchJSON(`/api/agents/${agentId}/run`, {
      method: "POST",
      body: JSON.stringify({ owner_approved: false }),
    });
    els.toolResult.textContent = JSON.stringify(result, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Run Agent";
  }
});

els.routineList.addEventListener("click", async (event) => {
  const button = event.target.closest(".run-routine-button");
  if (!button) return;
  const routineId = button.dataset.routineId;
  if (!routineId) return;
  button.disabled = true;
  button.textContent = "Running...";
  try {
    const result = await fetchJSON(`/api/routines/${routineId}/run`, {
      method: "POST",
      body: JSON.stringify({ owner_approved: false }),
    });
    els.toolResult.textContent = JSON.stringify(result, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Run Routine";
  }
});

els.chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = els.chatInput.value.trim();
  if (!message) return;
  const response = await fetchJSON("/api/chat", {
    method: "POST",
    body: JSON.stringify({ message, owner_approved: els.chatApproval.checked }),
  });
  els.toolResult.textContent = JSON.stringify(response, null, 2);
  els.chatInput.value = "";
  els.chatApproval.checked = false;
  await refreshAll();
});

els.taskForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = els.taskTitle.value.trim();
  if (!title) return;
  await fetchJSON("/api/tasks", {
    method: "POST",
    body: JSON.stringify({ title }),
  });
  els.taskTitle.value = "";
  await refreshAll();
});

els.memoryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = els.memoryText.value.trim();
  if (!text) return;
  await fetchJSON("/api/memories", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
  els.memoryText.value = "";
  await refreshAll();
});

els.agentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = els.agentName.value.trim();
  const goal = els.agentGoal.value.trim();
  if (!name || !goal) return;
  await fetchJSON("/api/agents", {
    method: "POST",
    body: JSON.stringify({
      name,
      goal,
      agent_type: "general",
      status: "active",
      tools: [
        "filesystem.list_directory",
        "browser.inspect_page",
        "browser.run_actions",
        "windows.open_url",
      ],
    }),
  });
  els.agentName.value = "";
  els.agentGoal.value = "";
  await refreshAll();
});

els.routineForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = els.routineName.value.trim();
  const goal = els.routineGoal.value.trim();
  if (!name || !goal) return;
  try {
    const steps = JSON.parse(els.routineSteps.value);
    await fetchJSON("/api/routines", {
      method: "POST",
      body: JSON.stringify({
        name,
        goal,
        description: "Manual routine created from the Project Q dashboard.",
        status: "active",
        trigger_type: "manual",
        trusted: els.routineTrusted.checked,
        steps,
      }),
    });
    els.routineName.value = "";
    els.routineGoal.value = "";
    els.routineTrusted.checked = false;
    els.routineSteps.value = JSON.stringify(defaultRoutineSteps, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  }
});

els.toolForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = JSON.parse(els.toolPayload.value);
    const result = await fetchJSON("/api/tools/execute", {
      method: "POST",
      body: JSON.stringify({
        tool_id: els.toolSelect.value,
        payload,
        owner_approved: els.toolApproval.checked,
      }),
    });
    els.toolResult.textContent = JSON.stringify(result, null, 2);
    await refreshAll();
  } catch (error) {
    els.toolResult.textContent = error.message;
  }
});

els.secretForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = els.secretName.value.trim();
  const value = els.secretValue.value.trim();
  const description = els.secretDescription.value.trim();
  if (!name || !value) return;
  await fetchJSON("/api/secrets", {
    method: "POST",
    body: JSON.stringify({ name, value, description }),
  });
  els.secretName.value = "";
  els.secretValue.value = "";
  els.secretDescription.value = "";
  await refreshAll();
});

els.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await fetchJSON("/api/settings", {
    method: "PUT",
    body: JSON.stringify({
      owner_name: els.ownerName.value.trim(),
      aggression_level: els.aggressionLevel.value,
      auto_approve_tier: Number(els.autoApproveTier.value),
      notifications_enabled: els.notificationsEnabled.checked,
      provider_enabled: els.providerEnabled.checked,
      provider_type: els.providerType.value,
      model_name: els.modelName.value.trim(),
      ollama_model_routing_enabled: els.ollamaModelRoutingEnabled.checked,
      ollama_general_model: els.ollamaGeneralModel.value.trim(),
      ollama_coding_model: els.ollamaCodingModel.value.trim(),
      ollama_reasoning_model: els.ollamaReasoningModel.value.trim(),
      ollama_fast_model: els.ollamaFastModel.value.trim(),
      model_base_url: els.modelBaseUrl.value.trim(),
      model_secret_name: els.modelSecretName.value.trim(),
      provider_timeout_seconds: Number(els.providerTimeoutSeconds.value),
      browser_headless: els.browserHeadless.checked,
      browser_channel: els.browserChannel.value.trim(),
      browser_executable_path: els.browserExecutablePath.value.trim(),
      file_access_roots: els.fileAccessRoots.value
        .split("\n")
        .map((root) => root.trim())
        .filter(Boolean),
      learning_interval_seconds: Number(els.learningIntervalSeconds.value || 300),
      learning_max_cycles_per_start: Number(els.learningMaxCyclesPerStart.value || 12),
    }),
  });
  await refreshAll();
});

function escapeHTML(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

refreshAll();
