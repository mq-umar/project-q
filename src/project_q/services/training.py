from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from project_q.models import MemoryCreate, utc_now


class TrainingService:
    def __init__(self, db, memory_service, task_service, audit_service, data_root: Path) -> None:
        self.db = db
        self.memory_service = memory_service
        self.task_service = task_service
        self.audit_service = audit_service
        self.data_root = data_root

    def capability_plan(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "summary": (
                "Project Q gets closer to ChatGPT, Codex, and Claude quality through a system loop: "
                "strong base models, tool use, retrieval memory, evals, self-repair, and targeted fine-tuning."
            ),
            "local_ceiling": (
                "Fine-tuning a small local model will not create frontier-model intelligence by itself. "
                "It can improve Project Q's style, routing, tool discipline, typo tolerance, and domain habits."
            ),
            "recommended_local_models": [
                {
                    "model": "Qwen2.5-Coder 7B Instruct",
                    "role": "coding and tool-use behavior",
                    "why": "Best first target for local Project Q coding behavior on an 8 GB GPU using QLoRA-style settings.",
                },
                {
                    "model": "Qwen/Qwen2.5-Coder-1.5B-Instruct",
                    "role": "first LoRA smoke test",
                    "why": "Small enough to validate the full training pipeline quickly before risking long GPU runs.",
                },
                {
                    "model": "DeepSeek-R1 Distill 7B class",
                    "role": "reasoning lane",
                    "why": "Useful as a separate local reasoning model while Project Q routes coding to a coder model.",
                },
            ],
            "agentic_tool_use": [
                "Keep deterministic routing for file, browser, spreadsheet, website, codegen, and training requests.",
                "Use local/remote models for planning, but execute actions through audited tools.",
                "Prefer eval-proven tool calls over free-form answers for computer-control tasks.",
            ],
            "fine_tuning": [
                "Use exported Project Q conversations as supervised fine-tuning records.",
                "Use preference records for mistakes Project Q should avoid, such as stale-memory overrides.",
                "Train LoRA adapters first; merge/export only after evals improve.",
                "Do not train on secrets, private documents, or bad outputs without filtering.",
            ],
            "retrieval_memory": [
                "Keep long-term facts in memory, but prioritize the latest owner request over old memories.",
                "Use file/search/RAG tools for private documents instead of stuffing everything into model weights.",
                "Store lessons from diagnostics as evals and preferences before training.",
            ],
            "evals": [
                "Run routing evals before and after each adapter.",
                "Track expected tool IDs, must-not-say phrases, and high-risk computer-control prompts.",
                "Only promote an adapter when it improves evals without breaking safety gates.",
            ],
            "execution_order": [
                "1. Keep improving deterministic tools and UI automation.",
                "2. Export datasets and prepare a LoRA job.",
                "3. Dry-run validation scripts.",
                "4. Train a small adapter.",
                "5. Evaluate adapter behavior.",
                "6. Merge/export only after passing evals.",
                "7. Use it as a specialist lane, not the only brain.",
            ],
        }

    def prepare_lora_job(
        self,
        *,
        reason: str = "manual",
        base_model: str = "Qwen/Qwen2.5-Coder-1.5B-Instruct",
        max_records: int = 300,
        max_steps: int = 120,
        max_seq_length: int = 1536,
    ) -> dict[str, Any]:
        export = self.export_dataset(reason=f"{reason}:lora_job", max_records=max_records)
        job_id = self.db.make_id("lora_job")
        job_dir = self.data_root / "training" / "jobs" / job_id
        dataset_dir = job_dir / "datasets"
        adapter_dir = job_dir / "adapters" / "project_q_lora_adapter"
        training_env_dir = self.data_root / "training" / "envs" / "project_q_lora_py312"
        dataset_dir.mkdir(parents=True, exist_ok=True)
        adapter_dir.parent.mkdir(parents=True, exist_ok=True)

        sft_copy = dataset_dir / "project_q_sft.jsonl"
        preference_copy = dataset_dir / "project_q_preferences.jsonl"
        eval_copy = dataset_dir / "project_q_routing_evals.jsonl"
        self._copy_text_file(Path(export["sft_dataset_path"]), sft_copy)
        self._copy_text_file(Path(export["preference_dataset_path"]), preference_copy)
        self._copy_text_file(Path(export["routing_eval_path"]), eval_copy)

        config = {
            "job_id": job_id,
            "base_model": base_model.strip() or "Qwen/Qwen2.5-Coder-1.5B-Instruct",
            "sft_dataset_path": str(sft_copy),
            "preference_dataset_path": str(preference_copy),
            "routing_eval_path": str(eval_copy),
            "output_adapter_dir": str(adapter_dir),
            "python_executable": sys.executable,
            "training_env_dir": str(training_env_dir),
            "max_steps": max(1, int(max_steps)),
            "max_seq_length": max(256, int(max_seq_length)),
            "per_device_train_batch_size": 1,
            "gradient_accumulation_steps": 8,
            "learning_rate": 0.0002,
            "lora_r": 16,
            "lora_alpha": 32,
            "lora_dropout": 0.05,
            "device_note": "RTX 3060 Ti friendly defaults; start with 1.5B or 3B before larger 7B adapters.",
            "created_at": utc_now(),
        }
        config_path = job_dir / "config.json"
        train_script_path = job_dir / "train_lora.py"
        run_script_path = job_dir / "run_training.ps1"
        readme_path = job_dir / "README.md"
        eval_prompts_path = job_dir / "eval_prompts.jsonl"
        requirements_path = job_dir / "requirements-training.txt"
        evaluate_path = job_dir / "evaluate_adapter.py"
        merge_path = job_dir / "merge_lora.py"
        validate_path = job_dir / "validate_job.py"
        modelfile_path = job_dir / "Modelfile.template"
        setup_env_path = job_dir / "setup_training_env.ps1"

        config_path.write_text(json.dumps(config, indent=2, ensure_ascii=True, sort_keys=True), encoding="utf-8")
        train_script_path.write_text(self._lora_job_script(), encoding="utf-8")
        run_script_path.write_text(self._lora_run_script(train_script_path, config_path, validate_path), encoding="utf-8")
        readme_path.write_text(self._lora_job_readme(config), encoding="utf-8")
        self._write_jsonl(eval_prompts_path, self._routing_eval_records())
        requirements_path.write_text(self._training_requirements(), encoding="utf-8")
        evaluate_path.write_text(self._adapter_eval_script(), encoding="utf-8")
        merge_path.write_text(self._merge_lora_script(), encoding="utf-8")
        validate_path.write_text(self._validate_job_script(), encoding="utf-8")
        modelfile_path.write_text(self._ollama_modelfile_template(), encoding="utf-8")
        setup_env_path.write_text(self._setup_training_env_script(config, requirements_path), encoding="utf-8")

        memory = self.memory_service.create(
            MemoryCreate(
                text=(
                    f"LoRA training job ({reason}): prepared local job {job_id} for {config['base_model']} "
                    f"with {max_records} requested source record(s)."
                ),
                kind="episodic",
                source="training_lab",
                confidence=0.86,
                owner_confirmed=False,
                tags=["training_lab", "lora_job", "weight_training", "self_improvement"],
                metadata={
                    "job_id": job_id,
                    "job_dir": str(job_dir),
                    "config_path": str(config_path),
                    "train_script_path": str(train_script_path),
                    "base_model": config["base_model"],
                },
            )
        )
        self.audit_service.log(
            action_type="training_lora_job",
            action_tier=2,
            tool_name="training.prepare_lora_job",
            outcome="completed",
            input_sources=["conversations", "audit", "tasks", reason],
            metadata={
                "job_id": job_id,
                "job_dir": str(job_dir),
                "config_path": str(config_path),
                "train_script_path": str(train_script_path),
                "created_memory_id": memory["id"],
            },
        )
        return {
            "status": "ready",
            "job_id": job_id,
            "job_dir": str(job_dir),
            "config_path": str(config_path),
            "train_script_path": str(train_script_path),
            "run_script_path": str(run_script_path),
            "readme_path": str(readme_path),
            "sft_dataset_path": str(sft_copy),
            "preference_dataset_path": str(preference_copy),
            "routing_eval_path": str(eval_copy),
            "eval_prompts_path": str(eval_prompts_path),
            "base_model": config["base_model"],
            "requirements_path": str(requirements_path),
            "evaluate_adapter_path": str(evaluate_path),
            "merge_lora_path": str(merge_path),
            "validate_job_path": str(validate_path),
            "modelfile_template_path": str(modelfile_path),
            "setup_env_path": str(setup_env_path),
            "created_memory_id": memory["id"],
            "summary": (
                "Prepared a local Project Q LoRA weight-training job. The script dry-runs by default; "
                "install the ML dependencies and run with --train when you are ready to update adapter weights."
            ),
            "next_step": f"Open {readme_path} and run {run_script_path} first without --train to validate the job.",
        }

    def export_dataset(self, reason: str = "manual", max_records: int = 200) -> dict[str, Any]:
        training_dir = self.data_root / "training"
        training_dir.mkdir(parents=True, exist_ok=True)
        exported_at = utc_now()
        sft_path = training_dir / "project_q_sft.jsonl"
        preference_path = training_dir / "project_q_preferences.jsonl"
        eval_path = training_dir / "project_q_routing_evals.jsonl"
        lora_script_path = training_dir / "train_project_q_lora.py"
        readme_path = training_dir / "README.md"

        sft_records = self._conversation_sft_records(max_records=max_records)
        sft_records.extend(self._built_in_router_records())
        sft_records.extend(self._synthetic_tool_router_records())
        preference_records = self._preference_records()
        eval_records = self._routing_eval_records()

        self._write_jsonl(sft_path, sft_records)
        self._write_jsonl(preference_path, preference_records)
        self._write_jsonl(eval_path, eval_records)
        lora_script_path.write_text(self._lora_script(), encoding="utf-8")
        readme_path.write_text(self._training_readme(sft_path, preference_path, eval_path, lora_script_path), encoding="utf-8")

        memory = self.memory_service.create(
            MemoryCreate(
                text=(
                    f"Training export ({reason}): prepared {len(sft_records)} SFT record(s), "
                    f"{len(preference_records)} preference record(s), and {len(eval_records)} routing eval(s)."
                ),
                kind="episodic",
                source="training_lab",
                confidence=0.84,
                owner_confirmed=False,
                tags=["training_lab", "fine_tuning", "self_improvement"],
                metadata={
                    "sft_dataset_path": str(sft_path),
                    "preference_dataset_path": str(preference_path),
                    "routing_eval_path": str(eval_path),
                    "lora_script_path": str(lora_script_path),
                },
            )
        )
        self.audit_service.log(
            action_type="training_export",
            action_tier=1,
            tool_name="training.export_dataset",
            outcome="completed",
            input_sources=["conversations", "audit", "tasks", reason],
            metadata={
                "sft_record_count": len(sft_records),
                "preference_record_count": len(preference_records),
                "routing_eval_count": len(eval_records),
                "memory_id": memory["id"],
            },
        )
        return {
            "status": "completed",
            "reason": reason,
            "sft_dataset_path": str(sft_path),
            "preference_dataset_path": str(preference_path),
            "routing_eval_path": str(eval_path),
            "lora_script_path": str(lora_script_path),
            "readme_path": str(readme_path),
            "sft_record_count": len(sft_records),
            "preference_record_count": len(preference_records),
            "routing_eval_count": len(eval_records),
            "created_memory_id": memory["id"],
            "next_step": (
                "Review the exported dataset, then run the LoRA script with a small local base model first "
                "before attempting a larger 7B fine-tune."
            ),
            "exported_at": exported_at,
        }

    def _conversation_sft_records(self, max_records: int) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT role, content, created_at
                FROM conversations
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (max_records,),
            ).fetchall()

        records: list[dict[str, Any]] = []
        pending_user: dict[str, str] | None = None
        for row in rows:
            role = row["role"]
            if role == "user":
                pending_user = {"content": row["content"], "created_at": row["created_at"]}
                continue
            if role != "assistant" or pending_user is None:
                continue
            records.append(
                {
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are Project Q. Route clear local-control requests to tools, ask for confirmation "
                                "when owner intent is ambiguous, and avoid stale memory overriding the latest command."
                            ),
                        },
                        {"role": "user", "content": pending_user["content"]},
                        {"role": "assistant", "content": row["content"]},
                    ],
                    "source": "conversation",
                    "created_at": row["created_at"],
                }
            )
            pending_user = None
        return records

    @staticmethod
    def _built_in_router_records() -> list[dict[str, Any]]:
        return [
            {
                "messages": [
                    {"role": "system", "content": "You are Project Q. Prefer tools for explicit local actions."},
                    {"role": "user", "content": "search files for resume"},
                    {
                        "role": "assistant",
                        "content": (
                            "Use filesystem.resolve_file_request with query 'resume' and ask which file if multiple "
                            "matches are found."
                        ),
                    },
                ],
                "source": "built_in_router_lesson",
            },
            {
                "messages": [
                    {"role": "system", "content": "You are Project Q. Prefer tools for spreadsheet analysis."},
                    {"role": "user", "content": "find the total sales amount by rep in this spreadsheet"},
                    {
                        "role": "assistant",
                        "content": "Use spreadsheet.analyze with operation 'sum' and group_by 'rep'.",
                    },
                ],
                "source": "built_in_router_lesson",
            },
            {
                "messages": [
                    {"role": "system", "content": "You are Project Q. Repair obvious typos before routing."},
                    {"role": "user", "content": "creat a webiste for a barber shpo with pricng and bokking"},
                    {
                        "role": "assistant",
                        "content": "Normalize the request and use code.generate_website.",
                    },
                ],
                "source": "built_in_typo_lesson",
            },
            {
                "messages": [
                    {"role": "system", "content": "You are Project Q. Route weight-training requests to jobs."},
                    {"role": "user", "content": "train yurself with a local lora job"},
                    {
                        "role": "assistant",
                        "content": "Normalize the request and use training.prepare_lora_job.",
                    },
                ],
                "source": "built_in_training_lesson",
            },
        ]

    def _preference_records(self) -> list[dict[str, Any]]:
        records = [
            {
                "prompt": "search files for resume",
                "chosen": "Resolve the current file-search intent with filesystem.resolve_file_request.",
                "rejected": "Answer from an unrelated older TODO-scanner memory.",
                "source": "regression_lesson",
            },
            {
                "prompt": "windows.launch_application blocked because tier 2 requires owner approval",
                "chosen": "Classify this as an expected owner-approval gate and explain how to approve it.",
                "rejected": "Create duplicate generic investigation tasks forever.",
                "source": "regression_lesson",
            },
        ]
        for task in self.task_service.list_all(limit=50):
            if task["source"] in {"self_diagnostics", "learning_lab"}:
                records.append(
                    {
                        "prompt": task["title"],
                        "chosen": task["description"] or task["title"],
                        "rejected": "Ignore the diagnostic signal.",
                        "source": "task_lesson",
                    }
                )
        return records

    @staticmethod
    def _synthetic_tool_router_records() -> list[dict[str, Any]]:
        examples = {
            "filesystem.resolve_file_request": [
                "search files for resume",
                "find my resume for Muhammad Umar Qasim",
                "look through my files for the invoice from April",
                "open the file called Cover Letter",
                "search my computer files for Razia Qasim Resume",
                "find files about project q prd",
            ],
            "filesystem.open_file_choice": [
                "open option 1",
                "reveal option 2",
                "select file choice 3",
                "show option 1 from the last file search",
                "open file option 2",
            ],
            "spreadsheet.analyze": [
                "what is the average revenue in sales.xlsx",
                "find the total sales amount by rep in this spreadsheet",
                "count the rows in this excel sheet",
                "calculate the max units from data.csv",
                "analyze this workbook and summarize the numeric columns",
            ],
            "spreadsheet.write_analysis": [
                "add a summary sheet with total revenue",
                "modify this spreadsheet with an average sales analysis tab",
                "create an excel analysis copy grouped by rep",
                "save a workbook summary with totals",
                "write the spreadsheet analysis into a new sheet",
            ],
            "code.generate_website": [
                "create a website for an IT consulting company",
                "creat a webiste for a barber shpo with pricng and bokking",
                "make a landing page for a restaurant with menu and reservations",
                "build a CRM web app with login and reports",
                "design a portfolio website with gallery and contact",
                "generate a luxury dark website for Crown Fade",
            ],
            "code.generate_project": [
                "build a Python script that scans this repo for TODO comments",
                "buid a pyhton scipt that scans todo commnets",
                "create an API with health check and tasks endpoint",
                "generate a command line tool for organizing files",
                "implement a small dashboard app with notes",
                "write a script that summarizes markdown files",
            ],
            "browser.complete_goal": [
                "find me a tutorial on how to setup this stand",
                "open google search invincible then go to images and find a high quality image",
                "search youtube for a setup guide and open the best video",
                "find a tutorial video and play it",
                "browse for the best guide and open the useful result",
            ],
            "windows.open_url": [
                "open my browser and search google for project q",
                "google search local llm setup",
                "open a browser tab for qwen coder ollama",
                "search google for invincible",
                "open https://example.com",
            ],
            "training.prepare_lora_job": [
                "train yourself with a local lora job",
                "train yurself with a local lora job",
                "prepare a fine tune job for Project Q",
                "create model weight training job",
                "update model weights with LoRA",
            ],
            "training.capability_plan": [
                "how can Project Q become ChatGPT Codex and Claude level",
                "make it like ChatGPT and Claude",
                "how do we get closer to AGI",
                "make Project Q extremely intelligent",
                "what is the path to Codex level coding ability",
            ],
            "diagnostics.run_self_check": [
                "run diagnostics",
                "debug yourself",
                "fix your mistakes",
                "troubleshoot Project Q failures",
                "test yourself and find issues",
            ],
            "knowledge.answer": [
                "write an essay about the industrial revolution",
                "solve this physics momentum problem",
                "explain World War II causes",
                "calculate this algebra problem",
                "draft a concise business memo",
            ],
        }
        system = (
            "You are the Project Q tool router. Return exactly one Project Q tool id and no explanation."
        )
        eval_style_prefix = (
            "You are the Project Q tool router. Return exactly one tool id from this list and no explanation:\n"
            "browser.complete_goal\n"
            "code.generate_project\n"
            "code.generate_website\n"
            "diagnostics.run_self_check\n"
            "filesystem.open_file_choice\n"
            "filesystem.resolve_file_request\n"
            "knowledge.answer\n"
            "spreadsheet.analyze\n"
            "spreadsheet.write_analysis\n"
            "training.capability_plan\n"
            "training.prepare_lora_job\n"
            "windows.open_url\n"
        )
        records: list[dict[str, Any]] = []
        for tool_id, prompts in examples.items():
            for prompt in prompts:
                records.append(
                    {
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": f"Owner request: {prompt}"},
                            {"role": "assistant", "content": tool_id},
                        ],
                        "source": "synthetic_tool_router",
                    }
                )
                records.append(
                    {
                        "messages": [
                            {"role": "system", "content": system},
                            {
                                "role": "user",
                                "content": f"{eval_style_prefix}Owner request: {prompt}\nTool id:",
                            },
                            {"role": "assistant", "content": tool_id},
                        ],
                        "source": "synthetic_tool_router_eval_style",
                    }
                )
        return records

    @staticmethod
    def _routing_eval_records() -> list[dict[str, Any]]:
        return [
            {
                "input": "search files for resume",
                "expected_tool": "filesystem.resolve_file_request",
                "must_not_include": "TODO",
            },
            {
                "input": "open my browser and search google for invinicible, than go to images",
                "expected_tool": "browser.complete_goal",
                "must_not_include": "literal full query search",
            },
            {
                "input": "what is the average revenue in sales.xlsx",
                "expected_tool": "spreadsheet.analyze",
                "must_not_include": "cannot access spreadsheet",
            },
            {
                "input": "creat a webiste for a barber shpo with pricng and bokking",
                "expected_tool": "code.generate_website",
                "must_not_include": "no action",
            },
            {
                "input": "buid a pyhton scipt that scans todo commnets",
                "expected_tool": "code.generate_project",
                "must_not_include": "cannot",
            },
            {
                "input": "train yurself with a local lora job",
                "expected_tool": "training.prepare_lora_job",
                "must_not_include": "export only",
            },
        ]

    @staticmethod
    def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
        text = "\n".join(json.dumps(record, ensure_ascii=True, sort_keys=True) for record in records)
        path.write_text(text + ("\n" if text else ""), encoding="utf-8")

    @staticmethod
    def _copy_text_file(source: Path, destination: Path) -> None:
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    @staticmethod
    def _lora_job_script() -> str:
        return '''"""
Project Q local LoRA weight-training job.

Dry-runs by default. Add --train after installing dependencies to update adapter weights.
"""

from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def format_messages(tokenizer, messages):
    if hasattr(tokenizer, "apply_chat_template"):
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
    return "\\n".join(f"{item['role']}: {item['content']}" for item in messages)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--max-steps", type=int)
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    dataset_path = Path(config["sft_dataset_path"]).expanduser().resolve()
    output_dir = Path(config["output_adapter_dir"]).expanduser().resolve()
    max_steps = int(args.max_steps or config.get("max_steps", 120))

    print("Project Q LoRA job is ready.")
    print(f"Base model: {config['base_model']}")
    print(f"Dataset: {dataset_path}")
    print(f"Output adapter: {output_dir}")
    print(f"Max steps: {max_steps}")
    if not dataset_path.exists():
        raise SystemExit(f"Dataset not found: {dataset_path}")
    if not args.train:
        print("Dry run only. Re-run with --train to actually update LoRA adapter weights.")
        return

    try:
        import torch
        from datasets import load_dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments
        from trl import SFTConfig, SFTTrainer
    except ImportError as exc:
        raise SystemExit(
            "Missing dependencies. Install torch, transformers, datasets, peft, trl, and accelerate first."
        ) from exc

    tokenizer = AutoTokenizer.from_pretrained(config["base_model"], trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        config["base_model"],
        trust_remote_code=True,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto" if torch.cuda.is_available() else None,
    )
    dataset = load_dataset("json", data_files=str(dataset_path), split="train")

    peft_config = LoraConfig(
        r=int(config.get("lora_r", 16)),
        lora_alpha=int(config.get("lora_alpha", 32)),
        lora_dropout=float(config.get("lora_dropout", 0.05)),
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    max_seq_length_value = int(config.get("max_seq_length", 1536))
    sft_config_kwargs = {
        "output_dir": str(output_dir),
        "per_device_train_batch_size": int(config.get("per_device_train_batch_size", 1)),
        "gradient_accumulation_steps": int(config.get("gradient_accumulation_steps", 8)),
        "learning_rate": float(config.get("learning_rate", 0.0002)),
        "max_steps": max_steps,
        "logging_steps": 5,
        "save_steps": max(20, max_steps),
        "fp16": torch.cuda.is_available(),
        "report_to": "none",
        "max_length": max_seq_length_value,
        "packing": False,
    }
    try:
        training_args = SFTConfig(**sft_config_kwargs)
    except TypeError:
        legacy_kwargs = dict(sft_config_kwargs)
        max_seq_length_value = legacy_kwargs.pop("max_length")
        legacy_kwargs.pop("packing", None)
        legacy_kwargs["report_to"] = []
        training_args = TrainingArguments(**legacy_kwargs)

    def formatting_func(example):
        return format_messages(tokenizer, example["messages"])

    trainer_kwargs = {
        "model": model,
        "train_dataset": dataset,
        "args": training_args,
        "peft_config": peft_config,
        "formatting_func": formatting_func,
    }
    trainer_signature = inspect.signature(SFTTrainer.__init__)
    if "processing_class" in trainer_signature.parameters:
        trainer_kwargs["processing_class"] = tokenizer
    else:
        trainer_kwargs["tokenizer"] = tokenizer
        trainer_kwargs["max_seq_length"] = max_seq_length_value
    trainer = SFTTrainer(**trainer_kwargs)
    trainer.train()
    trainer.model.save_pretrained(str(output_dir))
    tokenizer.save_pretrained(str(output_dir))
    print(f"Saved Project Q LoRA adapter to {output_dir}")


if __name__ == "__main__":
    main()
'''

    @staticmethod
    def _lora_run_script(train_script_path: Path, config_path: Path, validate_path: Path) -> str:
        return (
            "$ErrorActionPreference = 'Stop'\n"
            f"$Script = '{train_script_path.as_posix()}'\n"
            f"$Config = '{config_path.as_posix()}'\n"
            f"$Validator = '{validate_path.as_posix()}'\n"
            "$env:PYTHONUTF8 = '1'\n"
            "$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'\n"
            "$ConfigJson = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json\n"
            "$EnvPython = Join-Path $ConfigJson.training_env_dir 'Scripts\\python.exe'\n"
            "$Python = if ($env:PROJECT_Q_PYTHON) { $env:PROJECT_Q_PYTHON } elseif (Test-Path -LiteralPath $EnvPython) { $EnvPython } else { $ConfigJson.python_executable }\n"
            "& $Python $Validator --config $Config\n"
            "& $Python $Script --config $Config\n"
            "Write-Host \"Dry run complete. Add --train to update LoRA adapter weights after dependencies are installed.\"\n"
        )

    @staticmethod
    def _lora_job_readme(config: dict[str, Any]) -> str:
        return (
            "# Project Q LoRA Training Job\n\n"
            "This job turns Project Q's local conversations, repair lessons, routing evals, and owner preferences "
            "into a LoRA adapter training package.\n\n"
            "## What This Does\n\n"
            "- Keeps your data local in the Project Q data folder.\n"
            "- Builds an SFT dataset from conversation turns and built-in routing lessons.\n"
            "- Builds preference and routing eval files for regression testing.\n"
            "- Provides a dry-run-first training script so mistakes fail safely before GPU work starts.\n\n"
            "## Recommended First Model\n\n"
            f"- Base model: `{config['base_model']}`\n"
            "- Your RTX 3060 Ti has 8 GB VRAM, so start with a 1.5B or 3B instruct/coder model. "
            "Move to larger models only after the dry run and dependency checks pass.\n\n"
            "## Run\n\n"
            "```powershell\n"
            ".\\setup_training_env.ps1\n"
            "python .\\validate_job.py --config .\\config.json\n"
            ".\\run_training.ps1\n"
            "```\n\n"
            "That is a dry run. To actually train adapter weights, install compatible GPU dependencies, then run:\n\n"
            "```powershell\n"
            "python .\\train_lora.py --config .\\config.json --train\n"
            "```\n\n"
            "## Important\n\n"
            "This trains adapter weights, not the full base model. That is the safest and most realistic path on this PC.\n"
        )

    @staticmethod
    def _setup_training_env_script(config: dict[str, Any], requirements_path: Path) -> str:
        return (
            "$ErrorActionPreference = 'Stop'\n"
            f"$BasePython = '{Path(str(config['python_executable'])).as_posix()}'\n"
            f"$EnvDir = '{Path(str(config['training_env_dir'])).as_posix()}'\n"
            f"$Requirements = '{requirements_path.as_posix()}'\n"
            "$TorchIndexUrl = if ($env:PROJECT_Q_TORCH_INDEX_URL) { $env:PROJECT_Q_TORCH_INDEX_URL } else { 'https://download.pytorch.org/whl/cu128' }\n"
            "$env:PYTHONUTF8 = '1'\n"
            "if (!(Test-Path -LiteralPath $EnvDir)) {\n"
            "  & $BasePython -m venv $EnvDir\n"
            "}\n"
            "$Python = Join-Path $EnvDir 'Scripts\\python.exe'\n"
            "& $Python -m pip install --upgrade pip setuptools wheel\n"
            "& $Python -m pip install --index-url $TorchIndexUrl torch\n"
            "& $Python -m pip install -r $Requirements\n"
            "Write-Host \"Project Q training environment ready: $Python\"\n"
            "Write-Host \"To force this environment in the current terminal: `$env:PROJECT_Q_PYTHON='$Python'\"\n"
        )

    @staticmethod
    def _training_requirements() -> str:
        return (
            "accelerate>=0.33.0\n"
            "datasets>=2.20.0\n"
            "peft>=0.12.0\n"
            "safetensors>=0.4.4\n"
            "sentencepiece>=0.2.0\n"
            "transformers>=4.44.0\n"
            "trl>=0.9.6\n"
        )

    @staticmethod
    def _validate_job_script() -> str:
        return '''from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_KEYS = {
    "base_model",
    "sft_dataset_path",
    "preference_dataset_path",
    "routing_eval_path",
    "output_adapter_dir",
    "max_steps",
    "max_seq_length",
}


def read_jsonl(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"Missing config: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    missing = sorted(REQUIRED_KEYS - set(config))
    if missing:
        raise SystemExit(f"Missing config keys: {', '.join(missing)}")
    for key in ("sft_dataset_path", "preference_dataset_path", "routing_eval_path"):
        path = Path(config[key]).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"Missing dataset for {key}: {path}")
        count = read_jsonl(path)
        print(f"{key}: {count} record(s)")
    print("Project Q LoRA job validation passed.")


if __name__ == "__main__":
    main()
'''

    @staticmethod
    def _adapter_eval_script() -> str:
        return '''"""
Evaluate a Project Q LoRA adapter against routing evals.

Default mode validates eval data only. Add --run-model to load the base model plus adapter and score generated text.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if text:
                rows.append(json.loads(text))
    return rows


TOOL_IDS = [
    "browser.complete_goal",
    "code.generate_project",
    "code.generate_website",
    "diagnostics.run_self_check",
    "filesystem.open_file_choice",
    "filesystem.resolve_file_request",
    "knowledge.answer",
    "spreadsheet.analyze",
    "spreadsheet.write_analysis",
    "training.capability_plan",
    "training.prepare_lora_job",
    "windows.open_url",
]


def score_text(text: str, expected_tool: str, must_not_include: str) -> dict:
    lowered = text.lower()
    expected = expected_tool.lower()
    forbidden = must_not_include.lower()
    return {
        "passed": expected in lowered and (not forbidden or forbidden not in lowered),
        "expected_tool_found": expected in lowered,
        "forbidden_found": bool(forbidden and forbidden in lowered),
    }


def build_prompt(tokenizer, owner_request: str) -> str:
    system = "You are the Project Q tool router. Return exactly one Project Q tool id and no explanation."
    user = (
        "Return exactly one tool id from this list and no explanation:\\n"
        + "\\n".join(TOOL_IDS)
        + f"\\nOwner request: {owner_request}\\nTool id:"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if hasattr(tokenizer, "apply_chat_template"):
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return system + "\\n" + user + "\\nassistant:"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--run-model", action="store_true")
    parser.add_argument("--max-new-tokens", type=int, default=180)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    evals = load_jsonl(Path(config["routing_eval_path"]))
    if not args.run_model:
        print(f"Validated {len(evals)} routing eval prompt(s). Re-run with --run-model to score adapter output.")
        return

    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise SystemExit("Install torch, transformers, and peft before model eval.") from exc

    tokenizer = AutoTokenizer.from_pretrained(config["base_model"], trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        config["base_model"],
        trust_remote_code=True,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto" if torch.cuda.is_available() else None,
    )
    model = PeftModel.from_pretrained(model, config["output_adapter_dir"])
    passed = 0
    for item in evals:
        prompt = build_prompt(tokenizer, item["input"])
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        output = model.generate(
            **inputs,
            max_new_tokens=args.max_new_tokens,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
        )
        generated = output[0][inputs["input_ids"].shape[-1]:]
        text = tokenizer.decode(generated, skip_special_tokens=True).strip()
        score = score_text(text, item["expected_tool"], item.get("must_not_include", ""))
        passed += int(score["passed"])
        print(json.dumps({"input": item["input"], "score": score, "text": text[-500:]}, ensure_ascii=True))
    print(f"Passed {passed}/{len(evals)} routing evals.")


if __name__ == "__main__":
    main()
'''

    @staticmethod
    def _merge_lora_script() -> str:
        return '''"""
Merge a trained Project Q LoRA adapter into its base model for export.

Dry-runs by default. Add --merge after training has produced adapter files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--output-dir", default="merged_project_q_model")
    parser.add_argument("--merge", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    adapter_dir = Path(config["output_adapter_dir"]).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    print(f"Base model: {config['base_model']}")
    print(f"Adapter: {adapter_dir}")
    print(f"Merged output: {output_dir}")
    if not args.merge:
        print("Dry run only. Re-run with --merge after adapter training completes.")
        return
    if not adapter_dir.exists():
        raise SystemExit(f"Adapter directory not found: {adapter_dir}")
    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise SystemExit("Install torch, transformers, and peft before merging.") from exc
    model = AutoModelForCausalLM.from_pretrained(
        config["base_model"],
        trust_remote_code=True,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto" if torch.cuda.is_available() else None,
    )
    model = PeftModel.from_pretrained(model, str(adapter_dir))
    merged = model.merge_and_unload()
    tokenizer = AutoTokenizer.from_pretrained(config["base_model"], trust_remote_code=True)
    merged.save_pretrained(str(output_dir), safe_serialization=True)
    tokenizer.save_pretrained(str(output_dir))
    print(f"Saved merged model to {output_dir}")


if __name__ == "__main__":
    main()
'''

    @staticmethod
    def _ollama_modelfile_template() -> str:
        return (
            "# Convert the merged Hugging Face model to GGUF first, then point FROM to that file.\n"
            "# Example: FROM ./project-q-qwen-coder.gguf\n"
            "FROM ./merged-project-q.gguf\n\n"
            "PARAMETER temperature 0.2\n"
            "PARAMETER top_p 0.9\n"
            "PARAMETER num_ctx 8192\n\n"
            "SYSTEM \"You are Project Q, a private Windows executive agent. Prefer audited tools, route current owner intent before old memory, repair obvious typos, and be concise.\"\n"
        )

    @staticmethod
    def _lora_script() -> str:
        return '''"""
Project Q LoRA training script.

This script is intentionally conservative for an 8 GB GPU. Start with a 1.5B or 3B instruct/coder model.
It dry-runs by default. Add --train after installing dependencies to actually update LoRA adapter weights.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def format_messages(tokenizer, messages):
    if hasattr(tokenizer, "apply_chat_template"):
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
    return "\\n".join(f"{item['role']}: {item['content']}" for item in messages)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", default="Qwen/Qwen2.5-Coder-1.5B-Instruct")
    parser.add_argument("--dataset", default="project_q_sft.jsonl")
    parser.add_argument("--output-dir", default="project_q_lora_adapter")
    parser.add_argument("--max-steps", type=int, default=120)
    parser.add_argument("--max-seq-length", type=int, default=1536)
    parser.add_argument("--train", action="store_true")
    args = parser.parse_args()

    dataset_path = Path(args.dataset).resolve()
    if not dataset_path.exists():
        raise SystemExit(f"Dataset not found: {dataset_path}")

    print("Project Q LoRA training assets are ready.")
    print(f"Base model: {args.base_model}")
    print(f"Dataset: {dataset_path}")
    print(f"Output adapter: {Path(args.output_dir).resolve()}")
    print("Recommended first pass for RTX 3060 Ti: Qwen2.5-Coder-1.5B or another 1.5B/3B instruct model.")
    if not args.train:
        print("Dry run only. Re-run with --train to update LoRA adapter weights.")
        return

    try:
        import torch
        from datasets import load_dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments
        from trl import SFTTrainer
    except ImportError as exc:
        raise SystemExit(
            "Missing training dependencies. Install torch, transformers, datasets, peft, trl, and accelerate first."
        ) from exc

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        trust_remote_code=True,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto" if torch.cuda.is_available() else None,
    )

    dataset = load_dataset("json", data_files=str(dataset_path), split="train")

    def formatting_func(example):
        return format_messages(tokenizer, example["messages"])

    peft_config = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    training_args = TrainingArguments(
        output_dir=args.output_dir,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        learning_rate=2e-4,
        max_steps=args.max_steps,
        logging_steps=5,
        save_steps=max(20, args.max_steps),
        fp16=torch.cuda.is_available(),
        report_to=[],
    )
    trainer = SFTTrainer(
        model=model,
        train_dataset=dataset,
        args=training_args,
        peft_config=peft_config,
        formatting_func=formatting_func,
        max_seq_length=args.max_seq_length,
        tokenizer=tokenizer,
    )
    trainer.train()
    trainer.model.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    print(f"Saved Project Q LoRA adapter to {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
'''

    @staticmethod
    def _training_readme(
        sft_path: Path,
        preference_path: Path,
        eval_path: Path,
        lora_script_path: Path,
    ) -> str:
        return (
            "# Project Q Training Lab\n\n"
            "This folder contains the first real weight-training assets for Project Q.\n\n"
            f"- SFT dataset: `{sft_path.name}`\n"
            f"- Preference dataset: `{preference_path.name}`\n"
            f"- Routing evals: `{eval_path.name}`\n"
            f"- LoRA training script: `{lora_script_path.name}`\n\n"
            "Start with a small local model on an RTX 3060 Ti. The script dry-runs by default; after installing "
            "torch, transformers, datasets, peft, trl, and accelerate, run it with `--train` to update LoRA adapter weights.\n"
        )
