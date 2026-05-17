from __future__ import annotations

import json
import shutil
import uuid
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.models import AgentCreate, ChatRequest, ReasonerPlan, RoutineCreate, SettingsUpdate, TaskCreate
from project_q.services.artifacts import ArtifactValidationService
from project_q.services.reasoner import ReasonerService
from project_q.tools.base import ToolDefinition
from project_q.tools.browser import BrowserActionsTool, BrowserGoalTool
from project_q.tools.windows import WindowsListWindowsTool, WindowsOpenUrlTool


def write_minimal_docx(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types/>')
        archive.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{escaped}</w:t></w:r></w:p></w:body>"
                "</w:document>"
            ),
        )


def write_sales_workbook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.append(["Rep", "Sales Amount", "Units"])
    sheet.append(["Aisha", 120, 2])
    sheet.append(["Umar", 180, 3])
    sheet.append(["Razia", 300, 5])
    workbook.save(path)


class FakeBrowserInspectTool:
    definition = ToolDefinition(
        tool_id="browser.inspect_page",
        name="Inspect Browser Page",
        description="fake browser tool",
        tier=1,
    )

    def execute(self, payload):
        return {
            "title": "Example Domain",
            "url": payload["url"],
            "text_excerpt": "Example Domain body text",
            "links": [],
        }


class FakeOpenUrlTool:
    definition = ToolDefinition(
        tool_id="windows.open_url",
        name="Open URL",
        description="fake open url tool",
        tier=1,
    )

    def execute(self, payload):
        return {
            "opened": True,
            "url": payload["url"],
        }


class FakeBrowserGoalTool:
    definition = ToolDefinition(
        tool_id="browser.complete_goal",
        name="Complete Browser Goal",
        description="fake browser goal tool",
        tier=1,
    )

    def execute(self, payload):
        return {
            "opened_url": "https://www.google.com/search?tbm=isch&tbs=isz:l&q=invincible",
            "intent": {
                "mode": "image_search",
                "query": "invincible",
            },
            "instruction": payload["instruction"],
        }


class FakeGoalInspectTool:
    def execute(self, payload):
        return {
            "title": "Search results",
            "url": payload["url"],
            "links": [
                {
                    "text": "How to set up this stand - full tutorial",
                    "href": "https://www.youtube.com/watch?v=abc123",
                },
                {
                    "text": "Channel link",
                    "href": "https://www.youtube.com/@creator",
                },
            ],
        }


class FakeCodeRepairService:
    def repair_python_file(self, *, path, content, validation):
        del path, content, validation
        return {
            "content": "from pathlib import Path\nprint(Path('repo') / 'file.py')\n",
            "model_name": "qwen2.5-coder:7b",
        }


class FakeRuntimeCodeRepairService:
    def repair_python_file(self, *, path, content, validation):
        del path, content, validation
        return {
            "content": (
                "from pathlib import Path\n\n"
                "def scan_todo_comments(root_dir):\n"
                "    for file_path in Path(root_dir).rglob('*'):\n"
                "        if not file_path.is_file():\n"
                "            continue\n"
                "        if file_path.suffix not in {'.py', '.html'}:\n"
                "            continue\n"
                "        for index, line in enumerate(file_path.read_text(encoding='utf-8').splitlines(), start=1):\n"
                "            if 'TODO' in line:\n"
                "                print(f'{file_path}:{index}: {line.strip()}')\n\n"
                "if __name__ == '__main__':\n"
                "    scan_todo_comments(Path(__file__).resolve().parents[1])\n"
            ),
            "model_name": "qwen2.5-coder:7b",
        }


class ProjectQApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="Project Q Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8899,
        )
        self.app = create_application(config)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_chat_can_create_memory_task_and_agent(self) -> None:
        memory_reply = self.app.conversations.respond(ChatRequest(message="remember I prefer concise answers"))
        task_reply = self.app.conversations.respond(ChatRequest(message="task: ship the Windows MVP"))
        agent_reply = self.app.conversations.respond(ChatRequest(message="spawn a coding agent to build the website shell"))

        self.assertEqual(len(memory_reply.created_memory_ids), 1)
        self.assertEqual(len(task_reply.created_task_ids), 1)
        self.assertEqual(len(agent_reply.created_agent_ids), 1)
        self.assertEqual(len(self.app.memory.list_all()), 1)
        self.assertEqual(len(self.app.tasks.list_all()), 1)
        self.assertEqual(len(self.app.agents.list_all()), 1)

    def test_chat_can_create_routine(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(message="automation: open VS Code and list files in the workspace")
        )

        self.assertEqual(len(reply.created_routine_ids), 1)
        created = self.app.routines.list_all()
        self.assertEqual(len(created), 1)
        self.assertGreaterEqual(len(created[0]["steps"]), 1)

    def test_chat_can_auto_execute_local_browser_tool(self) -> None:
        self.app.tools.tools["browser.inspect_page"] = FakeBrowserInspectTool()

        reply = self.app.conversations.respond(
            ChatRequest(message="Summarize https://example.com for me")
        )

        self.assertEqual(len(reply.executed_tools), 1)
        self.assertEqual(reply.executed_tools[0]["tool_id"], "browser.inspect_page")
        self.assertIn("Executed tools:", reply.reply)

    def test_chat_can_open_visible_browser_search_in_heuristic_mode(self) -> None:
        self.app.tools.tools["windows.open_url"] = FakeOpenUrlTool()

        reply = self.app.conversations.respond(
            ChatRequest(message="open the browser and search google for invincible")
        )

        self.assertEqual(len(reply.executed_tools), 1)
        self.assertEqual(reply.executed_tools[0]["tool_id"], "windows.open_url")
        self.assertEqual(reply.executed_tools[0]["result"]["url"], "https://www.google.com/search?q=invincible")

    def test_chat_normalizes_model_browser_actions_to_visible_open_url(self) -> None:
        self.app.tools.tools["windows.open_url"] = FakeOpenUrlTool()
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="ollama",
                model_name="qwen3.5:9b",
                ollama_model_routing_enabled=True,
                ollama_general_model="qwen3.5:9b",
                ollama_coding_model="qwen2.5-coder:7b",
                ollama_reasoning_model="deepseek-r1:7b",
                ollama_fast_model="llama3.1:8b",
                model_base_url="http://localhost:11434/api/chat",
                model_secret_name="",
            )
        )

        def fake_transport(_url, _body, _headers, _timeout_seconds):
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "Opening browser and searching for 'invincible' on Google.",
                            "memory_writes": [],
                            "task_writes": [],
                            "agent_writes": [],
                            "routine_writes": [],
                            "tool_calls": [
                                {
                                    "tool_id": "browser.run_actions",
                                    "reason": "Search in the browser.",
                                    "payload": {
                                        "url": "https://www.google.com/search?q=invincible",
                                        "actions": [],
                                    },
                                }
                            ],
                        }
                    ),
                }
            }

        self.app.reasoner.transport = fake_transport
        reply = self.app.conversations.respond(
            ChatRequest(message="open the browser and search google for invincible")
        )

        self.assertEqual(len(reply.executed_tools), 1)
        self.assertEqual(reply.executed_tools[0]["tool_id"], "windows.open_url")
        self.assertEqual(reply.executed_tools[0]["result"]["url"], "https://www.google.com/search?q=invincible")

    def test_chat_routes_complex_browser_goal_to_browser_complete_goal(self) -> None:
        self.app.tools.tools["browser.complete_goal"] = FakeBrowserGoalTool()

        reply = self.app.conversations.respond(
            ChatRequest(
                message="open my browser and search google for invinicible, than go to images and find a very good high quality image"
            )
        )

        self.assertEqual(len(reply.executed_tools), 1)
        self.assertEqual(reply.executed_tools[0]["tool_id"], "browser.complete_goal")

    def test_browser_goal_tool_opens_google_images_with_corrected_query(self) -> None:
        open_url = FakeOpenUrlTool()
        tool = BrowserGoalTool(
            self.root / ".project_q",
            open_url_tool=open_url,
            inspect_tool=FakeGoalInspectTool(),
        )

        result = tool.execute(
            {
                "instruction": "open my browser and search google for invinicible, than go to images and find a very good high quality image"
            }
        )

        self.assertEqual(result["intent"]["mode"], "image_search")
        self.assertEqual(result["intent"]["query"], "invincible")
        self.assertEqual(result["opened_url"], "https://www.google.com/search?tbm=isch&tbs=isz:l&q=invincible")

    def test_browser_goal_tool_opens_selected_tutorial_video(self) -> None:
        open_url = FakeOpenUrlTool()
        tool = BrowserGoalTool(
            self.root / ".project_q",
            open_url_tool=open_url,
            inspect_tool=FakeGoalInspectTool(),
        )

        result = tool.execute(
            {
                "instruction": "find me a tutorial on how to setup this stand",
            }
        )

        self.assertEqual(result["intent"]["mode"], "tutorial_video")
        self.assertEqual(result["intent"]["query"], "how to setup this stand")
        self.assertEqual(result["opened_url"], "https://www.youtube.com/watch?v=abc123&autoplay=1")

    def test_policy_blocks_high_tier_tool_without_approval(self) -> None:
        self.app.settings.update(SettingsUpdate(auto_approve_tier=1))
        shell_tool = self.app.tools.get("shell.run_command")
        decision = self.app.policy.authorize_tool(tier=shell_tool.definition.tier, owner_approved=False)

        self.assertFalse(decision.allowed)
        self.assertIn("requires owner approval", decision.reason)

    def test_filesystem_tool_can_write_with_owner_approval(self) -> None:
        write_tool = self.app.tools.get("filesystem.write_file")
        decision = self.app.policy.authorize_tool(tier=write_tool.definition.tier, owner_approved=True)
        self.assertTrue(decision.allowed)

        result = write_tool.execute({"path": "notes/test.txt", "content": "Project Q"})
        written = Path(result["path"]).read_text(encoding="utf-8")
        self.assertEqual(written, "Project Q")

    def test_filesystem_tools_can_use_owner_configured_roots(self) -> None:
        owner_root = self.root.parent / f"owner-files-{uuid.uuid4().hex}"
        owner_root.mkdir(parents=True, exist_ok=True)
        try:
            (owner_root / "brief.txt").write_text("Project Q can read this owner file.", encoding="utf-8")
            self.app.settings.update(SettingsUpdate(file_access_roots=[str(owner_root)]))

            read_tool = self.app.tools.get("filesystem.read_file")
            result = read_tool.execute({"path": str(owner_root / "brief.txt")})

            self.assertEqual(result["content"], "Project Q can read this owner file.")
        finally:
            shutil.rmtree(owner_root, ignore_errors=True)

    def test_filesystem_search_finds_filename_and_content_inside_allowed_roots(self) -> None:
        owner_root = self.root.parent / f"search-root-{uuid.uuid4().hex}"
        owner_root.mkdir(parents=True, exist_ok=True)
        try:
            (owner_root / "finance_note.txt").write_text("invoice from Acme is due Friday", encoding="utf-8")
            (owner_root / "ignore.bin").write_bytes(b"\x00\x01\x02")
            self.app.settings.update(SettingsUpdate(file_access_roots=[str(owner_root)]))

            search_tool = self.app.tools.get("filesystem.search_files")
            result = search_tool.execute(
                {
                    "root": str(owner_root),
                    "query": "invoice",
                    "include_content": True,
                    "max_results": 10,
                }
            )

            self.assertEqual(result["result_count"], 1)
            self.assertEqual(result["results"][0]["name"], "finance_note.txt")
            self.assertIn("invoice from Acme", result["results"][0]["preview"])
        finally:
            shutil.rmtree(owner_root, ignore_errors=True)

    def test_chat_can_search_owner_files_heuristically(self) -> None:
        owner_root = self.root.parent / f"chat-search-root-{uuid.uuid4().hex}"
        owner_root.mkdir(parents=True, exist_ok=True)
        try:
            (owner_root / "invoice.txt").write_text("invoice from Acme is due Friday", encoding="utf-8")
            self.app.settings.update(SettingsUpdate(file_access_roots=[str(owner_root)]))

            with patch("project_q.tools.filesystem.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = json.dumps({"opened": True, "mode": "reveal"})
                mock_run.return_value.stderr = ""
                reply = self.app.conversations.respond(ChatRequest(message="search my files for invoice"))

            self.assertEqual(len(reply.executed_tools), 1)
            self.assertEqual(reply.executed_tools[0]["tool_id"], "filesystem.resolve_file_request")
            self.assertEqual(reply.executed_tools[0]["result"]["status"], "resolved")
        finally:
            shutil.rmtree(owner_root, ignore_errors=True)

    def test_provider_enabled_file_search_uses_deterministic_router_before_stale_model(self) -> None:
        owner_root = self.root.parent / f"provider-file-router-{uuid.uuid4().hex}"
        owner_root.mkdir(parents=True, exist_ok=True)
        try:
            write_minimal_docx(owner_root / "Razia_Qasim_Resume.docx", "Razia Qasim resume")
            write_minimal_docx(owner_root / "Resume.docx", "Muhammad Umar Qasim resume")
            self.app.settings.update(
                SettingsUpdate(
                    file_access_roots=[str(owner_root)],
                    provider_enabled=True,
                    provider_type="ollama",
                    model_name="qwen3.5:9b",
                    ollama_model_routing_enabled=True,
                    ollama_general_model="qwen3.5:9b",
                    ollama_coding_model="qwen2.5-coder:7b",
                    ollama_reasoning_model="deepseek-r1:7b",
                    ollama_fast_model="llama3.1:8b",
                    model_base_url="http://localhost:11434/api/chat",
                    model_secret_name="",
                )
            )
            provider_calls: list[bytes] = []

            def stale_transport(_url, body, _headers, _timeout_seconds):
                provider_calls.append(body)
                return {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "reply": "The repository contains no `TODO` comments.",
                                "memory_writes": [],
                                "task_writes": [],
                                "agent_writes": [],
                                "routine_writes": [],
                                "tool_calls": [],
                            }
                        ),
                    }
                }

            self.app.reasoner.transport = stale_transport

            reply = self.app.conversations.respond(ChatRequest(message="search files for resume"))

            self.assertEqual(provider_calls, [])
            self.assertEqual(reply.reasoning_mode, "deterministic")
            self.assertEqual(reply.executed_tools[0]["tool_id"], "filesystem.resolve_file_request")
            self.assertEqual(reply.executed_tools[0]["result"]["status"], "needs_confirmation")
            self.assertNotIn("TODO", reply.reply)
        finally:
            shutil.rmtree(owner_root, ignore_errors=True)

    def test_file_resolution_asks_for_confirmation_when_resume_matches_are_ambiguous(self) -> None:
        owner_root = self.root.parent / f"resume-root-{uuid.uuid4().hex}"
        owner_root.mkdir(parents=True, exist_ok=True)
        try:
            write_minimal_docx(owner_root / "Razia_Qasim_Resume.docx", "Razia Qasim resume")
            write_minimal_docx(owner_root / "Resume.docx", "Muhammad Umar Qasim resume")
            self.app.settings.update(SettingsUpdate(file_access_roots=[str(owner_root)]))

            resolver = self.app.tools.get("filesystem.resolve_file_request")
            result = resolver.execute(
                {
                    "root": str(owner_root),
                    "instruction": "search files for resume",
                    "query": "resume",
                    "action": "reveal",
                    "max_results": 10,
                }
            )

            self.assertEqual(result["status"], "needs_confirmation")
            self.assertTrue(result["choice_id"].startswith("choice_"))
            self.assertEqual(len(result["options"]), 2)
            self.assertIn("Which file did you mean", result["message"])
        finally:
            shutil.rmtree(owner_root, ignore_errors=True)

    def test_file_resolution_uses_docx_preview_to_pick_specific_resume(self) -> None:
        owner_root = self.root.parent / f"specific-resume-root-{uuid.uuid4().hex}"
        owner_root.mkdir(parents=True, exist_ok=True)
        try:
            write_minimal_docx(owner_root / "Razia_Qasim_Resume.docx", "Razia Qasim resume")
            target = owner_root / "Resume.docx"
            write_minimal_docx(target, "Muhammad Umar Qasim resume software engineer")
            self.app.settings.update(SettingsUpdate(file_access_roots=[str(owner_root)]))

            resolver = self.app.tools.get("filesystem.resolve_file_request")
            result = resolver.execute(
                {
                    "root": str(owner_root),
                    "instruction": "find the file resume that is for Muhammad Umar Qasim",
                    "query": "resume",
                    "action": "reveal",
                    "dry_run": True,
                    "max_results": 10,
                }
            )

            self.assertEqual(result["status"], "resolved")
            self.assertEqual(Path(result["selected"]["path"]).resolve(), target.resolve())
            self.assertEqual(result["action_result"]["mode"], "reveal")
            self.assertTrue(result["action_result"]["dry_run"])
        finally:
            shutil.rmtree(owner_root, ignore_errors=True)

    def test_chat_file_search_lists_choices_and_followup_opens_selected_option(self) -> None:
        owner_root = self.root.parent / f"choice-resume-root-{uuid.uuid4().hex}"
        owner_root.mkdir(parents=True, exist_ok=True)
        try:
            write_minimal_docx(owner_root / "Razia_Qasim_Resume.docx", "Razia Qasim resume")
            write_minimal_docx(owner_root / "Resume.docx", "Muhammad Umar Qasim resume")
            self.app.settings.update(SettingsUpdate(file_access_roots=[str(owner_root)]))

            first = self.app.conversations.respond(ChatRequest(message="search files for resume"))
            self.assertEqual(first.executed_tools[0]["tool_id"], "filesystem.resolve_file_request")
            self.assertEqual(first.executed_tools[0]["result"]["status"], "needs_confirmation")
            self.assertIn("open option 1", first.reply.lower())

            with patch("project_q.tools.filesystem.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = json.dumps({"opened": True, "mode": "reveal"})
                mock_run.return_value.stderr = ""
                followup = self.app.conversations.respond(ChatRequest(message="open option 2"))

            self.assertEqual(followup.executed_tools[0]["tool_id"], "filesystem.open_file_choice")
            self.assertEqual(followup.executed_tools[0]["result"]["selection"], 2)
            self.assertTrue(followup.executed_tools[0]["result"]["action_result"]["opened"])
        finally:
            shutil.rmtree(owner_root, ignore_errors=True)

    def test_spreadsheet_analyze_calculates_sum_and_average_by_header(self) -> None:
        workbook_path = self.root / "data" / "sales.xlsx"
        write_sales_workbook(workbook_path)

        tool = self.app.tools.get("spreadsheet.analyze")
        total = tool.execute({"path": str(workbook_path), "operation": "sum", "column": "sales"})
        average = tool.execute({"path": str(workbook_path), "operation": "average", "column": "sales amount"})

        self.assertEqual(total["result"], 600)
        self.assertEqual(average["result"], 200)
        self.assertEqual(total["matched_column"], "Sales Amount")

    def test_spreadsheet_write_analysis_creates_summary_copy(self) -> None:
        workbook_path = self.root / "data" / "sales.xlsx"
        write_sales_workbook(workbook_path)

        tool = self.app.tools.get("spreadsheet.write_analysis")
        result = tool.execute(
            {
                "path": str(workbook_path),
                "operation": "average",
                "column": "sales",
            }
        )

        output_path = Path(result["output_path"])
        self.assertTrue(output_path.exists())
        workbook = load_workbook(output_path, data_only=False)
        self.assertIn("Project Q Analysis", workbook.sheetnames)
        summary = workbook["Project Q Analysis"]
        self.assertEqual(summary["A1"].value, "Project Q Analysis")
        self.assertEqual(summary["B4"].value, 200)

    def test_chat_can_analyze_spreadsheet_total_heuristically(self) -> None:
        workbook_path = self.root / "data" / "sales.xlsx"
        write_sales_workbook(workbook_path)

        reply = self.app.conversations.respond(
            ChatRequest(message=f"what is the total sales amount in {workbook_path}")
        )

        self.assertEqual(len(reply.executed_tools), 1)
        self.assertEqual(reply.executed_tools[0]["tool_id"], "spreadsheet.analyze")
        self.assertEqual(reply.executed_tools[0]["result"]["result"], 600)

    def test_spreadsheet_analyze_can_group_totals_by_column(self) -> None:
        workbook_path = self.root / "data" / "sales.xlsx"
        write_sales_workbook(workbook_path)

        tool = self.app.tools.get("spreadsheet.analyze")
        result = tool.execute(
            {
                "path": str(workbook_path),
                "operation": "sum",
                "column": "sales amount",
                "group_by": "rep",
            }
        )

        self.assertEqual(result["group_by"], "Rep")
        self.assertEqual(result["groups"]["Aisha"], 120)
        self.assertEqual(result["groups"]["Umar"], 180)
        self.assertEqual(result["groups"]["Razia"], 300)

    def test_chat_can_analyze_spreadsheet_grouped_totals_heuristically(self) -> None:
        workbook_path = self.root / "data" / "sales.xlsx"
        write_sales_workbook(workbook_path)

        reply = self.app.conversations.respond(
            ChatRequest(message=f"what is the total sales amount by rep in {workbook_path}")
        )

        self.assertEqual(reply.executed_tools[0]["tool_id"], "spreadsheet.analyze")
        self.assertEqual(reply.executed_tools[0]["result"]["groups"]["Aisha"], 120)
        self.assertEqual(reply.executed_tools[0]["result"]["group_by"], "Rep")

    def test_chat_routes_essay_request_to_knowledge_workbench(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(message="write a five paragraph essay about the causes of World War I")
        )

        self.assertEqual(reply.reasoning_mode, "deterministic")
        self.assertEqual(reply.executed_tools[0]["tool_id"], "knowledge.answer")
        result = reply.executed_tools[0]["result"]
        self.assertEqual(result["domain"], "writing")
        self.assertIn("Thesis", result["answer"])

    def test_knowledge_workbench_solves_linear_math_locally(self) -> None:
        tool = self.app.tools.get("knowledge.answer")
        result = tool.execute({"instruction": "solve 2x + 3 = 11"})

        self.assertEqual(result["domain"], "math")
        self.assertEqual(result["source"], "local-structured")
        self.assertIn("x = 4", result["answer"])

    def test_knowledge_workbench_solves_basic_physics_locally(self) -> None:
        tool = self.app.tools.get("knowledge.answer")
        result = tool.execute({"instruction": "physics problem: a 10 kg object accelerates at 2 m/s^2. What force is needed?"})

        self.assertEqual(result["domain"], "physics")
        self.assertIn("20 N", result["answer"])

    def test_knowledge_workbench_handles_history_and_quant_prompts(self) -> None:
        tool = self.app.tools.get("knowledge.answer")
        history = tool.execute({"instruction": "explain the historical importance of the printing press"})
        quant = tool.execute({"instruction": "quant: calculate the return if price moves from 100 to 112"})

        self.assertEqual(history["domain"], "history")
        self.assertIn("Context", history["answer"])
        self.assertEqual(quant["domain"], "quant")
        self.assertIn("12.00%", quant["answer"])

    def test_knowledge_workbench_uses_ollama_for_deeper_answer_when_configured(self) -> None:
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="ollama",
                model_name="qwen3.5:9b",
                ollama_model_routing_enabled=True,
                ollama_general_model="qwen3.5:9b",
                ollama_coding_model="qwen2.5-coder:7b",
                ollama_reasoning_model="deepseek-r1:7b",
                ollama_fast_model="llama3.1:8b",
                model_base_url="http://localhost:11434/api/chat",
                model_secret_name="",
            )
        )

        def fake_transport(_url, body, _headers, _timeout_seconds):
            request_payload = json.loads(body.decode("utf-8"))
            self.assertEqual(request_payload["model"], "deepseek-r1:7b")
            return {
                "message": {
                    "role": "assistant",
                    "content": "Use momentum conservation, define the system, then solve step by step.",
                }
            }

        self.app.knowledge.transport = fake_transport

        reply = self.app.conversations.respond(
            ChatRequest(message="solve this physics problem using conservation of momentum")
        )

        result = reply.executed_tools[0]["result"]
        self.assertEqual(reply.reasoning_mode, "deterministic")
        self.assertEqual(result["source"], "local-model")
        self.assertEqual(result["model_name"], "deepseek-r1:7b")
        self.assertIn("momentum conservation", result["answer"])

    def test_provider_enabled_website_request_routes_to_generator_before_stale_model(self) -> None:
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="ollama",
                model_name="qwen3.5:9b",
                ollama_model_routing_enabled=True,
                ollama_general_model="qwen3.5:9b",
                ollama_coding_model="qwen2.5-coder:7b",
                ollama_reasoning_model="deepseek-r1:7b",
                ollama_fast_model="llama3.1:8b",
                model_base_url="http://localhost:11434/api/chat",
                model_secret_name="",
            )
        )
        provider_calls: list[bytes] = []

        def stale_transport(_url, body, _headers, _timeout_seconds):
            provider_calls.append(body)
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "Understood. I will not create any new memory, tasks, agents, routines, or tools at this time.",
                            "memory_writes": [],
                            "task_writes": [],
                            "agent_writes": [],
                            "routine_writes": [],
                            "tool_calls": [],
                        }
                    ),
                }
            }

        self.app.reasoner.transport = stale_transport

        reply = self.app.conversations.respond(
            ChatRequest(message="can you create me a website for an IT consulting company?", owner_approved=True)
        )

        self.assertEqual(provider_calls, [])
        self.assertEqual(reply.reasoning_mode, "deterministic")
        self.assertEqual(reply.executed_tools[0]["tool_id"], "code.generate_website")
        result = reply.executed_tools[0]["result"]
        self.assertTrue((Path(result["project_dir"]) / "index.html").exists())
        self.assertTrue((Path(result["project_dir"]) / "styles.css").exists())
        self.assertTrue((Path(result["project_dir"]) / "script.js").exists())
        html = (Path(result["project_dir"]) / "index.html").read_text(encoding="utf-8")
        self.assertIn("IT Consulting", html)
        self.assertNotIn("will not create", reply.reply.lower())

    def test_website_request_without_owner_approval_is_blocked_but_not_noop(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(message="create me a website for an IT consulting company")
        )

        self.assertEqual(reply.reasoning_mode, "deterministic")
        self.assertEqual(reply.executed_tools, [])
        self.assertEqual(reply.blocked_tools[0]["tool_id"], "code.generate_website")
        self.assertIn("Blocked pending approval", reply.reply)

    def test_project_build_planner_records_dispatch_for_fullstack_app(self) -> None:
        planner = self.app.tools.get("project.plan_build")

        result = planner.execute(
            {
                "instruction": "build me a CRM dashboard app with login, reports, and client notes",
            }
        )

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["task_type"], "fullstack_app")
        self.assertIn("acceptance_criteria", result)
        self.assertTrue(any("responsive" in item.lower() for item in result["acceptance_criteria"]))
        dispatch = self.app.dispatches.get(result["dispatch_id"])
        self.assertEqual(dispatch["id"], result["dispatch_id"])
        self.assertEqual(dispatch["task_type"], "fullstack_app")
        self.assertIn("CRM", dispatch["project_name"])

    def test_website_generation_records_project_dispatch_artifact(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(message="create me a website for an IT consulting company", owner_approved=True)
        )

        result = reply.executed_tools[0]["result"]
        dispatch = self.app.dispatches.get(result["dispatch_id"])

        self.assertEqual(dispatch["status"], "completed")
        self.assertEqual(dispatch["task_type"], "multipage_website")
        self.assertEqual(dispatch["project_path"], result["project_dir"])
        self.assertTrue(any(item["path"].endswith("index.html") for item in dispatch["artifacts"]))

    def test_website_generator_builds_prompt_aware_multipage_site(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(
                message=(
                    "create a luxury dark website for a barber shop called Crown Fade Studio "
                    "with pricing, gallery, booking, and contact"
                ),
                owner_approved=True,
            )
        )

        result = reply.executed_tools[0]["result"]
        project_dir = Path(result["project_dir"])
        index_html = (project_dir / "index.html").read_text(encoding="utf-8")
        styles_css = (project_dir / "styles.css").read_text(encoding="utf-8")

        self.assertEqual(result["site_kind"], "multipage_website")
        self.assertTrue((project_dir / "pricing.html").exists())
        self.assertTrue((project_dir / "gallery.html").exists())
        self.assertTrue((project_dir / "booking.html").exists())
        self.assertIn("Crown Fade Studio", index_html)
        self.assertIn("Barber Shop", index_html)
        self.assertIn("Book a Chair", index_html)
        self.assertIn("--accent: #d7b46a", styles_css)
        self.assertNotIn("Apex Signal IT", index_html)

    def test_website_generator_builds_static_web_app_when_prompt_asks_for_app(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(
                message="build a CRM web app with login, dashboard, reports, and client notes",
                owner_approved=True,
            )
        )

        result = reply.executed_tools[0]["result"]
        project_dir = Path(result["project_dir"])
        index_html = (project_dir / "index.html").read_text(encoding="utf-8")
        app_js = (project_dir / "app.js").read_text(encoding="utf-8")
        dispatch = self.app.dispatches.get(result["dispatch_id"])

        self.assertEqual(result["site_kind"], "static_web_app")
        self.assertEqual(dispatch["task_type"], "fullstack_app")
        self.assertTrue((project_dir / "app.js").exists())
        self.assertIn("Client Notes", index_html)
        self.assertIn("Reports", index_html)
        self.assertIn("demoLogin", app_js)

    def test_website_generator_uses_distinct_output_dirs_for_distinct_prompts(self) -> None:
        tool = self.app.tools.get("code.generate_website")

        barber = tool.execute(
            {"instruction": "create a luxury website for Crown Fade barber shop with booking"}
        )
        restaurant = tool.execute(
            {"instruction": "create a warm restaurant website for Saffron Table with menu and reservations"}
        )

        self.assertNotEqual(barber["project_dir"], restaurant["project_dir"])
        self.assertIn("crown-fade", barber["project_dir"].lower())
        self.assertIn("saffron-table", restaurant["project_dir"].lower())

    def test_typo_tolerant_website_request_routes_to_generator(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(
                message=(
                    "creat a luxry dark webiste for a barber shpo called Crown Fade "
                    "with pricng, gallerie, bokking and contct"
                ),
                owner_approved=True,
            )
        )

        result = reply.executed_tools[0]["result"]
        project_dir = Path(result["project_dir"])

        self.assertEqual(reply.executed_tools[0]["tool_id"], "code.generate_website")
        self.assertEqual(result["site_kind"], "multipage_website")
        self.assertTrue((project_dir / "pricing.html").exists())
        self.assertTrue((project_dir / "booking.html").exists())
        self.assertIn("Crown Fade", (project_dir / "index.html").read_text(encoding="utf-8"))

    def test_typo_tolerant_script_request_generates_project(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(
                message="buid a pyhton scipt that scan this repo and lists todo commnets",
                owner_approved=True,
            )
        )

        result = reply.executed_tools[0]["result"]
        main_py = (Path(result["project_dir"]) / "src" / "main.py").read_text(encoding="utf-8")

        self.assertEqual(reply.executed_tools[0]["tool_id"], "code.generate_project")
        self.assertEqual(result["project_type"], "script_tool")
        self.assertIn("TODO", main_py)
        self.assertIn("Path", main_py)

    def test_provider_noop_for_actionable_request_falls_back_to_local_dispatch(self) -> None:
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="ollama",
                model_name="qwen3.5:9b",
                ollama_model_routing_enabled=True,
                ollama_general_model="qwen3.5:9b",
                ollama_coding_model="qwen2.5-coder:7b",
                ollama_reasoning_model="deepseek-r1:7b",
                ollama_fast_model="llama3.1:8b",
                model_base_url="http://localhost:11434/api/chat",
                model_secret_name="",
            )
        )
        provider_calls: list[bytes] = []

        def noop_transport(_url, body, _headers, _timeout_seconds):
            provider_calls.append(body)
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "Understood. I will not create any new memory, tasks, agents, routines, or tools at this time.",
                            "memory_writes": [],
                            "task_writes": [],
                            "agent_writes": [],
                            "routine_writes": [],
                            "tool_calls": [],
                        }
                    ),
                }
            }

        self.app.reasoner.transport = noop_transport

        reply = self.app.conversations.respond(
            ChatRequest(message="prepare a client onboarding checklist for my consulting business")
        )

        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(reply.reasoning_mode, "local-fallback")
        self.assertNotIn("will not create", reply.reply.lower())
        self.assertTrue(reply.executed_tools or reply.created_task_ids)

    def test_provider_noop_for_app_build_request_falls_back_to_project_dispatch(self) -> None:
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="ollama",
                model_name="qwen3.5:9b",
                ollama_model_routing_enabled=True,
                ollama_general_model="qwen3.5:9b",
                ollama_coding_model="qwen2.5-coder:7b",
                ollama_reasoning_model="deepseek-r1:7b",
                ollama_fast_model="llama3.1:8b",
                model_base_url="http://localhost:11434/api/chat",
                model_secret_name="",
            )
        )

        def noop_transport(_url, _body, _headers, _timeout_seconds):
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "Understood. I will not create any new memory, tasks, agents, routines, or tools at this time.",
                            "memory_writes": [],
                            "task_writes": [],
                            "agent_writes": [],
                            "routine_writes": [],
                            "tool_calls": [],
                        }
                    ),
                }
            }

        self.app.reasoner.transport = noop_transport

        reply = self.app.conversations.respond(
            ChatRequest(message="build me a CRM dashboard app with login and reports", owner_approved=True)
        )

        self.assertEqual(reply.reasoning_mode, "local-fallback")
        self.assertEqual(reply.executed_tools[0]["tool_id"], "code.generate_project")
        self.assertEqual(reply.executed_tools[0]["result"]["project_type"], "fullstack_app")

    def test_chat_build_script_generates_real_python_project(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(
                message="build a Python script that scans this repo and lists every TODO comment",
                owner_approved=True,
            )
        )

        result = reply.executed_tools[0]["result"]
        project_dir = Path(result["project_dir"])
        main_py = (project_dir / "src" / "main.py").read_text(encoding="utf-8")

        self.assertEqual(reply.executed_tools[0]["tool_id"], "code.generate_project")
        self.assertEqual(result["project_type"], "script_tool")
        self.assertTrue((project_dir / "README.md").exists())
        self.assertIn("Path", main_py)
        self.assertIn("TODO", main_py)
        self.assertNotIn("{workspace_root}", main_py)

    def test_chat_build_api_generates_real_api_project(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(
                message="build an API for tracking tasks with a health check and JSON endpoints",
                owner_approved=True,
            )
        )

        result = reply.executed_tools[0]["result"]
        project_dir = Path(result["project_dir"])
        app_py = (project_dir / "app.py").read_text(encoding="utf-8")
        readme = (project_dir / "README.md").read_text(encoding="utf-8")

        self.assertEqual(reply.executed_tools[0]["tool_id"], "code.generate_project")
        self.assertEqual(result["project_type"], "api")
        self.assertIn("/health", app_py)
        self.assertIn("HTTPServer", app_py)
        self.assertIn("python app.py", readme)

    def test_project_generation_without_owner_approval_is_blocked_but_not_noop(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(message="build a Python script that scans this repo for TODO comments")
        )

        self.assertEqual(reply.executed_tools, [])
        self.assertEqual(reply.blocked_tools[0]["tool_id"], "code.generate_project")
        self.assertIn("Blocked pending approval", reply.reply)

    def test_shell_tool_can_run_from_owner_configured_roots(self) -> None:
        owner_root = self.root.parent / f"shell-root-{uuid.uuid4().hex}"
        owner_root.mkdir(parents=True, exist_ok=True)
        try:
            self.app.settings.update(SettingsUpdate(file_access_roots=[str(owner_root)]))

            shell_tool = self.app.tools.get("shell.run_command")
            result = shell_tool.execute(
                {
                    "command": "(Get-Location).Path",
                    "workdir": str(owner_root),
                    "timeout_seconds": 5,
                }
            )

            self.assertEqual(result["returncode"], 0)
            self.assertEqual(Path(result["stdout"].strip()).resolve(), owner_root.resolve())
        finally:
            shutil.rmtree(owner_root, ignore_errors=True)

    def test_learning_lab_run_once_records_findings_and_creates_memory(self) -> None:
        result = self.app.learning.run_once(reason="idle simulation")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["mode"], "run_once")
        self.assertGreaterEqual(len(result["findings"]), 1)
        self.assertGreaterEqual(len(result["created_memory_ids"]), 1)
        runs = self.app.learning.list_runs()
        self.assertEqual(runs[0]["id"], result["id"])
        self.assertTrue(any(memory["source"] == "learning_lab" for memory in self.app.memory.list_all()))

    def test_learning_lab_start_and_stop_updates_status(self) -> None:
        start = self.app.learning.start(max_cycles=1, interval_seconds=1)
        self.assertTrue(start["running"])

        stop = self.app.learning.stop()
        self.assertFalse(stop["running"])

    def test_self_diagnostics_run_records_code_generation_probe(self) -> None:
        result = self.app.diagnostics.run(source="test")

        self.assertEqual(result["status"], "completed")
        check_names = {check["name"] for check in result["checks"]}
        self.assertIn("tool_registry", check_names)
        self.assertIn("code_generation_probe", check_names)
        code_probe = next(check for check in result["checks"] if check["name"] == "code_generation_probe")
        self.assertEqual(code_probe["status"], "passed")
        self.assertTrue(Path(code_probe["artifact_path"]).exists())
        self.assertGreaterEqual(len(result["created_memory_ids"]), 1)

    def test_self_diagnostics_turns_recent_failures_into_repair_tasks(self) -> None:
        self.app.audit.log(
            action_type="tool_execution",
            action_tier=2,
            tool_name="filesystem.write_file",
            outcome="failed",
            error="simulated file write failure",
            metadata={"path": "notes/broken.py"},
        )

        result = self.app.diagnostics.run(source="test")

        self.assertGreaterEqual(len(result["created_task_ids"]), 1)
        created_tasks = [self.app.tasks.get(task_id) for task_id in result["created_task_ids"]]
        self.assertTrue(any("Investigate Project Q failure" in task["title"] for task in created_tasks))

    def test_diagnostics_auto_resolves_transient_http_server_disconnect_noise(self) -> None:
        noisy_task = self.app.tasks.create(
            TaskCreate(
                title="Investigate Project Q failure: http_server",
                description="http_server ended as failed: [WinError 10053] An established connection was aborted by the software in your host machine",
                priority=2,
                source="self_diagnostics",
            )
        )
        self.app.audit.log(
            action_type="server_error",
            action_tier=0,
            tool_name="http_server",
            outcome="failed",
            error="[WinError 10053] An established connection was aborted by the software in your host machine",
        )

        result = self.app.diagnostics.run(source="test")

        self.assertEqual(self.app.tasks.get(noisy_task["id"])["status"], "completed")
        self.assertEqual(result["created_task_ids"], [])
        self.assertTrue(any(repair["kind"] == "transient_http_disconnect" for repair in result["repair"]["repairs"]))

    def test_self_diagnostics_tool_is_available_to_project_q(self) -> None:
        tool = self.app.tools.get("diagnostics.run_self_check")
        result = tool.execute({"source": "tool-test"})

        self.assertEqual(result["status"], "completed")
        self.assertTrue(any(check["name"] == "code_generation_probe" for check in result["checks"]))

    def test_chat_can_run_self_diagnostics_heuristically(self) -> None:
        reply = self.app.conversations.respond(ChatRequest(message="run diagnostics on yourself and troubleshoot issues"))

        self.assertEqual(len(reply.executed_tools), 1)
        self.assertEqual(reply.executed_tools[0]["tool_id"], "diagnostics.run_self_check")
        self.assertEqual(reply.executed_tools[0]["result"]["status"], "completed")

    def test_learning_lab_includes_self_diagnostics_summary(self) -> None:
        result = self.app.learning.run_once(reason="diagnostic simulation")

        self.assertTrue(any(finding["kind"] == "self_diagnostics" for finding in result["findings"]))

    def test_diagnostics_auto_repairs_known_reasoner_payload_failure(self) -> None:
        task = self.app.tasks.create(
            TaskCreate(
                title="Investigate Project Q failure: ollama_reasoner",
                description="ReasonerPlan tool_calls.0.payload Input should be a valid dictionary",
                priority=2,
                source="self_diagnostics",
                metadata={"finding_kind": "recent_failure"},
            )
        )
        self.app.audit.log(
            action_type="reasoner_plan",
            action_tier=0,
            tool_name="ollama_reasoner",
            outcome="failed",
            error=(
                "1 validation error for ReasonerPlan\n"
                "tool_calls.0.payload\n"
                "Input should be a valid dictionary [type=dict_type, input_value='python -c ...']"
            ),
        )

        result = self.app.diagnostics.run(source="test")

        repaired_task = self.app.tasks.get(task["id"])
        self.assertEqual(repaired_task["status"], "completed")
        self.assertIn("repair", result)
        self.assertTrue(result["repair"]["repairs"])
        self.assertTrue(any(memory["source"] == "self_repair" for memory in self.app.memory.list_all()))

    def test_learning_lab_auto_repairs_expected_policy_blocks_without_pending_noise(self) -> None:
        self.app.audit.log(
            action_type="tool_execution",
            action_tier=2,
            tool_name="windows.launch_application",
            outcome="blocked",
            metadata={
                "reason": "tier 2 requires owner approval",
                "payload": {"app_name": "Browser", "app_path": "explorer.exe"},
            },
        )

        result = self.app.learning.run_once(reason="policy block simulation")

        pending_window_tasks = [
            task
            for task in self.app.tasks.list_all(limit=100)
            if task["status"] == "pending" and "windows.launch_application" in task["title"]
        ]
        self.assertEqual(pending_window_tasks, [])
        self.assertTrue(any(finding["kind"] == "self_repair" for finding in result["findings"]))
        self_repair_memory_count = len(
            [memory for memory in self.app.memory.list_all(limit=100) if memory["source"] == "self_repair"]
        )

        second_result = self.app.learning.run_once(reason="policy block simulation repeat")

        self.assertEqual(
            len([memory for memory in self.app.memory.list_all(limit=100) if memory["source"] == "self_repair"]),
            self_repair_memory_count,
        )
        self.assertFalse(any(finding["kind"] == "self_repair" for finding in second_result["findings"]))

    def test_training_export_dataset_creates_local_weight_training_assets(self) -> None:
        self.app.conversations.respond(ChatRequest(message="remember I prefer direct coding-first answers"))

        result = self.app.training.export_dataset(reason="test")

        sft_path = Path(result["sft_dataset_path"])
        preference_path = Path(result["preference_dataset_path"])
        lora_script_path = Path(result["lora_script_path"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(sft_path.exists())
        self.assertTrue(preference_path.exists())
        self.assertTrue(lora_script_path.exists())
        first_record = json.loads(sft_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("messages", first_record)
        self.assertIn("next_step", result)
        self.assertGreater(result["sft_record_count"], 50)

    def test_training_prepare_lora_job_creates_weight_training_job(self) -> None:
        self.app.conversations.respond(ChatRequest(message="remember route typo-heavy coding requests to tools"))

        result = self.app.training.prepare_lora_job(
            reason="test",
            base_model="Qwen/Qwen2.5-Coder-1.5B-Instruct",
        )

        job_dir = Path(result["job_dir"])
        train_script = job_dir / "train_lora.py"
        config_path = job_dir / "config.json"

        self.assertEqual(result["status"], "ready")
        self.assertTrue(train_script.exists())
        self.assertTrue(config_path.exists())
        self.assertIn("LoraConfig", train_script.read_text(encoding="utf-8"))
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["base_model"], "Qwen/Qwen2.5-Coder-1.5B-Instruct")
        self.assertIn("sft_dataset_path", config)

    def test_training_prepare_lora_job_creates_full_finetune_lifecycle_assets(self) -> None:
        result = self.app.training.prepare_lora_job(reason="test")

        job_dir = Path(result["job_dir"])

        self.assertTrue((job_dir / "requirements-training.txt").exists())
        self.assertTrue((job_dir / "evaluate_adapter.py").exists())
        self.assertTrue((job_dir / "merge_lora.py").exists())
        self.assertTrue((job_dir / "Modelfile.template").exists())
        self.assertTrue((job_dir / "validate_job.py").exists())
        self.assertTrue((job_dir / "setup_training_env.ps1").exists())
        self.assertIn("python .\\validate_job.py", (job_dir / "README.md").read_text(encoding="utf-8"))
        eval_script = (job_dir / "evaluate_adapter.py").read_text(encoding="utf-8")
        self.assertIn("TOOL_IDS", eval_script)
        self.assertIn("apply_chat_template", eval_script)
        config = json.loads((job_dir / "config.json").read_text(encoding="utf-8"))
        self.assertIn("python_executable", config)
        self.assertIn("training_env_dir", config)
        run_script = (job_dir / "run_training.ps1").read_text(encoding="utf-8")
        self.assertIn("& $Python", run_script)
        self.assertIn("PYTHONUTF8", run_script)

    def test_training_capability_plan_explains_chatgpt_codex_claude_path(self) -> None:
        result = self.app.training.capability_plan()

        self.assertEqual(result["status"], "ready")
        self.assertIn("fine_tuning", result)
        self.assertIn("agentic_tool_use", result)
        self.assertIn("evals", result)
        self.assertTrue(any("qwen" in item["model"].lower() for item in result["recommended_local_models"]))

    def test_chat_training_weight_request_routes_to_lora_job(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(
                message="train yurself with a local lora job so you can update model weights",
                owner_approved=True,
            )
        )

        self.assertEqual(reply.executed_tools[0]["tool_id"], "training.prepare_lora_job")
        self.assertEqual(reply.executed_tools[0]["result"]["status"], "ready")
        self.assertIn("train_lora.py", reply.reply)

    def test_chat_how_to_become_chatgpt_codex_claude_routes_capability_plan(self) -> None:
        reply = self.app.conversations.respond(
            ChatRequest(message="how can Project Q become ChatGPT Codex and Claude level?")
        )

        self.assertEqual(reply.executed_tools[0]["tool_id"], "training.capability_plan")
        self.assertIn("agentic_tool_use", reply.executed_tools[0]["result"])
        self.assertIn("ChatGPT", reply.reply)

    def test_artifact_validator_repairs_windows_path_literals(self) -> None:
        validator = ArtifactValidationService()
        script_path = self.root / "notes" / "scanner.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            "root = 'C:\\Users\\umarq.APEX\\Documents\\Jarvis'\nprint(root)\n",
            encoding="utf-8",
        )

        result = validator.validate_written_file(script_path)

        self.assertEqual(result["status"], "repaired")
        repaired = script_path.read_text(encoding="utf-8")
        self.assertIn("C:/Users/umarq.APEX/Documents/Jarvis", repaired)

    def test_artifact_validator_repairs_fstring_path_separator(self) -> None:
        validator = ArtifactValidationService()
        script_path = self.root / "notes" / "scanner_warn.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            "root = 'repo'\nfile = 'demo.py'\nprint(f'{root}\\{file}')\n",
            encoding="utf-8",
        )

        result = validator.validate_written_file(script_path)

        self.assertEqual(result["status"], "repaired")
        repaired = script_path.read_text(encoding="utf-8")
        self.assertIn("f'{root}/{file}'", repaired)

    def test_artifact_validator_repairs_workspace_root_placeholder(self) -> None:
        validator = ArtifactValidationService()
        script_path = self.root / "notes" / "scanner_placeholder.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            "import os\n"
            "def scan_repository(directory):\n"
            "    return []\n"
            "if __name__ == '__main__':\n"
            "    results = scan_repository('{workspace_root}')\n"
            "    print(results)\n",
            encoding="utf-8",
        )

        result = validator.validate_written_file(script_path)

        self.assertEqual(result["status"], "repaired")
        repaired = script_path.read_text(encoding="utf-8")
        self.assertIn("Path(__file__).resolve().parents[1]", repaired)

    def test_executor_reports_invalid_generated_python(self) -> None:
        plan = ReasonerPlan.model_validate(
            {
                "reply": "Write a broken script.",
                "memory_writes": [],
                "task_writes": [],
                "agent_writes": [],
                "routine_writes": [],
                "tool_calls": [
                    {
                        "tool_id": "filesystem.write_file",
                        "reason": "Create a script file.",
                        "payload": {
                            "path": "notes/broken.py",
                            "content": "print('hello'\n",
                        },
                    }
                ],
            }
        )

        execution = self.app.executor.execute(
            plan=plan,
            owner_approved=True,
            input_sources=["owner", "reasoner"],
        )

        validation = execution["executed_tools"][0]["result"]["validation"]
        self.assertEqual(validation["status"], "invalid")
        self.assertIn("was never closed", validation["message"])

    def test_executor_can_repair_invalid_generated_python_with_model(self) -> None:
        self.app.executor.code_repair_service = FakeCodeRepairService()
        plan = ReasonerPlan.model_validate(
            {
                "reply": "Write a broken script.",
                "memory_writes": [],
                "task_writes": [],
                "agent_writes": [],
                "routine_writes": [],
                "tool_calls": [
                    {
                        "tool_id": "filesystem.write_file",
                        "reason": "Create a script file.",
                        "payload": {
                            "path": "notes/broken_repaired.py",
                            "content": "print(f'{root}/{file}'\n",
                        },
                    }
                ],
            }
        )

        execution = self.app.executor.execute(
            plan=plan,
            owner_approved=True,
            input_sources=["owner", "reasoner"],
        )

        validation = execution["executed_tools"][0]["result"]["validation"]
        self.assertEqual(validation["status"], "repaired_with_model")
        self.assertEqual(validation["model_name"], "qwen2.5-coder:7b")

    def test_executor_can_repair_runtime_invalid_generated_python_with_model(self) -> None:
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "demo.py").write_text("# TODO: demo\nprint('demo')\n", encoding="utf-8")
        self.app.executor.code_repair_service = FakeRuntimeCodeRepairService()
        plan = ReasonerPlan.model_validate(
            {
                "reply": "Write a broken runtime script.",
                "memory_writes": [],
                "task_writes": [],
                "agent_writes": [],
                "routine_writes": [],
                "tool_calls": [
                    {
                        "tool_id": "filesystem.write_file",
                        "reason": "Create a script file.",
                        "payload": {
                            "path": "notes/runtime_broken.py",
                            "content": (
                                "from pathlib import Path\n\n"
                                "def scan_todo_comments(root_dir):\n"
                                "    for root, _, files in Path(root_dir).rglob('*'):\n"
                                "        if not root.is_file():\n"
                                "            continue\n"
                                "        file_path = root.resolve()\n"
                                "        if file_path.suffix in {'.py', '.html'}:\n"
                                "            print(file_path)\n\n"
                                "if __name__ == '__main__':\n"
                                "    scan_todo_comments('.')\n"
                            ),
                        },
                    }
                ],
            }
        )

        execution = self.app.executor.execute(
            plan=plan,
            owner_approved=True,
            input_sources=["owner", "reasoner"],
        )

        validation = execution["executed_tools"][0]["result"]["validation"]
        self.assertEqual(validation["status"], "repaired_with_model")
        self.assertEqual(validation["model_name"], "qwen2.5-coder:7b")

    def test_reasoner_routine_plan_normalizes_custom_step_labels(self) -> None:
        plan = ReasonerPlan.model_validate(
            {
                "reply": "Save a reusable routine.",
                "memory_writes": [],
                "task_writes": [],
                "agent_writes": [],
                "routine_writes": [
                    {
                        "name": "Todo scanner routine",
                        "goal": "Create a todo scanner file",
                        "description": "Generated by a model",
                        "status": "active",
                        "trigger_type": "manual",
                        "trusted": False,
                        "tools": ["filesystem.write_file"],
                        "steps": [
                            {
                                "step_type": "WriteResults",
                                "label": "Write the Python file",
                                "tool_id": "filesystem.write_file",
                                "payload": {"path": "notes/demo.py", "content": "print('ok')\n"},
                                "delay_seconds": 0,
                                "continue_on_error": False,
                                "requires_owner_approval": True,
                            }
                        ],
                        "notes": ["first", "second"],
                    }
                ],
                "tool_calls": [],
            }
        )

        routine = plan.routine_writes[0]
        self.assertEqual(routine.steps[0].step_type, "tool")
        self.assertIsNone(routine.steps[0].delay_seconds)
        self.assertEqual(routine.notes, "first\nsecond")

    def test_vault_round_trip(self) -> None:
        self.app.vault.set_secret("demo-token", "super-secret", "test credential")
        recovered = self.app.vault.get_secret("demo-token")
        listed = self.app.vault.list_secret_names()

        self.assertEqual(recovered, "super-secret")
        self.assertEqual(listed[0]["name"], "demo-token")

    def test_application_runtime_metadata_round_trip(self) -> None:
        self.app.write_runtime_metadata()
        self.assertTrue(self.app.runtime_file.exists())
        metadata = json.loads(self.app.runtime_file.read_text(encoding="utf-8"))
        self.assertEqual(metadata["pid"], self.app.process_id)
        self.assertEqual(metadata["build_id"], self.app.build_id)
        self.app.clear_runtime_metadata()
        self.assertFalse(self.app.runtime_file.exists())

    def test_remote_reasoner_uses_provider_response(self) -> None:
        self.app.vault.set_secret("openai_api_key", "test-key", "provider secret")
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="openai_responses",
                model_name="demo-model",
                model_base_url="https://api.openai.com/v1/responses",
                model_secret_name="openai_api_key",
            )
        )

        def fake_transport(_url, _body, headers, _timeout_seconds):
            self.assertEqual(headers["Authorization"], "Bearer test-key")
            return {
                "output_text": json.dumps(
                    {
                        "reply": "Remote provider handled the request.",
                        "memory_writes": [{"text": "Owner prefers fast output", "kind": "semantic", "confidence": 0.88}],
                        "task_writes": [],
                        "agent_writes": [],
                        "tool_calls": [],
                    }
                )
            }

        reasoner = ReasonerService(self.app.settings, self.app.vault, self.app.audit, transport=fake_transport)
        result = reasoner.plan(user_message="remember that I like fast output", context=self.app.context.build("test"))

        self.assertEqual(result.mode, "remote")
        self.assertEqual(result.plan.reply, "Remote provider handled the request.")
        self.assertEqual(result.plan.memory_writes[0].text, "Owner prefers fast output")

    def test_ollama_reasoner_uses_local_chat_response(self) -> None:
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="ollama",
                model_name="qwen3.5:9b",
                ollama_model_routing_enabled=True,
                ollama_general_model="qwen3.5:9b",
                ollama_coding_model="qwen2.5-coder:7b",
                ollama_reasoning_model="deepseek-r1:7b",
                ollama_fast_model="llama3.1:8b",
                model_base_url="http://localhost:11434/api/chat",
                model_secret_name="",
            )
        )

        def fake_transport(_url, body, headers, _timeout_seconds):
            self.assertEqual(headers["Content-Type"], "application/json")
            request_payload = json.loads(body.decode("utf-8"))
            self.assertEqual(request_payload["model"], "qwen3.5:9b")
            self.assertFalse(request_payload["think"])
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "Local Ollama handled the request.",
                            "memory_writes": [],
                            "task_writes": [{"title": "Inspect docs", "description": "Use local reasoning", "priority": 2}],
                            "agent_writes": [],
                            "tool_calls": [],
                        }
                    ),
                }
            }

        def fake_get_transport(_url, _headers, _timeout_seconds):
            return {
                "models": [
                    {
                        "name": "qwen3.5:9b",
                        "details": {
                            "family": "qwen3.5",
                            "parameter_size": "9B",
                            "quantization_level": "Q4_K_M",
                        },
                    }
                ]
            }

        reasoner = ReasonerService(
            self.app.settings,
            self.app.vault,
            self.app.audit,
            transport=fake_transport,
            get_transport=fake_get_transport,
        )
        result = reasoner.plan(user_message="help me inspect docs", context=self.app.context.build("test"))
        available_models = reasoner.list_available_models()

        self.assertEqual(result.mode, "local-model")
        self.assertEqual(result.plan.reply, "Local Ollama handled the request.")
        self.assertEqual(result.plan.task_writes[0].title, "Inspect docs")
        self.assertEqual(available_models["items"][0]["name"], "qwen3.5:9b")

    def test_ollama_reasoner_routes_coding_requests_to_coding_model(self) -> None:
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="ollama",
                model_name="qwen3.5:9b",
                ollama_model_routing_enabled=True,
                ollama_general_model="qwen3.5:9b",
                ollama_coding_model="qwen2.5-coder:7b",
                ollama_reasoning_model="deepseek-r1:7b",
                ollama_fast_model="llama3.1:8b",
                model_base_url="http://localhost:11434/api/chat",
                model_secret_name="",
            )
        )

        def fake_transport(_url, body, _headers, _timeout_seconds):
            request_payload = json.loads(body.decode("utf-8"))
            self.assertEqual(request_payload["model"], "qwen2.5-coder:7b")
            self.assertFalse(request_payload["think"])
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "Coding model handled the request.",
                            "memory_writes": [],
                            "task_writes": [],
                            "agent_writes": [],
                            "routine_writes": [],
                            "tool_calls": [],
                        }
                    ),
                }
            }

        reasoner = ReasonerService(self.app.settings, self.app.vault, self.app.audit, transport=fake_transport)
        result = reasoner.plan(
            user_message="Build a Python script that refactors this repo",
            context=self.app.context.build("test"),
        )

        self.assertEqual(result.model_name, "qwen2.5-coder:7b")
        self.assertEqual(result.plan.reply, "Coding model handled the request.")

    def test_ollama_reasoner_normalizes_string_task_priority(self) -> None:
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="ollama",
                model_name="qwen2.5-coder:7b",
                ollama_model_routing_enabled=True,
                ollama_general_model="qwen3.5:9b",
                ollama_coding_model="qwen2.5-coder:7b",
                ollama_reasoning_model="deepseek-r1:7b",
                ollama_fast_model="llama3.1:8b",
                model_base_url="http://localhost:11434/api/chat",
                model_secret_name="",
            )
        )

        def fake_transport(_url, body, _headers, _timeout_seconds):
            request_payload = json.loads(body.decode("utf-8"))
            self.assertEqual(request_payload["model"], "qwen2.5-coder:7b")
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "Coding task parsed correctly.",
                            "memory_writes": [],
                            "task_writes": [
                                {
                                    "title": "Scan TODO comments",
                                    "description": "Create a repo TODO scanner.",
                                    "priority": "High",
                                }
                            ],
                            "agent_writes": [],
                            "routine_writes": [],
                            "tool_calls": [],
                        }
                    ),
                }
            }

        reasoner = ReasonerService(self.app.settings, self.app.vault, self.app.audit, transport=fake_transport)
        result = reasoner.plan(
            user_message="Build a script that scans this repo and lists every TODO comment",
            context=self.app.context.build("test"),
        )

        self.assertEqual(result.plan.task_writes[0].priority, 1)

    def test_ollama_reasoner_normalizes_string_tool_payload_for_shell(self) -> None:
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="ollama",
                model_name="qwen2.5-coder:7b",
                ollama_model_routing_enabled=True,
                ollama_general_model="qwen3.5:9b",
                ollama_coding_model="qwen2.5-coder:7b",
                ollama_reasoning_model="deepseek-r1:7b",
                ollama_fast_model="llama3.1:8b",
                model_base_url="http://localhost:11434/api/chat",
                model_secret_name="",
            )
        )

        def fake_transport(_url, body, _headers, _timeout_seconds):
            request_payload = json.loads(body.decode("utf-8"))
            self.assertEqual(request_payload["model"], "qwen2.5-coder:7b")
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "Use a shell command to inspect the repo.",
                            "memory_writes": [],
                            "task_writes": [],
                            "agent_writes": [],
                            "routine_writes": [],
                            "tool_calls": [
                                {
                                    "tool_id": "shell.run_command",
                                    "reason": "Search for TODO comments in the repo.",
                                    "payload": "Get-ChildItem -Recurse | Select-String -Pattern 'TODO'",
                                }
                            ],
                        }
                    ),
                }
            }

        reasoner = ReasonerService(self.app.settings, self.app.vault, self.app.audit, transport=fake_transport)
        result = reasoner.plan(
            user_message="Build a script that scans this repo and lists every TODO comment",
            context=self.app.context.build("test"),
        )

        self.assertEqual(result.plan.tool_calls[0].tool_id, "shell.run_command")
        self.assertEqual(
            result.plan.tool_calls[0].payload["command"],
            "Get-ChildItem -Recurse | Select-String -Pattern 'TODO'",
        )

    def test_ollama_reasoner_normalizes_visible_browser_requests_to_open_url(self) -> None:
        self.app.settings.update(
            SettingsUpdate(
                provider_enabled=True,
                provider_type="ollama",
                model_name="qwen3.5:9b",
                ollama_model_routing_enabled=True,
                ollama_general_model="qwen3.5:9b",
                ollama_coding_model="qwen2.5-coder:7b",
                ollama_reasoning_model="deepseek-r1:7b",
                ollama_fast_model="llama3.1:8b",
                model_base_url="http://localhost:11434/api/chat",
                model_secret_name="",
            )
        )

        def fake_transport(_url, body, _headers, _timeout_seconds):
            request_payload = json.loads(body.decode("utf-8"))
            self.assertEqual(request_payload["model"], "qwen3.5:9b")
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "I will use the browser automation path.",
                            "memory_writes": [],
                            "task_writes": [],
                            "agent_writes": [],
                            "routine_writes": [],
                            "tool_calls": [
                                {
                                    "tool_id": "browser.run_actions",
                                    "reason": "Search in a browser tab.",
                                    "payload": {
                                        "url": "https://www.google.com",
                                        "actions": [
                                            {"type": "fill", "selector": "input[name='q']", "value": "invincible"}
                                        ],
                                    },
                                }
                            ],
                        }
                    ),
                }
            }

        reasoner = ReasonerService(self.app.settings, self.app.vault, self.app.audit, transport=fake_transport)
        result = reasoner.plan(
            user_message="open the browser and search google for invincible",
            context=self.app.context.build("test"),
        )

        self.assertEqual(result.plan.tool_calls[0].tool_id, "windows.open_url")
        self.assertEqual(result.plan.tool_calls[0].payload["url"], "https://www.google.com/search?q=invincible")

    def test_agent_runner_creates_run_record(self) -> None:
        self.app.tools.tools["browser.inspect_page"] = FakeBrowserInspectTool()
        agent = self.app.agents.create(
            AgentCreate(
                name="Research Helper",
                agent_type="research",
                goal="Summarize https://example.com and create any useful follow-up tasks.",
                status="active",
                tools=["browser.inspect_page"],
            )
        )

        run = self.app.agent_runner.run(agent["id"], owner_approved=False)
        refreshed_agent = self.app.agents.get(agent["id"])

        self.assertEqual(run["agent_id"], agent["id"])
        self.assertEqual(run["outcome"], "completed")
        self.assertEqual(refreshed_agent["last_run_outcome"], "completed")
        self.assertEqual(refreshed_agent["status"], "active")
        self.assertTrue(run["reply"])

    def test_trusted_routine_can_run_tier_two_tool(self) -> None:
        routine = self.app.routines.create(
            RoutineCreate(
                name="Write marker file",
                goal="Create a marker file for the workspace.",
                trusted=True,
                steps=[
                    {
                        "step_type": "tool",
                        "label": "Write marker file",
                        "tool_id": "filesystem.write_file",
                        "payload": {"path": "notes/routine.txt", "content": "routine"},
                        "continue_on_error": False,
                        "requires_owner_approval": False,
                    }
                ],
            )
        )

        run = self.app.routine_runner.run(routine["id"], owner_approved=False)
        refreshed_routine = self.app.routines.get(routine["id"])

        self.assertEqual(run["outcome"], "completed")
        self.assertEqual(refreshed_routine["last_run_outcome"], "completed")
        self.assertEqual((self.root / "notes" / "routine.txt").read_text(encoding="utf-8"), "routine")

    def test_browser_tool_builds_worker_command(self) -> None:
        tool = BrowserActionsTool(self.root / ".project_q")

        with patch("project_q.tools.browser.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps({"mode": "actions", "title": "Example"})
            mock_run.return_value.stderr = ""

            result = tool.execute(
                {
                    "url": "https://example.com",
                    "actions": [{"type": "extract_text", "selector": "body"}],
                }
            )

        called_args = mock_run.call_args.args[0]
        self.assertTrue(called_args[1].endswith("browser_worker.js"))
        payload = json.loads(called_args[2])
        self.assertEqual(payload["mode"], "actions")
        self.assertEqual(result["title"], "Example")

    def test_windows_list_tool_builds_encoded_powershell_command(self) -> None:
        tool = WindowsListWindowsTool()

        with patch("project_q.tools.windows.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps({"windows": []})
            mock_run.return_value.stderr = ""

            result = tool.execute({"limit": 5})

        called_args = mock_run.call_args.args[0]
        self.assertEqual(called_args[2], "-EncodedCommand")
        self.assertEqual(result["windows"], [])

    def test_windows_open_url_builds_start_process_command(self) -> None:
        tool = WindowsOpenUrlTool()

        with patch("project_q.tools.windows.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps({"opened": True, "url": "https://example.com"})
            mock_run.return_value.stderr = ""

            result = tool.execute({"url": "https://example.com"})

        called_args = mock_run.call_args.args[0]
        self.assertEqual(called_args[2], "-EncodedCommand")
        self.assertTrue(result["opened"])


if __name__ == "__main__":
    unittest.main()
