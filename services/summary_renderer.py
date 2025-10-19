"""Utilities for rendering per-student printable summaries."""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from jinja2 import Environment, FileSystemLoader, TemplateError

from models import RubricModel

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")


def _ljust(value: Any, width: int) -> str:
    try:
        width = int(width)
    except (TypeError, ValueError):
        width = 0
    return str(value).ljust(max(width, 0))


def _sanitize_text(value: str) -> str:
    """Remove non-printable control characters."""

    return _CONTROL_CHARS.sub("", value)


def _limit_words(text: str, limit: int) -> str:
    words = [word for word in text.split() if word]
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit])


def _ensure_lines(lines: List[str]) -> List[str]:
    return lines if lines else [""]


@dataclass(slots=True)
class SummarySettings:
    """Configuration for printable summary generation."""

    enabled: bool = True
    markdown_enabled: bool = False
    line_width: int = 100
    include_zip_readme: bool = False
    readme_template: str = "batch_header.txt.j2"
    template_dir: Path = Path("templates")
    text_template: str = "student_summary.txt.j2"
    markdown_template: str = "student_summary.md.j2"
    course_name: str = ""
    teacher_name: str = ""


@dataclass(slots=True)
class SummaryRenderResult:
    """Rendered payload for a single student."""

    text: Optional[str]
    markdown: Optional[str]

    @property
    def produced_any(self) -> bool:
        return bool((self.text or "").strip()) or bool((self.markdown or "").strip())


class SummaryRenderError(RuntimeError):
    """Raised when summary rendering fails."""


class SummaryRenderer:
    """Render plain-text and markdown summaries from validated evaluations."""

    def __init__(self, rubric: RubricModel, settings: SummarySettings) -> None:
        self.settings = settings
        self._rubric = rubric.model_dump(mode="json")
        loader = FileSystemLoader(str(settings.template_dir))
        self._env = Environment(
            loader=loader,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self._env.filters.setdefault("wrap_text", self._wrap_text)
        self._env.filters.setdefault("wrap_lines", self._wrap_lines)
        self._env.filters.setdefault("sanitize", _sanitize_text)
        self._env.filters.setdefault("ljust", _ljust)
        self._env.globals.setdefault("SUMMARY_LINE_WIDTH", settings.line_width)
        self._env.globals.setdefault("wrap_text", self._wrap_text)
        self._env.globals.setdefault("wrap_lines", self._wrap_lines)

    def render_student(
        self,
        student_name: str,
        evaluation: Dict[str, Any],
        *,
        job_name: str,
        generated_at: datetime,
        flags: Optional[Dict[str, Any]] = None,
    ) -> SummaryRenderResult:
        """Render printable summaries for a validated evaluation."""

        context = self._build_context(
            student_name=student_name,
            evaluation=evaluation,
            job_name=job_name,
            generated_at=generated_at,
            flags=flags or {},
        )

        text_content: Optional[str] = None
        markdown_content: Optional[str] = None

        if self.settings.enabled:
            text_content = self._render_template(self.settings.text_template, context)
        if self.settings.markdown_enabled:
            markdown_content = self._render_template(self.settings.markdown_template, context)

        return SummaryRenderResult(text=text_content, markdown=markdown_content)

    def render_batch_header(
        self,
        *,
        job_name: str,
        generated_at: datetime,
        students: Iterable[str],
    ) -> Optional[str]:
        """Render the optional README entry for the zip archive."""

        if not self.settings.include_zip_readme:
            return None

        template_name = self.settings.readme_template
        if not template_name:
            return None

        student_list = list(students)
        context = {
            "job_name": job_name,
            "generated_at": self._format_timestamp(generated_at),
            "student_count": len(student_list),
            "students": student_list,
            "course_name": self.settings.course_name,
            "teacher_name": self.settings.teacher_name,
        }
        return self._render_template(template_name, context)

    def _render_template(self, template_name: str, context: Dict[str, Any]) -> str:
        try:
            template = self._env.get_template(template_name)
        except TemplateError as exc:  # pragma: no cover - configuration issue
            raise SummaryRenderError(str(exc)) from exc

        try:
            rendered = template.render(context)
        except Exception as exc:  # pragma: no cover - template runtime errors
            raise SummaryRenderError(str(exc)) from exc

        return rendered.rstrip("\n") + "\n"

    def _build_context(
        self,
        *,
        student_name: str,
        evaluation: Dict[str, Any],
        job_name: str,
        generated_at: datetime,
        flags: Dict[str, Any],
    ) -> Dict[str, Any]:
        sanitized_eval = self._sanitize_payload(evaluation)
        sanitized_flags = self._sanitize_payload(flags)
        criteria_rows = self._prepare_rows(sanitized_eval)

        context: Dict[str, Any] = {
            "student_name": student_name,
            "job_name": job_name,
            "generated_at": self._format_timestamp(generated_at),
            "eval": sanitized_eval,
            "rubric": self._rubric,
            "flags": sanitized_flags,
            "criteria_rows": criteria_rows,
            "SUMMARY_LINE_WIDTH": self.settings.line_width,
            "line_width": self.settings.line_width,
            "COURSE_NAME": self.settings.course_name,
            "TEACHER_NAME": self.settings.teacher_name,
            "course_name": self.settings.course_name,
            "teacher_name": self.settings.teacher_name,
        }
        return context

    def _sanitize_payload(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            return {key: self._sanitize_payload(value) for key, value in payload.items()}
        if isinstance(payload, list):
            return [self._sanitize_payload(item) for item in payload]
        if isinstance(payload, str):
            return _sanitize_text(payload)
        return payload

    def _prepare_rows(self, evaluation: Dict[str, Any]) -> List[Dict[str, Any]]:
        criteria = evaluation.get("criteria", []) if isinstance(evaluation, dict) else []
        rows: List[Dict[str, Any]] = []
        for entry in criteria:
            if not isinstance(entry, dict):
                continue
            evidence = ""
            evidence_obj = entry.get("evidence")
            if isinstance(evidence_obj, dict):
                evidence = str(evidence_obj.get("quote", ""))
            explanation = _limit_words(str(entry.get("explanation", "")), 25)
            advice = _limit_words(str(entry.get("advice", "")), 25)

            evidence_lines = [line.strip() for line in evidence.splitlines() if line.strip()]
            if len(evidence_lines) > 3:
                evidence_lines = evidence_lines[:3]
            evidence = "\n".join(evidence_lines)

            row = {
                "id": str(entry.get("id", "")),
                "score": entry.get("score", ""),
                "evidence": evidence,
                "explanation": explanation,
                "advice": advice,
            }
            rows.append(row)
        return rows

    def _wrap_text(self, text: str, width: Optional[int] = None) -> str:
        width = width or self.settings.line_width
        lines = self._wrap_lines(text, width=width)
        return "\n".join(lines)

    def _wrap_lines(
        self,
        text: str,
        *,
        width: Optional[int] = None,
        max_lines: Optional[int] = None,
    ) -> List[str]:
        width = width or self.settings.line_width
        sanitized = _sanitize_text(text)
        segments = [segment.strip() for segment in sanitized.splitlines()]
        segments = [segment for segment in segments if segment]
        if not segments:
            segments = [sanitized.strip()]
        wrapped: List[str] = []
        for segment in segments:
            if not segment:
                continue
            parts = textwrap.wrap(
                segment,
                width=width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            if not parts:
                parts = [segment]
            wrapped.extend(parts)
            if max_lines is not None and len(wrapped) >= max_lines:
                break

        if max_lines is not None and len(wrapped) > max_lines:
            wrapped = wrapped[:max_lines]
        return _ensure_lines(wrapped)

    @staticmethod
    def _format_timestamp(value: datetime) -> str:
        return value.replace(microsecond=0).isoformat() + "Z"
