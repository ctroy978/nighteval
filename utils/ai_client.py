"""Client wrapper for OpenAI-compatible essay evaluation."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from openai import OpenAI


SYSTEM_PROMPT = (
    "You are an essay evaluator that grades only by the provided rubric. "
    "Return valid JSON only in the required structure."
)


class AIClientError(Exception):
    """Raised when the AI client cannot produce a valid evaluation."""


def _get_env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _build_client() -> OpenAI:
    api_key = _get_env("XAI_API_KEY", "OPENAI_API_KEY")
    if not api_key:
        raise AIClientError("Set XAI_API_KEY (or OPENAI_API_KEY) in the environment")

    base_url = _get_env("XAI_API_BASE", "OPENAI_API_BASE")
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
    model = _get_env("XAI_MODEL", "OPENAI_MODEL", default="grok-4-fast-reasoning")
    if not model:
        raise AIClientError("Set XAI_MODEL (or OPENAI_MODEL) in the environment")
    return model


def evaluate_essay(essay_text: str, rubric: Dict[str, Any]) -> Dict[str, Any]:
    """Send rubric and essay text to the model and return structured feedback."""

    user_content = _format_user_prompt(essay_text, rubric)
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    response = _request_with_retry(messages)
    return response


def _format_user_prompt(essay_text: str, rubric: Dict[str, Any]) -> str:
    rubric_json = json.dumps(rubric, ensure_ascii=False, indent=2)
    return (
        "Rubric:\n"
        f"{rubric_json}\n\n"
        "Essay:\n"
        f"{essay_text}\n\n"
        "Follow these rules:\n"
        "For each rubric criterion:\n"
        "1) Assign a numeric score based on the rubric descriptors.\n"
        "2) Quote one short passage (1–3 lines) from the essay as evidence.\n"
        "3) Give a short (≤25 words) explanation.\n"
        "4) Give a short (≤25 words) improvement suggestion limited to that criterion.\n\n"
        "Do not mention anything outside the rubric.\n"
        "Return JSON only, using this structure:\n"
        "{\n"
        "  \"overall\": { \"points_earned\": 0, \"points_possible\": 0 },\n"
        "  \"criteria\": [\n"
        "    {\n"
        "      \"id\": \"A\",\n"
        "      \"score\": 3,\n"
        "      \"evidence\": { \"quote\": \"Exact or paraphrased line(s)\" },\n"
        "      \"explanation\": \"≤25 words\",\n"
        "      \"advice\": \"≤25 words\"\n"
        "    }\n"
        "  ]\n"
        "}\n"
    )


def _request_with_retry(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    attempt = 0
    last_error: Exception | None = None

    while attempt < 2:
        try:
            client = _get_client()
            completion = client.chat.completions.create(
                model=_get_model(),
                messages=messages,
                response_format={"type": "json_object"},
                timeout=int(os.getenv("AI_TIMEOUT_SECONDS", "60")),
            )
            content = completion.choices[0].message.content or ""
            return json.loads(content)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            attempt += 1
            messages = messages + [
                {
                    "role": "system",
                    "content": "Your previous response was not valid JSON. Return only valid JSON conforming to the requested schema.",
                }
            ]
        except Exception as exc:  # pragma: no cover - network errors
            raise AIClientError(str(exc)) from exc

    raise AIClientError("Model response was not valid JSON") from last_error
