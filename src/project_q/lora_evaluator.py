from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any


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


def extract_tool_id(text: str, tool_ids: Iterable[str] = TOOL_IDS) -> str:
    content = str(text or "")
    for tool_id in tool_ids:
        pattern = rf"(?<![A-Za-z0-9_.-]){re.escape(tool_id)}(?![A-Za-z0-9_.-])"
        if re.search(pattern, content):
            return tool_id
    return ""


def evaluate_records(
    records: list[dict[str, Any]],
    *,
    generate: Callable[[str], str],
    tool_ids: Iterable[str] = TOOL_IDS,
    clock_values: Iterator[float] | None = None,
) -> dict[str, Any]:
    known_tools = list(tool_ids)
    clock = (lambda: next(clock_values)) if clock_values is not None else time.perf_counter
    results: list[dict[str, Any]] = []
    total_latency_ms = 0.0
    passed = 0
    for record in records:
        prompt = str(record["input"]).strip()
        expected = str(record["expected_tool"]).strip()
        started = clock()
        output = str(generate(prompt))
        elapsed_ms = max(0.0, (clock() - started) * 1000.0)
        selected = extract_tool_id(output, known_tools)
        item_passed = selected == expected
        passed += int(item_passed)
        total_latency_ms += elapsed_ms
        results.append(
            {
                "input": prompt,
                "expected_tool": expected,
                "selected_tool": selected,
                "passed": item_passed,
                "latency_ms": elapsed_ms,
                "output": output[-500:],
            }
        )
    count = len(records)
    return {
        "score": passed / count if count else 0.0,
        "latency_ms": total_latency_ms / count if count else 0.0,
        "failures": count - passed,
        "evaluated_prompts": count,
        "passed": passed,
        "results": results,
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or not record.get("input") or not record.get("expected_tool"):
            raise ValueError(f"invalid evaluation record at line {line_number}")
        records.append(record)
    if not records:
        raise ValueError("evaluation suite is empty")
    return records


def build_prompt(tokenizer: Any, owner_request: str) -> str:
    messages = [
        {
            "role": "system",
            "content": "You are the Project Q tool router. Return exactly one Project Q tool id and no explanation.",
        },
        {
            "role": "user",
            "content": (
                "Return exactly one tool id from this list and no explanation:\n"
                + "\n".join(TOOL_IDS)
                + f"\nOwner request: {owner_request}\nTool id:"
            ),
        },
    ]
    if hasattr(tokenizer, "apply_chat_template"):
        return tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
    return "\n".join(item["content"] for item in messages) + "\nassistant:"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--holdout", default="eval_prompts.jsonl")
    parser.add_argument("--output", default="clean_holdout_results.json")
    parser.add_argument("--max-new-tokens", type=int, default=16)
    parser.add_argument("--allow-download", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    records = load_jsonl(Path(args.holdout).expanduser().resolve())
    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise SystemExit("Install torch, transformers, and peft before model evaluation.") from exc

    local_only = not args.allow_download
    tokenizer = AutoTokenizer.from_pretrained(
        config["base_model"],
        trust_remote_code=True,
        local_files_only=local_only,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        config["base_model"],
        trust_remote_code=True,
        local_files_only=local_only,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto" if torch.cuda.is_available() else None,
    )
    model.eval()

    def generate(owner_request: str) -> str:
        prompt = build_prompt(tokenizer, owner_request)
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            output = model.generate(
                **inputs,
                max_new_tokens=max(1, args.max_new_tokens),
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )
        generated = output[0][inputs["input_ids"].shape[-1] :]
        return tokenizer.decode(generated, skip_special_tokens=True).strip()

    base = evaluate_records(records, generate=generate)
    adapter_model = PeftModel.from_pretrained(
        model,
        config["output_adapter_dir"],
        local_files_only=local_only,
    )
    adapter_model.eval()
    model = adapter_model
    adapter = evaluate_records(records, generate=generate)
    payload = {
        "base_metrics": {key: base[key] for key in ("score", "latency_ms", "failures", "evaluated_prompts")},
        "adapter_metrics": {
            key: adapter[key]
            for key in ("score", "latency_ms", "failures", "evaluated_prompts")
        },
        "holdout_prompts": [record["input"] for record in records],
        "base_results": base["results"],
        "adapter_results": adapter["results"],
        "environment": {
            "base_model": config["base_model"],
            "adapter_dir": config["output_adapter_dir"],
            "device": str(model.device),
            "torch_version": torch.__version__,
            "local_files_only": local_only,
        },
    }
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
