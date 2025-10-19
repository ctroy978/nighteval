"""Rubric models with validation helpers."""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RubricCriterion(BaseModel):
    """Single rubric criterion description."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    max_score: int = Field(gt=0)
    name: Optional[str] = None
    descriptors: Optional[Dict[str, str]] = None


class RubricModel(BaseModel):
    """Rubric definition validated via Pydantic."""

    model_config = ConfigDict(extra="forbid")

    criteria: List[RubricCriterion]
    overall_points_possible: Optional[int] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_internal(self) -> "RubricModel":
        seen: Set[str] = set()
        for criterion in self.criteria:
            identifier = criterion.id.strip()
            if not identifier:
                raise ValueError("Criterion id must not be blank")
            if identifier in seen:
                raise ValueError(f"Duplicate criterion id detected: {identifier}")
            seen.add(identifier)

        if not self.criteria:
            raise ValueError("Rubric must include at least one criterion")

        total = sum(item.max_score for item in self.criteria)
        if (
            self.overall_points_possible is not None
            and self.overall_points_possible != total
        ):
            raise ValueError(
                "overall_points_possible must equal the sum of all criterion max_score values"
            )
        return self

    @property
    def id_set(self) -> Set[str]:
        return {criterion.id for criterion in self.criteria}

    @property
    def score_map(self) -> Dict[str, int]:
        return {criterion.id: criterion.max_score for criterion in self.criteria}

    @property
    def points_possible(self) -> int:
        if self.overall_points_possible is not None:
            return self.overall_points_possible
        return sum(item.max_score for item in self.criteria)

    def validation_context(self) -> Dict[str, object]:
        """Context dictionary consumed by evaluation validation."""

        return {
            "rubric_ids": self.id_set,
            "rubric_scores": self.score_map,
            "overall_possible": self.points_possible,
        }
