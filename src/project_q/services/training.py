from __future__ import annotations

import json
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

from project_q.models import MemoryCreate, SettingsUpdate, utc_now
from project_q.services.training_evaluation import (
    AdapterEvaluationResult,
    BaseEvaluationResult,
    EvaluationReport,
    PromotionGateConfig,
    canonicalize_prompt,
    compare_evaluation_results,
    detect_prompt_leakage,
    deterministic_disjoint_split,
    evaluate_promotion,
)


MINIMUM_PROMOTION_SCORE = 0.85


class TrainingService:
    def __init__(
        self,
        db,
        memory_service,
        task_service,
        audit_service,
        data_root: Path,
        settings_service=None,
    ) -> None:
        self.db = db
        self.memory_service = memory_service
        self.task_service = task_service
        self.audit_service = audit_service
        self.data_root = data_root
        self.settings_service = settings_service
        self.training_root = self.data_root / "training"
        self.jobs_root = self.training_root / "jobs"
        self.active_adapter_path = self.training_root / "active_adapter.json"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._ensure_lifecycle_schema()

    def _ensure_lifecycle_schema(self) -> None:
        with self.db.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS training_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_dir TEXT NOT NULL,
                    base_model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    artifact_valid INTEGER NOT NULL DEFAULT 0,
                    promoted INTEGER NOT NULL DEFAULT 0,
                    latest_evaluation_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS training_evaluations (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES training_jobs(job_id)
                );
                """
            )

    def list_lora_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for path in sorted(self.jobs_root.iterdir(), reverse=True):
            if not path.is_dir() or not re.fullmatch(r"lora_job_[A-Za-z0-9_-]+", path.name):
                continue
            jobs.append(self._sync_lora_job(path.name))
        return sorted(jobs, key=lambda item: item["created_at"], reverse=True)

    def get_lora_job(self, job_id: str) -> dict[str, Any]:
        self._job_dir(job_id, must_exist=True)
        return self._sync_lora_job(job_id)

    def audit_lora_job(self, job_id: str) -> dict[str, Any]:
        job_dir = self._job_dir(job_id, must_exist=True)
        config_path = job_dir / "config.json"
        errors: list[str] = []
        config: dict[str, Any] = {}
        if not config_path.exists():
            errors.append("missing config.json")
        else:
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"invalid config.json: {exc}")

        adapter_dir = None
        if config.get("output_adapter_dir"):
            try:
                adapter_dir = self._configured_job_path(job_dir, config["output_adapter_dir"])
            except ValueError as exc:
                errors.append(str(exc))
        else:
            errors.append("config is missing output_adapter_dir")

        artifact_files: list[str] = []
        if adapter_dir is not None:
            required_config = adapter_dir / "adapter_config.json"
            model_candidates = (
                adapter_dir / "adapter_model.safetensors",
                adapter_dir / "adapter_model.bin",
            )
            if not required_config.is_file():
                errors.append("adapter_config.json is missing")
            else:
                try:
                    json.loads(required_config.read_text(encoding="utf-8"))
                    artifact_files.append(str(required_config))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(f"adapter_config.json is invalid: {exc}")
            model_path = next(
                (path for path in model_candidates if path.is_file() and path.stat().st_size > 0),
                None,
            )
            if model_path is None:
                errors.append("adapter model weights are missing or empty")
            else:
                artifact_files.append(str(model_path))

        training_prompts, dataset_errors = self._job_training_prompts(job_dir, config)
        errors.extend(dataset_errors)
        holdout_prompts, holdout_errors = self._job_holdout_prompts(job_dir, config)
        errors.extend(holdout_errors)
        leakage = detect_prompt_leakage(training_prompts, holdout_prompts)
        trainer_state = self._trainer_state_summary(job_dir)
        artifact_valid = not any(
            message.startswith(
                (
                    "missing config",
                    "invalid config",
                    "configured path is outside",
                    "config is missing output",
                    "adapter_config",
                    "adapter model",
                )
            )
            for message in errors
        )
        result = {
            "job_id": job_id,
            "job_dir": str(job_dir),
            "base_model": str(config.get("base_model", "")),
            "artifact": {
                "valid": artifact_valid,
                "adapter_dir": str(adapter_dir) if adapter_dir else "",
                "files": artifact_files,
                "errors": errors,
            },
            "training_prompt_count": len(training_prompts),
            "holdout_prompt_count": len(holdout_prompts),
            "leakage": leakage.to_dict(),
            "trainer_state": trainer_state,
            "audited_at": utc_now(),
        }
        self._upsert_job(
            job_id=job_id,
            job_dir=job_dir,
            base_model=str(config.get("base_model", "")),
            status=self._job_status(job_id, artifact_valid=artifact_valid),
            artifact_valid=artifact_valid,
        )
        return result

    def record_lora_evaluation(
        self,
        job_id: str,
        *,
        base_metrics: dict[str, Any],
        adapter_metrics: dict[str, Any],
        holdout_prompts: list[str],
        minimum_score: float = 0.85,
        maximum_score_regression: float = 0.0,
        maximum_latency_increase_ms: float | None = None,
        maximum_failure_increase: int | None = None,
        evaluator_source: str = "server_evaluator",
        trusted_evaluator: bool = True,
    ) -> dict[str, Any]:
        job_dir = self._job_dir(job_id, must_exist=True)
        if not holdout_prompts:
            raise ValueError("at least one holdout prompt is required")
        clean_holdouts = [str(prompt).strip() for prompt in holdout_prompts if str(prompt).strip()]
        if len(clean_holdouts) != len(holdout_prompts):
            raise ValueError("holdout prompts must be non-empty strings")

        config = json.loads((job_dir / "config.json").read_text(encoding="utf-8"))
        training_prompts, errors = self._job_training_prompts(job_dir, config)
        if errors:
            raise ValueError("; ".join(errors))
        leakage = detect_prompt_leakage(training_prompts, clean_holdouts)
        audit = self.audit_lora_job(job_id)
        artifact_digest = self._artifact_digest(audit)
        holdout_digest = self._holdout_digest(clean_holdouts)
        base = BaseEvaluationResult(**base_metrics)
        adapter = AdapterEvaluationResult(**adapter_metrics)
        enforced_minimum_score = max(MINIMUM_PROMOTION_SCORE, float(minimum_score))
        policy = PromotionGateConfig(
            minimum_score=enforced_minimum_score,
            maximum_score_regression=maximum_score_regression,
            maximum_latency_increase_ms=maximum_latency_increase_ms,
            maximum_failure_increase=maximum_failure_increase,
        )
        decision = evaluate_promotion(
            base,
            adapter,
            artifact_valid=bool(audit["artifact"]["valid"]),
            leakage=leakage,
            config=policy,
        )
        report = EvaluationReport(
            comparison=compare_evaluation_results(base, adapter),
            leakage=leakage,
            promotion=decision,
            artifact_valid=bool(audit["artifact"]["valid"]),
            metadata={
                "job_id": job_id,
                "base_model": config.get("base_model", ""),
                "holdout_prompts": clean_holdouts,
                "holdout_digest": holdout_digest,
                "artifact_digest": artifact_digest,
                "evaluator_source": str(evaluator_source or "server_evaluator")[:120],
                "trusted_evaluator": bool(trusted_evaluator),
                "created_at": utc_now(),
            },
        )
        payload = report.to_dict()
        report_path = job_dir / "evaluation_report.json"
        self._write_json(report_path, payload)
        created_at = utc_now()
        self._upsert_job(
            job_id=job_id,
            job_dir=job_dir,
            base_model=str(config.get("base_model", "")),
            status="evaluation_passed" if decision.approved else "evaluation_failed",
            artifact_valid=bool(audit["artifact"]["valid"]),
            latest_evaluation=payload,
        )
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO training_evaluations (id, job_id, report_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    self.db.make_id("training_eval"),
                    job_id,
                    json.dumps(payload, ensure_ascii=True, sort_keys=True),
                    created_at,
                ),
            )
        self.audit_service.log(
            action_type="training_evaluation",
            action_tier=1,
            tool_name="training.record_lora_evaluation",
            outcome="passed" if decision.approved else "failed",
            input_sources=[job_id, "clean_holdout"],
            metadata={
                "job_id": job_id,
                "adapter_score": adapter.score,
                "base_score": base.score,
                "promotion_approved": decision.approved,
                "report_path": str(report_path),
            },
        )
        return payload

    def promote_lora_job(self, job_id: str, *, owner_confirmed: bool) -> dict[str, Any]:
        if not owner_confirmed:
            raise PermissionError("owner confirmation is required to promote an adapter")
        job_dir = self._job_dir(job_id, must_exist=True)
        report_path = job_dir / "evaluation_report.json"
        if not report_path.exists():
            raise PermissionError("a persisted clean evaluation is required before promotion")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not bool(report.get("promotion", {}).get("approved")):
            raise PermissionError("this adapter did not pass the configured promotion gates")
        metadata = report.get("metadata", {})
        if not isinstance(metadata, dict) or not metadata.get("trusted_evaluator"):
            raise PermissionError("a trusted server-side evaluation is required before promotion")
        audit = self.audit_lora_job(job_id)
        if not audit["artifact"]["valid"]:
            raise PermissionError("adapter artifacts are not valid")
        expected_artifact_digest = metadata.get("artifact_digest")
        current_artifact_digest = self._artifact_digest(audit)
        if expected_artifact_digest != current_artifact_digest:
            raise PermissionError("adapter artifacts changed after evaluation")

        previous = None
        if self.active_adapter_path.exists():
            previous = json.loads(self.active_adapter_path.read_text(encoding="utf-8"))
        active = {
            "job_id": job_id,
            "adapter_dir": audit["artifact"]["adapter_dir"],
            "base_model": audit["base_model"],
            "promoted_at": utc_now(),
        }
        self._write_json(self.active_adapter_path, active)
        # PRD: a promoted adapter must actually change inference. Switch the served
        # Ollama model (graceful no-op until a GGUF artifact + ollama are present).
        serving: dict[str, Any] = {"status": "skipped", "reason": "serving switch not attempted"}
        try:
            serving = self._activate_ollama_serving(job_id, job_dir)
        except Exception as exc:  # noqa: BLE001
            serving = {"status": "error", "reason": str(exc)[:300]}
        active["serving"] = serving
        self._write_json(self.active_adapter_path, active)
        self._write_json(
            job_dir / "promotion.json",
            {"active": active, "previous": previous, "owner_confirmed": True},
        )
        self._upsert_job(
            job_id=job_id,
            job_dir=job_dir,
            base_model=audit["base_model"],
            status="promoted",
            artifact_valid=True,
            promoted=True,
            latest_evaluation=report,
        )
        self.audit_service.log(
            action_type="training_promotion",
            action_tier=2,
            tool_name="training.promote_lora_job",
            approved_by_owner=True,
            outcome="completed",
            input_sources=[job_id, str(report_path)],
            metadata=active,
        )
        return {"status": "promoted", **active, "rollback_available": True}

    def rollback_lora_job(self, job_id: str, *, owner_confirmed: bool) -> dict[str, Any]:
        if not owner_confirmed:
            raise PermissionError("owner confirmation is required to roll back an adapter")
        job_dir = self._job_dir(job_id, must_exist=True)
        promotion_path = job_dir / "promotion.json"
        if not promotion_path.exists():
            raise ValueError("this job has no promotion to roll back")
        active = (
            json.loads(self.active_adapter_path.read_text(encoding="utf-8"))
            if self.active_adapter_path.exists()
            else {}
        )
        if active.get("job_id") != job_id:
            raise ValueError("this adapter is not currently active")
        promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
        previous = promotion.get("previous")
        if previous:
            self._write_json(self.active_adapter_path, previous)
        else:
            self.active_adapter_path.unlink(missing_ok=True)
        rollback = {
            "status": "rolled_back",
            "job_id": job_id,
            "restored_job_id": previous.get("job_id") if previous else None,
            "rolled_back_at": utc_now(),
        }
        self._write_json(job_dir / "rollback.json", rollback)
        job = self.get_lora_job(job_id)
        self._upsert_job(
            job_id=job_id,
            job_dir=job_dir,
            base_model=job["base_model"],
            status="rolled_back",
            artifact_valid=bool(job["artifact_valid"]),
            promoted=False,
            latest_evaluation=job.get("latest_evaluation", {}),
        )
        self.audit_service.log(
            action_type="training_rollback",
            action_tier=2,
            tool_name="training.rollback_lora_job",
            approved_by_owner=True,
            outcome="completed",
            input_sources=[job_id],
            metadata=rollback,
        )
        return rollback

    def _activate_ollama_serving(self, job_id: str, job_dir: Path) -> dict[str, Any]:
        """Materialize and route to an Ollama model for a promoted adapter.

        Degrades gracefully: a real served model needs a GGUF artifact (from the
        offline merge + conversion step) and the ``ollama`` binary. When either is
        absent the intent is recorded and inference routing is left unchanged.
        """
        import shutil
        import subprocess

        served_model = "project-q-" + "".join(
            ch for ch in job_id.lower() if ch.isalnum() or ch in "_-"
        )[:40]
        gguf = next(iter(sorted(job_dir.glob("*.gguf"))), None)
        if gguf is None:
            return {
                "status": "pending",
                "served_model": served_model,
                "reason": "no GGUF artifact in job dir; run merge_lora.py + GGUF conversion, then re-promote",
            }
        if shutil.which("ollama") is None:
            return {
                "status": "skipped",
                "served_model": served_model,
                "reason": "ollama executable not found on PATH",
            }
        modelfile = job_dir / "Modelfile"
        modelfile.write_text(
            f"FROM ./{gguf.name}\n\n"
            "PARAMETER temperature 0.2\nPARAMETER top_p 0.9\nPARAMETER num_ctx 8192\n\n"
            'SYSTEM "You are Project Q, a private Windows executive agent. Prefer audited '
            'tools, route current owner intent before old memory, and be concise."\n',
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(
                ["ollama", "create", served_model, "-f", str(modelfile)],
                cwd=str(job_dir),
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "error", "served_model": served_model, "reason": str(exc)[:300]}
        if completed.returncode != 0:
            return {
                "status": "error",
                "served_model": served_model,
                "reason": (completed.stderr or completed.stdout or "ollama create failed").strip()[:300],
            }
        # Route inference to the freshly served fine-tuned model.
        if self.settings_service is not None:
            try:
                self.settings_service.update(
                    SettingsUpdate(model_name=served_model, ollama_general_model=served_model)
                )
            except Exception:  # noqa: BLE001
                pass
        return {"status": "active", "served_model": served_model}

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
        validation_copy = dataset_dir / "project_q_sft_validation.jsonl"
        preference_copy = dataset_dir / "project_q_preferences.jsonl"
        eval_copy = dataset_dir / "project_q_routing_evals.jsonl"
        self._copy_text_file(Path(export["sft_dataset_path"]), sft_copy)
        self._copy_text_file(Path(export["validation_dataset_path"]), validation_copy)
        self._copy_text_file(Path(export["preference_dataset_path"]), preference_copy)
        self._copy_text_file(Path(export["routing_eval_path"]), eval_copy)

        config = {
            "job_id": job_id,
            "base_model": base_model.strip() or "Qwen/Qwen2.5-Coder-1.5B-Instruct",
            "sft_dataset_path": str(sft_copy),
            "validation_dataset_path": str(validation_copy),
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
        self._upsert_job(
            job_id=job_id,
            job_dir=job_dir,
            base_model=config["base_model"],
            status="prepared",
            artifact_valid=False,
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
            "validation_dataset_path": str(validation_copy),
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
        validation_path = training_dir / "project_q_sft_validation.jsonl"
        preference_path = training_dir / "project_q_preferences.jsonl"
        eval_path = training_dir / "project_q_routing_evals.jsonl"
        lora_script_path = training_dir / "train_project_q_lora.py"
        readme_path = training_dir / "README.md"

        sft_records = self._conversation_sft_records(max_records=max_records)
        sft_records.extend(self._built_in_router_records())
        sft_records.extend(self._synthetic_tool_router_records())
        preference_records = self._preference_records()
        eval_records = self._routing_eval_records()
        holdout_keys = {
            canonicalize_prompt(record["input"])
            for record in eval_records
        }
        safe_sft_records = [
            record
            for record in sft_records
            if canonicalize_prompt(self._training_record_prompt(record)) not in holdout_keys
        ]
        split = deterministic_disjoint_split(
            safe_sft_records,
            validation_fraction=0.1,
            holdout_fraction=0.0,
            seed="project-q-training-v1",
            prompt_getter=self._training_record_prompt,
        )
        sft_records = list(split.train)
        validation_records = list(split.validation)

        self._write_jsonl(sft_path, sft_records)
        self._write_jsonl(validation_path, validation_records)
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
                    "validation_dataset_path": str(validation_path),
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
                "validation_record_count": len(validation_records),
                "preference_record_count": len(preference_records),
                "routing_eval_count": len(eval_records),
                "memory_id": memory["id"],
            },
        )
        return {
            "status": "completed",
            "reason": reason,
            "sft_dataset_path": str(sft_path),
            "validation_dataset_path": str(validation_path),
            "preference_dataset_path": str(preference_path),
            "routing_eval_path": str(eval_path),
            "lora_script_path": str(lora_script_path),
            "readme_path": str(readme_path),
            "sft_record_count": len(sft_records),
            "validation_record_count": len(validation_records),
            "preference_record_count": len(preference_records),
            "routing_eval_count": len(eval_records),
            "created_memory_id": memory["id"],
            "next_step": (
                "Review the exported dataset, then run the LoRA script with a small local base model first "
                "before attempting a larger 7B fine-tune."
            ),
            "exported_at": exported_at,
        }

    def _sync_lora_job(self, job_id: str) -> dict[str, Any]:
        audit = self.audit_lora_job(job_id)
        status = self._job_status(job_id, artifact_valid=bool(audit["artifact"]["valid"]))
        report_path = self._job_dir(job_id, must_exist=True) / "evaluation_report.json"
        latest_evaluation = (
            json.loads(report_path.read_text(encoding="utf-8"))
            if report_path.exists()
            else {}
        )
        self._upsert_job(
            job_id=job_id,
            job_dir=self._job_dir(job_id, must_exist=True),
            base_model=audit["base_model"],
            status=status,
            artifact_valid=bool(audit["artifact"]["valid"]),
            promoted=status == "promoted",
            latest_evaluation=latest_evaluation,
        )
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM training_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return {
            "job_id": row["job_id"],
            "job_dir": row["job_dir"],
            "base_model": row["base_model"],
            "status": row["status"],
            "artifact_valid": bool(row["artifact_valid"]),
            "promoted": bool(row["promoted"]),
            "latest_evaluation": json.loads(row["latest_evaluation_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _upsert_job(
        self,
        *,
        job_id: str,
        job_dir: Path,
        base_model: str,
        status: str,
        artifact_valid: bool,
        promoted: bool | None = None,
        latest_evaluation: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self.db.connection() as conn:
            existing = conn.execute(
                "SELECT created_at, promoted, latest_evaluation_json FROM training_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            promoted_value = (
                int(promoted)
                if promoted is not None
                else int(existing["promoted"]) if existing else 0
            )
            evaluation_json = (
                json.dumps(latest_evaluation, ensure_ascii=True, sort_keys=True)
                if latest_evaluation is not None
                else existing["latest_evaluation_json"] if existing else "{}"
            )
            conn.execute(
                """
                INSERT INTO training_jobs (
                    job_id, job_dir, base_model, status, artifact_valid, promoted,
                    latest_evaluation_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    job_dir = excluded.job_dir,
                    base_model = excluded.base_model,
                    status = excluded.status,
                    artifact_valid = excluded.artifact_valid,
                    promoted = excluded.promoted,
                    latest_evaluation_json = excluded.latest_evaluation_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    str(job_dir),
                    base_model,
                    status,
                    int(artifact_valid),
                    promoted_value,
                    evaluation_json,
                    created_at,
                    now,
                ),
            )

    def _job_status(self, job_id: str, *, artifact_valid: bool) -> str:
        active = {}
        if self.active_adapter_path.exists():
            try:
                active = json.loads(self.active_adapter_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                active = {}
        if active.get("job_id") == job_id:
            return "promoted"
        job_dir = self._job_dir(job_id, must_exist=True)
        if (job_dir / "rollback.json").exists():
            return "rolled_back"
        report_path = job_dir / "evaluation_report.json"
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                return (
                    "evaluation_passed"
                    if report.get("promotion", {}).get("approved")
                    else "evaluation_failed"
                )
            except (OSError, json.JSONDecodeError):
                return "evaluation_failed"
        return "trained" if artifact_valid else "prepared"

    @staticmethod
    def _artifact_digest(audit: dict[str, Any]) -> str:
        files = [
            Path(str(path))
            for path in audit.get("artifact", {}).get("files", [])
            if str(path).strip()
        ]
        if not files:
            return ""
        digest = hashlib.sha256()
        for path in sorted(files, key=lambda item: str(item)):
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _holdout_digest(prompts: list[str]) -> str:
        digest = hashlib.sha256()
        for prompt in prompts:
            digest.update(canonicalize_prompt(prompt).encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _job_dir(self, job_id: str, *, must_exist: bool) -> Path:
        if not isinstance(job_id, str) or not re.fullmatch(r"lora_job_[A-Za-z0-9_-]+", job_id):
            raise ValueError("invalid LoRA job id")
        root = self.jobs_root.resolve()
        path = (root / job_id).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("LoRA job path escapes the training jobs directory") from exc
        if must_exist and not path.is_dir():
            raise KeyError(job_id)
        return path

    @staticmethod
    def _configured_job_path(job_dir: Path, configured_path: Any) -> Path:
        candidate = Path(str(configured_path)).expanduser()
        if not candidate.is_absolute():
            candidate = job_dir / candidate
        resolved_job_dir = job_dir.resolve()
        resolved = candidate.resolve()
        try:
            resolved.relative_to(resolved_job_dir)
        except ValueError as exc:
            raise ValueError("configured path is outside the job directory") from exc
        return resolved

    def _job_training_prompts(
        self,
        job_dir: Path,
        config: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        configured = config.get("sft_dataset_path")
        if not configured:
            return [], ["config is missing sft_dataset_path"]
        try:
            path = self._configured_job_path(job_dir, configured)
        except ValueError as exc:
            return [], [str(exc)]
        records, errors = self._read_jsonl(path)
        prompts: list[str] = []
        for index, record in enumerate(records):
            try:
                prompts.append(self._training_record_prompt(record))
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"invalid training record {index + 1}: {exc}")
        return prompts, errors

    def _job_holdout_prompts(
        self,
        job_dir: Path,
        config: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        preferred = job_dir / "eval_prompts.jsonl"
        try:
            path = (
                preferred
                if preferred.exists()
                else self._configured_job_path(job_dir, config.get("routing_eval_path", ""))
            )
        except ValueError as exc:
            return [], [str(exc)]
        records, errors = self._read_jsonl(path)
        prompts: list[str] = []
        for index, record in enumerate(records):
            prompt = record.get("input") if isinstance(record, dict) else None
            if not isinstance(prompt, str) or not prompt.strip():
                errors.append(f"invalid holdout record {index + 1}")
                continue
            prompts.append(prompt.strip())
        return prompts, errors

    @staticmethod
    def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
        if not path.is_file():
            return [], [f"missing dataset: {path.name}"]
        if path.stat().st_size > 32 * 1024 * 1024:
            return [], [f"dataset exceeds 32 MB safety limit: {path.name}"]
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"invalid JSONL at {path.name}:{line_number}: {exc}")
                    continue
                if not isinstance(record, dict):
                    errors.append(f"JSONL row must be an object at {path.name}:{line_number}")
                    continue
                records.append(record)
        except OSError as exc:
            errors.append(f"unable to read {path.name}: {exc}")
        return records, errors

    @staticmethod
    def _training_record_prompt(record: dict[str, Any]) -> str:
        messages = record.get("messages")
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        content = next(
            (
                item.get("content")
                for item in messages
                if isinstance(item, dict) and item.get("role") == "user"
            ),
            None,
        )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("record is missing a user prompt")
        prompt = content
        if "Owner request:" in prompt:
            prompt = prompt.split("Owner request:", 1)[1]
        if "\nTool id:" in prompt:
            prompt = prompt.split("\nTool id:", 1)[0]
        return prompt.strip()

    @staticmethod
    def _trainer_state_summary(job_dir: Path) -> dict[str, Any]:
        candidates = sorted(job_dir.rglob("trainer_state.json"))
        if not candidates:
            return {}
        path = candidates[-1]
        if path.stat().st_size > 8 * 1024 * 1024:
            return {"path": str(path), "error": "trainer state exceeds 8 MB safety limit"}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"path": str(path), "error": str(exc)}
        history = state.get("log_history", [])
        losses = [
            float(item["loss"])
            for item in history
            if isinstance(item, dict) and isinstance(item.get("loss"), (int, float))
        ]
        return {
            "path": str(path),
            "global_step": state.get("global_step"),
            "epoch": state.get("epoch"),
            "first_loss": losses[0] if losses else None,
            "last_loss": losses[-1] if losses else None,
        }

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)

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
            {
                "input": "Locate the newest quarterly budget workbook in my documents.",
                "expected_tool": "filesystem.resolve_file_request",
                "must_not_include": "",
            },
            {
                "input": "Open the second result from that document search.",
                "expected_tool": "filesystem.open_file_choice",
                "must_not_include": "",
            },
            {
                "input": "Compare mean order value by region in orders.xlsx.",
                "expected_tool": "spreadsheet.analyze",
                "must_not_include": "",
            },
            {
                "input": "Write a new Summary tab with regional totals in orders.xlsx.",
                "expected_tool": "spreadsheet.write_analysis",
                "must_not_include": "",
            },
            {
                "input": "Build a polished multi-page website for Northstar Dental with appointments.",
                "expected_tool": "code.generate_website",
                "must_not_include": "",
            },
            {
                "input": "Generate a Python CLI that renames photos by capture date.",
                "expected_tool": "code.generate_project",
                "must_not_include": "",
            },
            {
                "input": "Research three reliable guides for installing a monitor arm and open the best one.",
                "expected_tool": "browser.complete_goal",
                "must_not_include": "",
            },
            {
                "input": "Open https://docs.python.org in my browser.",
                "expected_tool": "windows.open_url",
                "must_not_include": "",
            },
            {
                "input": "Prepare a local LoRA training package but do not start training.",
                "expected_tool": "training.prepare_lora_job",
                "must_not_include": "",
            },
            {
                "input": "Outline the milestones needed for Project Q to reach stronger coding quality.",
                "expected_tool": "training.capability_plan",
                "must_not_include": "",
            },
            {
                "input": "Run Project Q's health checks and identify failures.",
                "expected_tool": "diagnostics.run_self_check",
                "must_not_include": "",
            },
            {
                "input": "Explain how photosynthesis stores solar energy.",
                "expected_tool": "knowledge.answer",
                "must_not_include": "",
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
        evaluator_path = Path(__file__).resolve().parents[1] / "lora_evaluator.py"
        return evaluator_path.read_text(encoding="utf-8")

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
