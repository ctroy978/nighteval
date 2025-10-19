"""Helpers for loading and rendering prompt templates."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from jinja2 import Template, StrictUndefined


class PromptNotFoundError(FileNotFoundError):
    """Raised when a prompt template cannot be located."""


class PromptRenderError(RuntimeError):
    """Raised when a prompt template cannot be rendered."""


def _prompts_base_dir() -> Path:
    env_dir = os.getenv("PROMPTS_DIR")
    if env_dir:
        return Path(env_dir)
    return Path(__file__).resolve().parent.parent / "prompts"


def _resolve_prompt_path(name: str) -> Path:
    candidate = Path(name)
    if candidate.is_absolute() and candidate.exists():
        return candidate

    base = _prompts_base_dir()
    path = base / name
    if path.exists():
        return path

    if not name.endswith(".md"):
        path_with_ext = base / f"{name}.md"
        if path_with_ext.exists():
            return path_with_ext

    raise PromptNotFoundError(f"Prompt template not found: {name}")


def load_prompt(name: str, context: Dict[str, Any] | None = None) -> str:
    """Load a prompt template and render it with the given context."""

    path = _resolve_prompt_path(name)
    source = path.read_text(encoding="utf-8")
    context = context or {}

    try:
        template = Template(source, undefined=StrictUndefined)
        return template.render(**context)
    except Exception as exc:  # pragma: no cover - template syntax errors
        raise PromptRenderError(f"Failed to render prompt '{name}': {exc}") from exc

