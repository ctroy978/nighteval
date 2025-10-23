"""Evaluation models enforced via Pydantic."""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


ScoreValue = Union[int, float, str]


def _normalise_score_token(value: ScoreValue) -> str:
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric.is_integer():
            return str(int(numeric))
        return format(numeric, "g")
    return str(value).strip()


def _as_float(value: ScoreValue) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class FeedbackExample(BaseModel):
    """Concrete excerpts referenced in the evaluation."""

    model_config = ConfigDict(extra="forbid")

    excerpt: str = Field(min_length=1)
    comment: str = Field(min_length=1)

    @field_validator("excerpt", "comment", mode="after")
    @classmethod
    def _ensure_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class OverallModel(BaseModel):
    """Optional numeric breakdown retained for backward compatibility."""

    model_config = ConfigDict(extra="forbid")

    points_earned: int = Field(ge=0)
    points_possible: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> "OverallModel":
        if self.points_earned > self.points_possible:
            raise ValueError("overall points_earned cannot exceed points_possible")
        return self


class EvaluationCriterion(BaseModel):
    """Per-criterion evaluation with rubric-aware validation."""

    model_config = ConfigDict(extra="forbid")

    id: str
    criterion: str = Field(default="")
    description: str = Field(default="")
    assigned_level: str = Field(min_length=1)
    score: ScoreValue
    examples: List[FeedbackExample] = Field(min_length=2, max_length=2)
    improvement_suggestion: str = Field(min_length=1)

    @field_validator("criterion", mode="after")
    @classmethod
    def _trim_criterion(cls, value: str) -> str:
        return value.strip()

    @field_validator("description", mode="after")
    @classmethod
    def _trim_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("assigned_level", "improvement_suggestion", mode="after")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_against_rubric(self, info: ValidationInfo) -> "EvaluationCriterion":
        context: Dict[str, Dict[str, object]] = {}
        if info.context:
            context = info.context.get("criterion_context", {})  # type: ignore[assignment]

        criterion_context = context.get(self.id)
        if criterion_context is None:
            # If rubric context was provided we should not see an unknown id
            if context:
                raise ValueError(f"Unexpected criterion id '{self.id}'")
            return self

        allowed_scores: Optional[Set[str]] = criterion_context.get("allowed_scores")  # type: ignore[assignment]
        max_numeric: Optional[float] = criterion_context.get("max_numeric")  # type: ignore[assignment]

        if not self.criterion:
            label = criterion_context.get("criterion_label")
            if label:
                self = self.model_copy(update={"criterion": str(label)})
        if not self.description:
            description = criterion_context.get("criterion_description")
            if description:
                self = self.model_copy(update={"description": str(description)})

        token = _normalise_score_token(self.score)
        if allowed_scores and token not in allowed_scores:
            raise ValueError(
                f"Score '{self.score}' for criterion '{self.id}' not in rubric scale"
            )

        numeric_value = _as_float(self.score)
        if max_numeric is not None and numeric_value is not None and numeric_value > max_numeric + 1e-6:
            raise ValueError(
                f"Score {self.score} for criterion '{self.id}' exceeds max {max_numeric}"
            )

        if not self.criterion:
            raise ValueError(f"Criterion name missing for id '{self.id}'")
        if not self.description:
            raise ValueError(f"Criterion description missing for id '{self.id}'")

        return self


class EvaluationModel(BaseModel):
    """Complete evaluation payload produced by the AI model."""

    model_config = ConfigDict(extra="forbid")

    overall_score: ScoreValue
    summary: str = Field(min_length=1)
    criteria: List[EvaluationCriterion]
    overall: Optional[OverallModel] = None

    @field_validator("overall_score", mode="after")
    @classmethod
    def _validate_overall_score(cls, value: ScoreValue) -> ScoreValue:
        if isinstance(value, str) and not value.strip():
            raise ValueError("overall_score must not be blank")
        return value

    @field_validator("summary", mode="after")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be blank")
        return value

    @model_validator(mode="after")
    def _check_coverage(self, info: ValidationInfo) -> "EvaluationModel":
        rubric_ids: Set[str] | None = None
        overall_possible: Optional[float] = None
        criterion_context: Dict[str, Dict[str, object]] = {}
        if info.context:
            rubric_ids = info.context.get("rubric_ids")  # type: ignore[assignment]
            overall_possible = info.context.get("overall_possible")  # type: ignore[assignment]
            criterion_context = info.context.get("criterion_context", {})  # type: ignore[assignment]

        criterion_ids = {criterion.id for criterion in self.criteria}
        if rubric_ids is not None:
            missing = rubric_ids - criterion_ids
            extra = criterion_ids - rubric_ids
            coverage_errors: List[str] = []
            if missing:
                missing_list = ", ".join(sorted(missing))
                coverage_errors.append(f"missing rubric ids: {missing_list}")
            if extra:
                extra_list = ", ".join(sorted(extra))
                coverage_errors.append(f"unknown rubric ids: {extra_list}")
            if coverage_errors:
                raise ValueError("Evaluation has " + "; ".join(coverage_errors))

        all_numeric = True
        total_numeric = 0.0
        for criterion in self.criteria:
            numeric_score = _as_float(criterion.score)
            if numeric_score is None:
                all_numeric = False
                continue
            total_numeric += numeric_score
            # second guard: if rubric context present but missing entry, raise early
            if rubric_ids and criterion.id not in rubric_ids:
                raise ValueError(f"Unexpected criterion id '{criterion.id}'")
            crit_context = criterion_context.get(criterion.id)
            if crit_context and crit_context.get("max_numeric") is not None:
                max_numeric = float(crit_context["max_numeric"])
                if numeric_score > max_numeric + 1e-6:
                    raise ValueError(
                        f"Score {criterion.score} for criterion '{criterion.id}' exceeds max {max_numeric}"
                    )

        if self.overall is not None and all_numeric:
            if abs(self.overall.points_earned - total_numeric) > 1e-6:
                raise ValueError(
                    "overall.points_earned must equal the sum of all numeric criterion scores"
                )

        if self.overall is not None and overall_possible is not None:
            if abs(self.overall.points_possible - overall_possible) > 1e-6:
                raise ValueError(
                    "overall.points_possible must equal the sum of rubric max scores"
                )

        return self
