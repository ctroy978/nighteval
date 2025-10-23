"""Rubric models with validation helpers."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ScoreValue = Union[int, float, str]


def _normalise_score_token(value: ScoreValue) -> str:
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric.is_integer():
            return str(int(numeric))
        return format(numeric, "g")
    return str(value).strip()


def _extract_numeric(value: str) -> Optional[float]:
    try:
        return float(value)
    except ValueError:
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


class RubricLevel(BaseModel):
    """Representation of a single rubric level."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    score: Optional[ScoreValue] = None

    @field_validator("name", "description", mode="after")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @property
    def score_token(self) -> Optional[str]:
        if self.score is None:
            return None
        return _normalise_score_token(self.score)

    @property
    def numeric_score(self) -> Optional[float]:
        if self.score is None:
            return _extract_numeric(self.name)
        if isinstance(self.score, (int, float)):
            return float(self.score)
        if isinstance(self.score, str):
            return _extract_numeric(self.score)
        return None


class RubricCriterion(BaseModel):
    """Single rubric criterion description."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    max_score: Optional[float] = Field(default=None, gt=0)
    name: Optional[str] = None
    description: Optional[str] = None
    descriptors: Optional[Dict[str, str]] = None
    levels: Optional[List[RubricLevel]] = None

    @field_validator("name", "description", mode="after")
    @classmethod
    def _strip_optional(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _populate_levels(self) -> "RubricCriterion":
        if not self.levels and self.descriptors:
            derived: List[RubricLevel] = []
            for key, description in self.descriptors.items():
                level = RubricLevel(name=str(key), description=description, score=key)
                derived.append(level)
            object.__setattr__(self, "levels", derived)
        if not self.levels:
            raise ValueError(
                f"Criterion '{self.id}' must include at least one level or descriptor"
            )

        if self.max_score is None:
            numeric_candidates = [level.numeric_score for level in self.levels or []]
            numeric_values = [value for value in numeric_candidates if value is not None]
            if numeric_values:
                object.__setattr__(self, "max_score", max(numeric_values))

        return self

    @property
    def max_numeric_score(self) -> Optional[float]:
        if self.max_score is not None:
            return float(self.max_score)
        numeric_candidates = [level.numeric_score for level in self.levels or []]
        numeric_values = [value for value in numeric_candidates if value is not None]
        if numeric_values:
            return max(numeric_values)
        return None

    @property
    def allowed_score_tokens(self) -> Set[str]:
        tokens: Set[str] = set()
        for level in self.levels or []:
            token = level.score_token
            if token:
                tokens.add(token)
        if not tokens and self.max_score is not None:
            try:
                upper = int(self.max_score)
                for value in range(0, upper + 1):
                    tokens.add(_normalise_score_token(value))
            except (TypeError, ValueError):
                pass
        return tokens

    @property
    def top_level(self) -> Optional[RubricLevel]:
        if not self.levels:
            return None
        numeric_levels = sorted(
            ((level.numeric_score or float("-inf"), index, level) for index, level in enumerate(self.levels)),
            key=lambda item: (item[0], item[1]),
        )
        if numeric_levels and numeric_levels[-1][0] != float("-inf"):
            return numeric_levels[-1][2]
        # fall back to first declared level if no numeric ordering available
        return self.levels[0]


class RubricModel(BaseModel):
    """Rubric definition validated via Pydantic."""

    model_config = ConfigDict(extra="forbid")

    criteria: List[RubricCriterion]
    overall_points_possible: Optional[float] = Field(default=None, gt=0)

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

        numeric_totals: List[float] = []
        all_numeric = True
        for item in self.criteria:
            score = item.max_numeric_score
            if score is None:
                all_numeric = False
            else:
                numeric_totals.append(score)

        if self.overall_points_possible is not None and all_numeric:
            total = sum(numeric_totals)
            if abs(self.overall_points_possible - total) > 1e-6:
                raise ValueError(
                    "overall_points_possible must equal the sum of all criterion max scores"
                )
        return self

    @property
    def id_set(self) -> Set[str]:
        return {criterion.id for criterion in self.criteria}

    @property
    def score_map(self) -> Dict[str, float]:
        mapping: Dict[str, float] = {}
        for criterion in self.criteria:
            max_numeric = criterion.max_numeric_score
            if max_numeric is not None:
                mapping[criterion.id] = float(max_numeric)
        return mapping

    @property
    def points_possible(self) -> Optional[float]:
        if self.overall_points_possible is not None:
            return float(self.overall_points_possible)
        numeric_values = [item.max_numeric_score for item in self.criteria]
        if any(value is None for value in numeric_values):
            return None
        return float(sum(value for value in numeric_values if value is not None))

    def validation_context(self) -> Dict[str, object]:
        """Context dictionary consumed by evaluation validation."""

        criterion_context: Dict[str, Dict[str, object]] = {}
        for criterion in self.criteria:
            criterion_context[criterion.id] = {
                "allowed_scores": criterion.allowed_score_tokens,
                "max_numeric": criterion.max_numeric_score,
                "top_level_name": criterion.top_level.name if criterion.top_level else None,
                "top_level_description": criterion.top_level.description if criterion.top_level else None,
                "criterion_label": criterion.name or criterion.id,
                "criterion_description": criterion.description or (criterion.name or ""),
            }

        return {
            "rubric_ids": self.id_set,
            "rubric_scores": self.score_map,
            "overall_possible": self.points_possible,
            "criterion_context": criterion_context,
        }
