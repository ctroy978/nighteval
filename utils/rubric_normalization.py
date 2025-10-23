"""Helpers for normalizing rubric payloads into the canonical schema."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from models import RubricModel


_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")


@dataclass
class CanonicalizationConfig:
    """Runtime settings that influence rubric canonicalisation."""

    id_max_length: int = 40
    require_totals_equal: bool = True


@dataclass
class CanonicalizationResult:
    """Outcome of transforming an arbitrary payload into canonical form."""

    canonical: Optional[Dict[str, Any]]
    normalized: Optional[Dict[str, Any]]
    errors: List[Dict[str, str]]
    error_messages: List[str]
    warnings: List[str]
    converted: bool

    @property
    def is_valid(self) -> bool:
        return self.canonical is not None and not self.errors


def canonicalize_rubric(
    payload: Any, *, config: CanonicalizationConfig
) -> CanonicalizationResult:
    """Return a canonical rubric payload or a list of validation issues."""

    normalized = _ensure_dict(payload)
    errors: List[Dict[str, str]] = []
    error_messages: List[str] = []
    warnings: List[str] = []

    if normalized is None:
        message = "Rubric must be a JSON object"
        return CanonicalizationResult(
            canonical=None,
            normalized=None,
            errors=[{"loc": "__root__", "msg": message}],
            error_messages=[message],
            warnings=[],
            converted=False,
        )

    initial_attempt = copy.deepcopy(normalized)
    validation = _validate_canonical(initial_attempt, config)
    if validation.is_valid:
        return validation

    converted_payload, converted, convert_warnings = _auto_convert(normalized, config)
    warnings.extend(convert_warnings)
    if converted:
        validation = _validate_canonical(converted_payload, config)
        validation.converted = True
        validation.warnings.extend(warnings)
        return validation

    validation.warnings.extend(warnings)
    return validation


def _validate_canonical(
    payload: Dict[str, Any], config: CanonicalizationConfig
) -> CanonicalizationResult:
    """Validate payload against RubricModel and canonical rules."""

    errors: List[Dict[str, str]] = []
    error_messages: List[str] = []
    warnings: List[str] = []

    try:
        model = RubricModel.model_validate(payload)
    except ValidationError as exc:
        errors = _format_validation_errors(exc)
        error_messages = [f"{item['loc']}: {item['msg']}" for item in errors]
        normalized = _ensure_dict(payload)
        return CanonicalizationResult(
            canonical=None,
            normalized=normalized,
            errors=errors,
            error_messages=error_messages,
            warnings=warnings,
            converted=False,
        )

    canonical = model.model_dump(mode="json")

    numeric_total = model.points_possible
    if canonical.get("overall_points_possible") is None and numeric_total is not None:
        canonical["overall_points_possible"] = numeric_total

    sum_scores = _safe_sum(canonical.get("criteria", []))
    overall_raw = canonical.get("overall_points_possible")
    overall_numeric = (
        float(overall_raw) if isinstance(overall_raw, (int, float)) else None
    )

    if overall_numeric is None and sum_scores is not None:
        canonical["overall_points_possible"] = sum_scores
        overall_numeric = sum_scores

    if (
        config.require_totals_equal
        and sum_scores is not None
        and overall_numeric is not None
        and abs(overall_numeric - sum_scores) > 1e-6
    ):
        message = (
            "Sum of criterion max_score values must equal overall_points_possible"
        )
        errors.append({"loc": "overall_points_possible", "msg": message})
        error_messages.append(message)

    id_errors = _validate_ids(canonical.get("criteria", []), config)
    if id_errors:
        errors.extend(id_errors)
        error_messages.extend(f"{item['loc']}: {item['msg']}" for item in id_errors)

    return CanonicalizationResult(
        canonical=canonical if not errors else None,
        normalized=canonical,
        errors=errors,
        error_messages=error_messages,
        warnings=warnings,
        converted=False,
    )


def _validate_ids(
    criteria: List[Dict[str, Any]], config: CanonicalizationConfig
) -> List[Dict[str, str]]:
    """Ensure criterion IDs follow snake_case and length rules."""

    issues: List[Dict[str, str]] = []
    seen: Dict[str, int] = {}
    for index, criterion in enumerate(criteria):
        identifier = str(criterion.get("id", "")).strip()
        loc = f"criteria[{index}].id"
        if not identifier:
            issues.append({"loc": loc, "msg": "Criterion id must not be blank"})
            continue
        seen[identifier] = seen.get(identifier, 0) + 1
        if not _ID_PATTERN.match(identifier):
            issues.append({
                "loc": loc,
                "msg": "Criterion id must be snake_case (a-z, 0-9, underscore)",
            })
        if len(identifier) > config.id_max_length:
            issues.append({
                "loc": loc,
                "msg": f"Criterion id must be ≤{config.id_max_length} characters",
            })
    for identifier, count in seen.items():
        if count > 1:
            issues.append(
                {
                    "loc": "criteria[].id",
                    "msg": f"Duplicate id detected: {identifier}",
                }
            )
    return issues


def _format_validation_errors(error: ValidationError) -> List[Dict[str, str]]:
    formatted: List[Dict[str, str]] = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"])
        formatted.append({"loc": location or "__root__", "msg": issue["msg"]})
    return formatted


def _ensure_dict(payload: Any) -> Optional[Dict[str, Any]]:
    if isinstance(payload, dict):
        return payload
    return None


def _auto_convert(
    payload: Dict[str, Any], config: CanonicalizationConfig
) -> Tuple[Dict[str, Any], bool, List[str]]:
    """Apply heuristic conversions for legacy rubric shapes."""

    working = copy.deepcopy(payload)
    changed = False
    warnings: List[str] = []

    if "rubric" in working and isinstance(working["rubric"], dict):
        working = copy.deepcopy(working["rubric"])
        changed = True

    if "total_points" in working and "overall_points_possible" not in working:
        working["overall_points_possible"] = working["total_points"]
        changed = True

    criteria = working.get("criteria")
    if not isinstance(criteria, list):
        return working, changed, warnings

    existing_ids: Dict[str, int] = {}
    for index, item in enumerate(criteria):
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        name = item.get("name") or identifier or f"criterion_{index+1}"
        generated_id = _slugify(name, config.id_max_length)
        if not identifier or not isinstance(identifier, str):
            item["id"] = _dedupe_id(generated_id, existing_ids)
            changed = True
        else:
            slug = _slugify(identifier, config.id_max_length)
            slug = _dedupe_id(slug, existing_ids)
            if slug != identifier:
                item["id"] = slug
                changed = True
        existing_ids[item["id"]] = existing_ids.get(item["id"], 0) + 1

        if "max_score" not in item or not isinstance(item.get("max_score"), int):
            levels = item.get("levels")
            max_score = _extract_max_score(levels)
            if max_score is not None:
                item["max_score"] = max_score
                changed = True

        if "descriptors" not in item:
            levels = item.get("levels")
            descriptors = _levels_to_descriptors(levels)
            if descriptors:
                item["descriptors"] = descriptors
                changed = True

    sum_scores = _safe_sum(criteria)
    overall = working.get("overall_points_possible") if isinstance(working.get("overall_points_possible"), (int, float)) else None
    if sum_scores is not None:
        if overall is None:
            working["overall_points_possible"] = sum_scores
            changed = True
        elif abs(overall - sum_scores) > 1e-6 and not config.require_totals_equal:
            working["overall_points_possible"] = sum_scores
            changed = True
            warnings.append(
                "Adjusted overall_points_possible to match the sum of criterion max_score values"
            )

    if "levels" in working:
        working.pop("levels")
        changed = True

    if "criteria" in working:
        for item in working["criteria"]:
            if isinstance(item, dict) and "levels" in item:
                item.pop("levels")
                changed = True

    return working, changed, warnings


def _slugify(value: str, max_length: int) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "criterion"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


def _dedupe_id(identifier: str, taken: Dict[str, int]) -> str:
    if identifier not in taken:
        return identifier
    counter = taken[identifier]
    while True:
        candidate = f"{identifier}_{counter + 1}"
        if candidate not in taken:
            taken[identifier] = counter + 1
            return candidate
        counter += 1


def _extract_max_score(levels: Any) -> Optional[int]:
    if isinstance(levels, list):
        scores = [entry.get("score") for entry in levels if isinstance(entry, dict)]
        numeric = [int(score) for score in scores if isinstance(score, (int, float))]
        if numeric:
            return int(max(numeric))
    return None


def _levels_to_descriptors(levels: Any) -> Dict[str, str]:
    descriptors: Dict[str, str] = {}
    if not isinstance(levels, list):
        return descriptors
    for entry in levels:
        if not isinstance(entry, dict):
            continue
        score = entry.get("score")
        description = entry.get("description") or entry.get("text")
        if isinstance(score, (int, float)) and isinstance(description, str) and description.strip():
            descriptors[str(int(score))] = description.strip()
    return descriptors


def _safe_sum(criteria: List[Any]) -> Optional[float]:
    numeric_values: List[float] = []
    for item in criteria:
        if not isinstance(item, dict):
            return None
        value = item.get("max_score")
        if value is None:
            return None
        if isinstance(value, (int, float)):
            numeric_values.append(float(value))
        else:
            return None
    if not numeric_values:
        return None
    return float(sum(numeric_values))
