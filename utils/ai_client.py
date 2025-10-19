"""Client wrapper for OpenAI-compatible essay evaluation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from openai import OpenAI

from . import prompts


EVALUATION_SCHEMA = {
    "overall": {"points_earned": 0, "points_possible": 0},
    "criteria": [
        {
            "id": "A",
            "score": 3,
            "evidence": {"quote": "Exact or paraphrased line(s)"},
            "explanation": "≤25 words",
            "advice": "≤25 words",
        }
    ],
}


class AIClientError(Exception):
    """Raised when the AI client cannot produce a valid evaluation."""


@dataclass
class EvaluationResponse:
    """Structured response from the AI client."""

    content: Dict[str, Any]
    attempts: int
    raw_text: str
    usage: Dict[str, Any] | None


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


def evaluate_essay(essay_text: str, rubric: Dict[str, Any]) -> EvaluationResponse:
    """Send rubric and essay text to the model and return structured feedback."""

    rubric_json = json.dumps(rubric, ensure_ascii=False, indent=2)
    schema_json = json.dumps(EVALUATION_SCHEMA, ensure_ascii=False, indent=2)

    try:
        system_prompt = prompts.load_prompt("system.md", {})
        user_prompt = prompts.load_prompt(
            "rubric_evaluator.md",
            {
                "rubric_json": rubric_json,
                "essay_text": essay_text,
                "schema_json": schema_json,
            },
        )
    except (prompts.PromptNotFoundError, prompts.PromptRenderError) as exc:
        raise AIClientError(str(exc)) from exc

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    return _request_with_retry(messages)


def _request_with_retry(messages: List[Dict[str, str]]) -> EvaluationResponse:
    attempt = 0
    last_error: Exception | None = None
    raw_text = ""
    usage: Dict[str, Any] | None = None
    try:
        retry_prompt = prompts.load_prompt("retry_context.md", {})
    except (prompts.PromptNotFoundError, prompts.PromptRenderError) as exc:
        raise AIClientError(str(exc)) from exc

    while attempt < 2:
        try:
            client = _get_client()
            completion = client.chat.completions.create(
                model=_get_model(),
                messages=messages,
                response_format={"type": "json_object"},
                timeout=int(os.getenv("AI_TIMEOUT_SECONDS", "60")),
            )
            raw_text = completion.choices[0].message.content or ""
            if completion.usage:
                if hasattr(completion.usage, "model_dump"):
                    usage = completion.usage.model_dump()
                elif hasattr(completion.usage, "to_dict"):
                    usage = completion.usage.to_dict()
                else:  # pragma: no cover - defensive fallback
                    usage = dict(completion.usage)  # type: ignore[arg-type]
            content = json.loads(raw_text)
            return EvaluationResponse(
                content=content,
                attempts=attempt + 1,
                raw_text=raw_text,
                usage=usage,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            attempt += 1
            messages = messages + [
                {
                    "role": "system",
                    "content": retry_prompt,
                }
            ]
        except Exception as exc:  # pragma: no cover - network errors
            raise AIClientError(str(exc)) from exc

    raise AIClientError("Model response was not valid JSON") from last_error
