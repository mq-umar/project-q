from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Generic, TypeVar


RecordT = TypeVar("RecordT")


def canonicalize_prompt(prompt: str) -> str:
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    normalized = unicodedata.normalize("NFKC", prompt).casefold()
    words: list[str] = []
    pending_space = False
    for character in normalized:
        if character.isalnum():
            if pending_space and words:
                words.append(" ")
            words.append(character)
            pending_space = False
        else:
            pending_space = True
    return "".join(words)


normalize_prompt = canonicalize_prompt


@dataclass(frozen=True, slots=True)
class LeakageMatch:
    match_type: str
    train_index: int
    holdout_index: int
    training_prompt: str
    holdout_prompt: str
    canonical_prompt: str

    def to_dict(self) -> dict[str, Any]:
        return to_json_safe(self)


@dataclass(frozen=True, slots=True)
class LeakageReport:
    matches: tuple[LeakageMatch, ...] = ()

    @property
    def exact_matches(self) -> tuple[LeakageMatch, ...]:
        return tuple(match for match in self.matches if match.match_type == "exact")

    @property
    def normalized_matches(self) -> tuple[LeakageMatch, ...]:
        return tuple(match for match in self.matches if match.match_type == "normalized")

    @property
    def has_leakage(self) -> bool:
        return bool(self.matches)

    @property
    def is_clean(self) -> bool:
        return not self.matches

    def to_dict(self) -> dict[str, Any]:
        payload = to_json_safe(self)
        payload.update(
            {
                "exact_match_count": len(self.exact_matches),
                "normalized_match_count": len(self.normalized_matches),
                "has_leakage": self.has_leakage,
                "is_clean": self.is_clean,
            }
        )
        return payload


def detect_prompt_leakage(
    training_prompts: Iterable[str],
    holdout_prompts: Iterable[str],
) -> LeakageReport:
    training = tuple(training_prompts)
    holdout = tuple(holdout_prompts)
    exact_indexes: dict[str, int] = {}
    normalized_indexes: dict[str, int] = {}

    for index, prompt in enumerate(training):
        canonical = _required_canonical_prompt(prompt)
        exact_indexes.setdefault(prompt, index)
        normalized_indexes.setdefault(canonical, index)

    matches: list[LeakageMatch] = []
    for holdout_index, prompt in enumerate(holdout):
        canonical = _required_canonical_prompt(prompt)
        if prompt in exact_indexes:
            train_index = exact_indexes[prompt]
            match_type = "exact"
        elif canonical in normalized_indexes:
            train_index = normalized_indexes[canonical]
            match_type = "normalized"
        else:
            continue
        matches.append(
            LeakageMatch(
                match_type=match_type,
                train_index=train_index,
                holdout_index=holdout_index,
                training_prompt=training[train_index],
                holdout_prompt=prompt,
                canonical_prompt=canonical,
            )
        )
    return LeakageReport(matches=tuple(matches))


@dataclass(frozen=True, slots=True)
class DatasetSplit(Generic[RecordT]):
    train: tuple[RecordT, ...]
    validation: tuple[RecordT, ...]
    holdout: tuple[RecordT, ...]

    def to_dict(self) -> dict[str, Any]:
        return to_json_safe(self)


def deterministic_disjoint_split(
    records: Iterable[RecordT],
    *,
    validation_fraction: float = 0.1,
    holdout_fraction: float = 0.2,
    seed: str | int | bytes = "project-q",
    prompt_key: str = "input",
    prompt_getter: Callable[[RecordT], str] | None = None,
) -> DatasetSplit[RecordT]:
    validation_fraction = _fraction(validation_fraction, "validation_fraction")
    holdout_fraction = _fraction(holdout_fraction, "holdout_fraction")
    if validation_fraction + holdout_fraction >= 1.0:
        raise ValueError("validation and holdout fractions must sum to less than 1")

    groups: dict[str, list[RecordT]] = {}
    for record in records:
        prompt = _record_prompt(record, prompt_key=prompt_key, prompt_getter=prompt_getter)
        canonical = _required_canonical_prompt(prompt)
        groups.setdefault(canonical, []).append(record)

    ordered_groups = sorted(
        groups.items(),
        key=lambda item: _split_digest(seed, item[0]),
    )
    group_count = len(ordered_groups)
    holdout_count = _split_count(group_count, holdout_fraction)
    validation_count = _split_count(group_count, validation_fraction)

    holdout_groups = ordered_groups[:holdout_count]
    validation_end = holdout_count + validation_count
    validation_groups = ordered_groups[holdout_count:validation_end]
    train_groups = ordered_groups[validation_end:]
    return DatasetSplit(
        train=_flatten_groups(train_groups),
        validation=_flatten_groups(validation_groups),
        holdout=_flatten_groups(holdout_groups),
    )


deterministic_split = deterministic_disjoint_split


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    score: float
    latency_ms: float
    failures: int = 0
    evaluated_prompts: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        score = float(self.score)
        latency_ms = float(self.latency_ms)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("score must be a finite number between 0 and 1")
        if not math.isfinite(latency_ms) or latency_ms < 0.0:
            raise ValueError("latency_ms must be a finite non-negative number")
        if isinstance(self.failures, bool) or int(self.failures) != self.failures or self.failures < 0:
            raise ValueError("failures must be a non-negative integer")
        if (
            isinstance(self.evaluated_prompts, bool)
            or int(self.evaluated_prompts) != self.evaluated_prompts
            or self.evaluated_prompts < 0
        ):
            raise ValueError("evaluated_prompts must be a non-negative integer")
        if self.evaluated_prompts and self.failures > self.evaluated_prompts:
            raise ValueError("failures cannot exceed evaluated_prompts")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "latency_ms", latency_ms)
        object.__setattr__(self, "failures", int(self.failures))
        object.__setattr__(self, "evaluated_prompts", int(self.evaluated_prompts))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def failure_count(self) -> int:
        return self.failures

    def to_dict(self) -> dict[str, Any]:
        return to_json_safe(self)


@dataclass(frozen=True, slots=True)
class BaseEvaluationResult(EvaluationResult):
    pass


@dataclass(frozen=True, slots=True)
class AdapterEvaluationResult(EvaluationResult):
    pass


@dataclass(frozen=True, slots=True)
class EvaluationComparison:
    base: BaseEvaluationResult
    adapter: AdapterEvaluationResult
    score_delta: float
    latency_delta_ms: float
    failure_delta: int

    @property
    def score_improved(self) -> bool:
        return self.score_delta > 0.0

    @property
    def latency_improved(self) -> bool:
        return self.latency_delta_ms < 0.0

    @property
    def failures_improved(self) -> bool:
        return self.failure_delta < 0

    def to_dict(self) -> dict[str, Any]:
        payload = to_json_safe(self)
        payload.update(
            {
                "score_improved": self.score_improved,
                "latency_improved": self.latency_improved,
                "failures_improved": self.failures_improved,
            }
        )
        return payload


def compare_evaluation_results(
    base: BaseEvaluationResult,
    adapter: AdapterEvaluationResult,
) -> EvaluationComparison:
    if not isinstance(base, BaseEvaluationResult):
        raise TypeError("base must be a BaseEvaluationResult")
    if not isinstance(adapter, AdapterEvaluationResult):
        raise TypeError("adapter must be an AdapterEvaluationResult")
    return EvaluationComparison(
        base=base,
        adapter=adapter,
        score_delta=adapter.score - base.score,
        latency_delta_ms=adapter.latency_ms - base.latency_ms,
        failure_delta=adapter.failures - base.failures,
    )


compare_results = compare_evaluation_results


@dataclass(frozen=True, slots=True)
class PromotionGateConfig:
    minimum_score: float = 0.8
    maximum_score_regression: float = 0.0
    maximum_latency_increase_ms: float | None = None
    maximum_failure_increase: int | None = None

    def __post_init__(self) -> None:
        minimum_score = float(self.minimum_score)
        maximum_score_regression = float(self.maximum_score_regression)
        if not math.isfinite(minimum_score) or not 0.0 <= minimum_score <= 1.0:
            raise ValueError("minimum_score must be between 0 and 1")
        if (
            not math.isfinite(maximum_score_regression)
            or not 0.0 <= maximum_score_regression <= 1.0
        ):
            raise ValueError("maximum_score_regression must be between 0 and 1")
        if self.maximum_latency_increase_ms is not None:
            maximum_latency_increase_ms = float(self.maximum_latency_increase_ms)
            if not math.isfinite(maximum_latency_increase_ms) or maximum_latency_increase_ms < 0:
                raise ValueError("maximum_latency_increase_ms must be non-negative")
            object.__setattr__(
                self,
                "maximum_latency_increase_ms",
                maximum_latency_increase_ms,
            )
        if self.maximum_failure_increase is not None:
            if (
                isinstance(self.maximum_failure_increase, bool)
                or int(self.maximum_failure_increase) != self.maximum_failure_increase
                or self.maximum_failure_increase < 0
            ):
                raise ValueError("maximum_failure_increase must be a non-negative integer")
            object.__setattr__(
                self,
                "maximum_failure_increase",
                int(self.maximum_failure_increase),
            )
        object.__setattr__(self, "minimum_score", minimum_score)
        object.__setattr__(self, "maximum_score_regression", maximum_score_regression)


PromotionPolicy = PromotionGateConfig


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promote: bool
    checks: Mapping[str, bool]
    reasons: tuple[str, ...]
    config: PromotionGateConfig

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", dict(self.checks))
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def approved(self) -> bool:
        return self.promote

    def to_dict(self) -> dict[str, Any]:
        payload = to_json_safe(self)
        payload["approved"] = self.approved
        return payload


def evaluate_promotion(
    base: BaseEvaluationResult,
    adapter: AdapterEvaluationResult,
    *,
    artifact_valid: bool,
    leakage: LeakageReport,
    config: PromotionGateConfig | None = None,
) -> PromotionDecision:
    comparison = compare_evaluation_results(base, adapter)
    if not isinstance(leakage, LeakageReport):
        raise TypeError("leakage must be a LeakageReport")
    policy = config or PromotionGateConfig()
    if not isinstance(policy, PromotionGateConfig):
        raise TypeError("config must be a PromotionGateConfig")

    checks = {
        "artifact_valid": bool(artifact_valid),
        "no_leakage": leakage.is_clean,
        "minimum_score": adapter.score >= policy.minimum_score,
        "no_baseline_regression": (
            comparison.score_delta >= -policy.maximum_score_regression
        ),
    }
    if policy.maximum_latency_increase_ms is not None:
        checks["latency_budget"] = (
            comparison.latency_delta_ms <= policy.maximum_latency_increase_ms
        )
    if policy.maximum_failure_increase is not None:
        checks["failure_budget"] = (
            comparison.failure_delta <= policy.maximum_failure_increase
        )

    messages = {
        "artifact_valid": "adapter artifact validation failed",
        "no_leakage": "training prompts leaked into the holdout set",
        "minimum_score": (
            f"adapter score {adapter.score:.6g} is below minimum "
            f"{policy.minimum_score:.6g}"
        ),
        "no_baseline_regression": (
            f"adapter score regressed by {-comparison.score_delta:.6g}, exceeding "
            f"the allowed {policy.maximum_score_regression:.6g}"
        ),
        "latency_budget": (
            f"adapter latency increased by {comparison.latency_delta_ms:.6g} ms"
        ),
        "failure_budget": (
            f"adapter failures increased by {comparison.failure_delta}"
        ),
    }
    reasons = tuple(messages[name] for name, passed in checks.items() if not passed)
    return PromotionDecision(
        promote=all(checks.values()),
        checks=checks,
        reasons=reasons,
        config=policy,
    )


promotion_gate = evaluate_promotion


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    comparison: EvaluationComparison
    leakage: LeakageReport
    promotion: PromotionDecision
    artifact_valid: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.comparison, EvaluationComparison):
            raise TypeError("comparison must be an EvaluationComparison")
        if not isinstance(self.leakage, LeakageReport):
            raise TypeError("leakage must be a LeakageReport")
        if not isinstance(self.promotion, PromotionDecision):
            raise TypeError("promotion must be a PromotionDecision")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "artifact_valid", bool(self.artifact_valid))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return to_json_safe(self)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            allow_nan=False,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )


def to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        converted = float(value)
        return converted if math.isfinite(converted) else None
    if isinstance(value, Enum):
        return to_json_safe(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        payload = {
            item.name: to_json_safe(getattr(value, item.name))
            for item in fields(value)
        }
        if isinstance(value, LeakageReport):
            payload.update(
                {
                    "exact_match_count": len(value.exact_matches),
                    "normalized_match_count": len(value.normalized_matches),
                    "has_leakage": value.has_leakage,
                    "is_clean": value.is_clean,
                }
            )
        elif isinstance(value, EvaluationComparison):
            payload.update(
                {
                    "score_improved": value.score_improved,
                    "latency_improved": value.latency_improved,
                    "failures_improved": value.failures_improved,
                }
            )
        elif isinstance(value, PromotionDecision):
            payload["approved"] = value.approved
        return payload
    if isinstance(value, Mapping):
        return {
            str(key): to_json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (set, frozenset)):
        converted = [to_json_safe(item) for item in value]
        return sorted(
            converted,
            key=lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True),
        )
    if isinstance(value, (list, tuple)):
        return [to_json_safe(item) for item in value]
    return str(value)


serialize_report = to_json_safe


def _required_canonical_prompt(prompt: str) -> str:
    canonical = canonicalize_prompt(prompt)
    if not canonical:
        raise ValueError("prompt must contain at least one letter or number")
    return canonical


def _record_prompt(
    record: RecordT,
    *,
    prompt_key: str,
    prompt_getter: Callable[[RecordT], str] | None,
) -> str:
    if prompt_getter is not None:
        return prompt_getter(record)
    if isinstance(record, str):
        return record
    if isinstance(record, Mapping):
        if prompt_key not in record:
            raise ValueError(f"record is missing prompt key {prompt_key!r}")
        return record[prompt_key]
    raise TypeError("records must be strings, mappings, or use prompt_getter")


def _fraction(value: float, label: str) -> float:
    fraction = float(value)
    if not math.isfinite(fraction) or not 0.0 <= fraction < 1.0:
        raise ValueError(f"{label} must be between 0 and 1")
    return fraction


def _split_digest(seed: str | int | bytes, canonical_prompt: str) -> bytes:
    if isinstance(seed, bytes):
        seed_bytes = seed
    else:
        seed_bytes = str(seed).encode("utf-8")
    return hashlib.sha256(seed_bytes + b"\0" + canonical_prompt.encode("utf-8")).digest()


def _split_count(group_count: int, fraction: float) -> int:
    count = int(group_count * fraction)
    if fraction > 0.0 and group_count >= 3 and count == 0:
        return 1
    return count


def _flatten_groups(
    groups: Iterable[tuple[str, list[RecordT]]],
) -> tuple[RecordT, ...]:
    return tuple(record for _, records in groups for record in records)
