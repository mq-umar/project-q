# Project Q LoRA Job Audit

## Job

- Job ID: `lora_job_e5db0a2a9c95`
- Base model: `Qwen/Qwen2.5-Coder-1.5B-Instruct`
- GPU: NVIDIA GeForce RTX 3060 Ti, 8 GB
- Adapter: `adapter_model.safetensors`, 73,911,112 bytes
- Training completed: step 120 of 120, about 5.47 epochs
- Training data: 171 SFT records, 10 preference records

## Artifact Validation

The adapter, tokenizer, configuration, checkpoint, optimizer, scheduler, and trainer state are present. The adapter loads offline with the configured base model and generates valid Project Q tool IDs.

Training loss fell from about 4.18 at step 5 to 0.076 at step 120. Training token accuracy rose from about 0.39 to 0.97. These are training metrics only and do not establish generalization.

## Original Evaluation Defect

The bundled evaluator reported 6/6 correct tool routes. That score is not a valid promotion signal:

- Five of the six evaluation prompts appear verbatim in the SFT training set.
- Several leaked prompts occur multiple times.
- The job had no validation split, validation loss, or best-checkpoint selection.

The result proves that the adapter loads and reproduces trained routing behavior, but it does not prove improvement on unseen requests.

## Clean Holdout Comparison

A separate 12-prompt routing set was created after training. No prompt had an exact or normalized match in the SFT data. Both lanes used the same system prompt, allowed tool list, deterministic decoding, cached base model, and 16-token output limit.

| Lane | Correct | Accuracy | Generation Time |
|---|---:|---:|---:|
| Base model | 5/12 | 41.7% | 4.03 s |
| LoRA adapter | 9/12 | 75.0% | 6.53 s |

The adapter improved unseen routing by 4 prompts, or 33.3 percentage points.

Remaining adapter errors:

- Multi-step browser search was routed to `windows.open_url` instead of `browser.complete_goal`.
- Website creation was routed to `code.generate_project` instead of `code.generate_website`.
- Preparing a training package was routed to `training.capability_plan` instead of `training.prepare_lora_job`.

## Decision

Status: **trained, promising, not promoted**

The adapter is a useful candidate and shows real improvement, but Project Q must not auto-enable it yet. Promotion requires:

1. A generated train/validation/holdout split with leakage rejection.
2. A larger stable routing and safety suite.
3. Base-versus-adapter comparison persisted with outputs and environment metadata.
4. No regression on browser, code generation, approvals, and safety routing.
5. Explicit owner confirmation with a recorded rollback target.

The next dataset should add clean examples for the three remaining confusion pairs and reduce repeated near-duplicate router examples.

## Reproducible Lifecycle Evaluation

Project Q now includes a versioned, zero-leak 12-prompt suite and an offline base-versus-adapter runner. A fresh run on June 14, 2026 produced:

| Lane | Correct | Accuracy | Mean Generation Time |
|---|---:|---:|---:|
| Base model | 6/12 | 50.0% | 332.8 ms |
| LoRA adapter | 10/12 | 83.3% | 491.7 ms |

The adapter missed `filesystem.open_file_choice` and `training.capability_plan`. Project Q persisted the comparison, artifacts, outputs, environment, and clean-leakage result. The configured promotion minimum is 85%, so the job remains inactive.
