"""Client wrapper for structured essay evaluation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from openai import OpenAI
from pydantic import ValidationError

from models import EvaluationModel, RubricModel

from . import prompts, validation


PROMPT_SCHEMA_SAMPLE = {
    "overall": {"points_earned": 0, "points_possible": 0},
    "criteria": [
        {
            "id": "STRING",
            "score": 0,
            "evidence": {"quote": "Selected lines from the essay"},
            "explanation": "≤25 words",
            "advice": "≤25 words",
        }
    ],
}

RUBRIC_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_points_possible": {"type": "number"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "max_score": {"type": "number"},
                    "descriptors": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["id", "max_score"],
                "additionalProperties": True,
            },
        },
    },
    "required": ["criteria"],
    "additionalProperties": True,
}


class AIClientError(Exception):
    """Raised when the AI client cannot produce a valid evaluation."""


@dataclass
class EvaluationResult:
    """Outcome of an evaluation request."""

    status: Literal["ok", "retry_ok", "schema_fail"]
    attempts: int
    evaluation: Optional[EvaluationModel]
    payload: Optional[Dict[str, Any]]
    raw_text: str
    usage: Optional[Dict[str, Any]]
    schema_errors: List[str]


@dataclass
class RubricExtractionResult:
    """Outcome of a rubric extraction request."""

    status: Literal["ok", "retry_ok", "error"]
    attempts: int
    payload: Optional[Dict[str, Any]]
    raw_text: str
    usage: Optional[Dict[str, Any]]
    errors: List[str]


def evaluate_essay(
    essay_text: str,
    rubric: RubricModel,
    *,
    validation_retry: int = 1,
    trim_text_fields: bool = True,
) -> EvaluationResult:
    """Invoke the AI model and enforce structured validation."""

    rubric_json = json.dumps(rubric.model_dump(mode="json"), ensure_ascii=False, indent=2)
    schema_json = json.dumps(PROMPT_SCHEMA_SAMPLE, ensure_ascii=False, indent=2)
    criterion_ids = ", ".join(sorted(rubric.id_set))

    try:
        system_prompt = prompts.load_prompt("system.md", {})
        user_prompt = prompts.load_prompt(
            "rubric_evaluator.md",
            {
                "rubric_json": rubric_json,
                "essay_text": essay_text,
                "schema_json": schema_json,
                "criterion_ids": criterion_ids,
            },
        )
    except (prompts.PromptNotFoundError, prompts.PromptRenderError) as exc:
        raise AIClientError(str(exc)) from exc

    response_schema = EvaluationModel.model_json_schema()

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    max_attempts = max(validation_retry, 0) + 1
    attempts = 0
    last_raw = ""
    usage: Optional[Dict[str, Any]] = None
    schema_errors: List[str] = []

    while attempts < max_attempts:
        attempts += 1
        try:
            completion = _run_completion(messages, response_schema)
        except Exception as exc:  # pragma: no cover - network errors
            raise AIClientError(str(exc)) from exc

        last_raw = completion["content"]
        usage = completion["usage"]

        try:
            payload = json.loads(last_raw)
        except json.JSONDecodeError as exc:
            schema_errors = [f"Response was not valid JSON: {exc.msg}"]
            if attempts >= max_attempts:
                break
            messages.append(_retry_message(schema_errors))
            continue

        try:
            evaluation = validation.validate_evaluation(payload, rubric)
        except ValidationError as exc:
            schema_errors = validation.format_validation_errors(exc)
            if attempts >= max_attempts:
                break
            messages.append(_retry_message(schema_errors))
            continue

        normalized = validation.normalize_evaluation(
            evaluation, trim_text_fields=trim_text_fields
        )
        status: Literal["ok", "retry_ok"] = "retry_ok" if attempts > 1 else "ok"
        return EvaluationResult(
            status=status,
            attempts=attempts,
            evaluation=evaluation,
            payload=normalized,
            raw_text=last_raw,
            usage=usage,
            schema_errors=[],
        )

    return EvaluationResult(
        status="schema_fail",
        attempts=attempts,
        evaluation=None,
        payload=None,
        raw_text=last_raw,
        usage=usage,
        schema_errors=schema_errors,
    )


def extract_rubric_json(
    rubric_text: str, *, retry_attempts: int = 1
) -> RubricExtractionResult:
    """Use the AI model to infer rubric JSON from raw text."""

    try:
        extractor_prompt = prompts.load_prompt(
            "rubric_extractor.md", {"rubric_text": rubric_text}
        )
    except (prompts.PromptNotFoundError, prompts.PromptRenderError) as exc:
        raise AIClientError(str(exc)) from exc

    messages: List[Dict[str, str]] = [{"role": "system", "content": extractor_prompt}]
    max_attempts = max(retry_attempts, 0) + 1
    attempts = 0
    last_raw = ""
    usage: Optional[Dict[str, Any]] = None
    errors: List[str] = []

    while attempts < max_attempts:
        attempts += 1
        try:
            completion = _run_completion(messages, RUBRIC_RESPONSE_SCHEMA)
        except Exception as exc:  # pragma: no cover - network errors
            raise AIClientError(str(exc)) from exc

        last_raw = completion["content"]
        usage = completion["usage"]

        try:
            payload = json.loads(last_raw)
        except json.JSONDecodeError as exc:
            errors = [f"Response was not valid JSON: {exc.msg}"]
            if attempts >= max_attempts:
                break
            messages.append(_rubric_retry_message(errors))
            continue

        if not isinstance(payload, dict):
            errors = ["Response must be a JSON object"]
            if attempts >= max_attempts:
                break
            messages.append(_rubric_retry_message(errors))
            continue

        status: Literal["ok", "retry_ok"] = "retry_ok" if attempts > 1 else "ok"
        return RubricExtractionResult(
            status=status,
            attempts=attempts,
            payload=payload,
            raw_text=last_raw,
            usage=usage,
            errors=[],
        )

    return RubricExtractionResult(
        status="error",
        attempts=attempts,
        payload=None,
        raw_text=last_raw,
        usage=usage,
        errors=errors,
    )


def _run_completion(messages: List[Dict[str, str]], schema: Dict[str, Any]) -> Dict[str, Any]:
    client = _get_client()
    completion = client.chat.completions.create(
        model=_get_model(),
        messages=messages,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "evaluation",
                "schema": schema,
            },
        },
        timeout=int(os.getenv("AI_TIMEOUT_SECONDS", "120")),
    )

    message = completion.choices[0].message
    content = message.content or ""
    usage: Optional[Dict[str, Any]] = None
    if completion.usage:
        usage = _usage_to_dict(completion.usage)
    return {"content": content, "usage": usage}


def _retry_message(errors: List[str]) -> Dict[str, str]:
    error_block = "\n".join(f"- {error}" for error in errors)
    try:
        prompt = prompts.load_prompt("retry_context.md", {"errors": error_block})
    except (prompts.PromptNotFoundError, prompts.PromptRenderError) as exc:
        raise AIClientError(str(exc)) from exc
    return {"role": "system", "content": prompt}


def _rubric_retry_message(errors: List[str]) -> Dict[str, str]:
    summary = "\n".join(f"- {error}" for error in errors)
    try:
        prompt = prompts.load_prompt("rubric_retry.md", {"error_summary": summary})
    except (prompts.PromptNotFoundError, prompts.PromptRenderError) as exc:
        raise AIClientError(str(exc)) from exc
    return {"role": "system", "content": prompt}


def _usage_to_dict(usage: Any) -> Dict[str, Any]:
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if hasattr(usage, "to_dict"):
        return usage.to_dict()
    return dict(usage)  # type: ignore[arg-type]


def _get_env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _build_client() -> OpenAI:
    api_key = _get_env("AI_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY")
    if not api_key:
        raise AIClientError(
            "Set AI_API_KEY (or XAI_API_KEY / OPENAI_API_KEY) in the environment"
        )

    base_url = _get_env("AI_PROVIDER_URL", "XAI_API_BASE", "OPENAI_API_BASE")
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url)
    return OpenAI(api_key=api_key)


_CLIENT: OpenAI | None = None


def _get_client() -> OpenAI:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _build_client()
    return _CLIENT


def _get_model() -> str:
    model = _get_env("AI_MODEL", "XAI_MODEL", "OPENAI_MODEL", default="gpt-4-turbo")
    if not model:
        raise AIClientError(
            "Set AI_MODEL (or XAI_MODEL / OPENAI_MODEL) in the environment"
        )
    return model
