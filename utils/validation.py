"""Validation helpers for AI evaluation payloads."""

from __future__ import annotations

from typing import Any, Dict, Iterable


class EvaluationValidationError(ValueError):
    """Raised when an evaluation payload is missing required fields."""


def validate_evaluation_payload(payload: Dict[str, Any], rubric: Dict[str, Any]) -> None:
    """Raise if the evaluation payload is not structurally sound."""

    if not isinstance(payload, dict):
        raise EvaluationValidationError("Evaluation payload must be a dictionary")

    _validate_overall(payload.get("overall"))
    _validate_criteria(payload.get("criteria"), rubric.get("criteria", []))


def _validate_overall(overall: Any) -> None:
    if not isinstance(overall, dict):
        raise EvaluationValidationError("Missing 'overall' object in evaluation payload")

    for key in ("points_earned", "points_possible"):
        value = overall.get(key)
        if not isinstance(value, (int, float)):
            raise EvaluationValidationError(f"overall.{key} must be a number")


def _validate_criteria(criteria: Any, rubric_criteria: Iterable[Dict[str, Any]]) -> None:
    if not isinstance(criteria, list):
        raise EvaluationValidationError("'criteria' must be a list")

    required_ids = {str(item.get("id")) for item in rubric_criteria if item.get("id") is not None}
    seen_ids = set()

    for entry in criteria:
        if not isinstance(entry, dict):
            raise EvaluationValidationError("Each criterion entry must be an object")

        crit_id = str(entry.get("id", ""))
        if not crit_id:
            raise EvaluationValidationError("Each criterion entry must include an 'id'")

        seen_ids.add(crit_id)
        if not isinstance(entry.get("score"), (int, float)):
            raise EvaluationValidationError(f"Criterion {crit_id} has invalid score")

        evidence = entry.get("evidence")
        if not isinstance(evidence, dict) or "quote" not in evidence:
            raise EvaluationValidationError(f"Criterion {crit_id} must include evidence.quote")

        for field in ("explanation", "advice"):
            if not isinstance(entry.get(field), str):
                raise EvaluationValidationError(f"Criterion {crit_id} must include '{field}'")

    if required_ids and not required_ids.issubset(seen_ids):
        missing = ", ".join(sorted(required_ids - seen_ids))
        raise EvaluationValidationError(f"Evaluation missing required criteria: {missing}")
