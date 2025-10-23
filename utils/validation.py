"""Structured validation helpers built on Pydantic models."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import ValidationError

from models import EvaluationModel, RubricModel

COMMENT_WORD_LIMIT = 30
EXCERPT_LINE_LIMIT = 3
SUGGESTION_WORD_LIMIT = 120
SUMMARY_WORD_LIMIT = 80


def parse_rubric(payload: Any) -> RubricModel:
    """Return a validated rubric model."""

    return RubricModel.model_validate(payload)


def validate_evaluation(payload: Dict[str, Any], rubric: RubricModel) -> EvaluationModel:
    """Validate an evaluation payload against the rubric-derived context."""

    context = rubric.validation_context()
    return EvaluationModel.model_validate(payload, context=context)


def format_validation_errors(error: ValidationError) -> List[str]:
    """Convert a Pydantic ValidationError into concise bullet strings."""

    messages: List[str] = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"])
        if location:
            messages.append(f"{location}: {issue['msg']}")
        else:
            messages.append(issue["msg"])
    return messages


def normalize_evaluation(
    evaluation: EvaluationModel,
    *,
    trim_text_fields: bool = True,
) -> Dict[str, Any]:
    """Return a JSON-serialisable dict, optionally trimming textual fields."""

    data = evaluation.model_dump(mode="python")
    if not trim_text_fields:
        return data

    summary = data.get("summary")
    if isinstance(summary, str):
        data["summary"] = _trim_words(summary, SUMMARY_WORD_LIMIT)

    for criterion in data.get("criteria", []):
        examples = criterion.get("examples", [])
        if isinstance(examples, list):
            for example in examples:
                if isinstance(example, dict):
                    excerpt = example.get("excerpt")
                    comment = example.get("comment")
                    if isinstance(excerpt, str):
                        example["excerpt"] = _trim_lines(excerpt, EXCERPT_LINE_LIMIT)
                    if isinstance(comment, str):
                        example["comment"] = _trim_words(comment, COMMENT_WORD_LIMIT)
        suggestion = criterion.get("improvement_suggestion")
        if isinstance(suggestion, str):
            criterion["improvement_suggestion"] = _trim_words(
                suggestion, SUGGESTION_WORD_LIMIT
            )
    return data


def _trim_lines(text: str, limit: int) -> str:
    lines = [line.strip() for line in text.splitlines()]
    trimmed = [line for line in lines if line][:limit]
    return "\n".join(trimmed)


def _trim_words(text: str, limit: int) -> str:
    words: List[str] = [word for word in text.split() if word]
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit])
