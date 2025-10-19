"""Evaluation models enforced via Pydantic."""

from __future__ import annotations

from typing import Dict, List, Set

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator


class EvidenceModel(BaseModel):
    """Student evidence quoted by the evaluator."""

    model_config = ConfigDict(extra="forbid")

    quote: str = Field(min_length=1)


class OverallModel(BaseModel):
    """Overall scoring summary."""

    model_config = ConfigDict(extra="forbid")

    points_earned: int = Field(ge=0)
    points_possible: int = Field(gt=0)


class EvaluationCriterion(BaseModel):
    """Per-criterion evaluation with bounds enforced by rubric context."""

    model_config = ConfigDict(extra="forbid")

    id: str
    score: int = Field(ge=0)
    evidence: EvidenceModel
    explanation: str = Field(min_length=1)
    advice: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_score(self, info: ValidationInfo) -> "EvaluationCriterion":
        rubric_scores: Dict[str, int] | None = None
        if info.context:
            rubric_scores = info.context.get("rubric_scores")  # type: ignore[assignment]
        if rubric_scores is None:
            return self

        if self.id not in rubric_scores:
            raise ValueError(f"Unexpected criterion id '{self.id}'")

        max_score = rubric_scores[self.id]
        if self.score > max_score:
            raise ValueError(
                f"Score {self.score} for criterion '{self.id}' exceeds max {max_score}"
            )
        return self


class EvaluationModel(BaseModel):
    """Complete evaluation payload produced by the AI model."""

    model_config = ConfigDict(extra="forbid")

    overall: OverallModel
    criteria: List[EvaluationCriterion]

    @model_validator(mode="after")
    def _check_coverage(self, info: ValidationInfo) -> "EvaluationModel":
        rubric_ids: Set[str] | None = None
        overall_possible: int | None = None
        if info.context:
            rubric_ids = info.context.get("rubric_ids")  # type: ignore[assignment]
            overall_possible = info.context.get("overall_possible")  # type: ignore[assignment]

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

        total_earned = sum(criterion.score for criterion in self.criteria)
        if total_earned != self.overall.points_earned:
            raise ValueError(
                "overall.points_earned must equal the sum of all criterion scores"
            )

        if overall_possible is not None and self.overall.points_possible != overall_possible:
            raise ValueError(
                "overall.points_possible must equal the sum of rubric max scores"
            )

        if self.overall.points_earned > self.overall.points_possible:
            raise ValueError("overall.points_earned cannot exceed overall.points_possible")

        return self
