"""PDF rendering utilities for printable summaries."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer


_PAGE_SIZES = {
    "letter": LETTER,
    "us_letter": LETTER,
    "a4": A4,
}


@dataclass(slots=True)
class PDFSettings:
    """Runtime configuration for PDF exports."""

    page_size: str = "letter"
    font: str = "Helvetica"
    line_spacing: float = 1.2
    course_name: str = ""
    teacher_name: str = ""


class PDFRenderError(RuntimeError):
    """Raised when a PDF could not be generated."""


class PDFSummaryRenderer:
    """Render PDF summaries using ReportLab."""

    def __init__(self, settings: PDFSettings) -> None:
        self.settings = settings
        self._page_size = self._resolve_page_size(settings.page_size)
        sample_styles = getSampleStyleSheet()
        base = sample_styles["Normal"]
        self._body_style = ParagraphStyle(
            "SummaryBody",
            parent=base,
            fontName=settings.font,
            fontSize=12,
            leading=max(12, 12 * settings.line_spacing),
            spaceAfter=6,
        )
        self._header_style = ParagraphStyle(
            "SummaryHeader",
            parent=self._body_style,
            fontSize=16,
            leading=max(16, 16 * settings.line_spacing),
            spaceAfter=12,
        )
        self._subheader_style = ParagraphStyle(
            "SummarySubheader",
            parent=self._body_style,
            fontSize=12,
            spaceBefore=6,
            spaceAfter=4,
        )

    def generate_student_pdf(self, context: Mapping[str, object], target: Path) -> int:
        """Generate a PDF summary for a single student.

        Returns the number of bytes written.
        """

        target.parent.mkdir(parents=True, exist_ok=True)
        document = SimpleDocTemplate(
            str(target),
            pagesize=self._page_size,
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
            topMargin=1.0 * inch,
            bottomMargin=1.0 * inch,
        )
        story = self._build_story(context)
        try:
            document.build(story)
        except Exception as exc:  # pragma: no cover - ReportLab runtime issues
            raise PDFRenderError(str(exc)) from exc
        return target.stat().st_size if target.exists() else 0

    def generate_batch_pdf(
        self,
        contexts: Iterable[Mapping[str, object]],
        target: Path,
    ) -> int:
        """Generate a combined PDF for multiple students."""

        target.parent.mkdir(parents=True, exist_ok=True)
        document = SimpleDocTemplate(
            str(target),
            pagesize=self._page_size,
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
            topMargin=1.0 * inch,
            bottomMargin=1.0 * inch,
        )
        story: List[object] = []
        contexts_list = list(contexts)
        for index, context in enumerate(contexts_list):
            story.extend(self._build_story(context))
            if index < len(contexts_list) - 1:
                story.append(PageBreak())
        if not story:
            return 0
        try:
            document.build(story)
        except Exception as exc:  # pragma: no cover - ReportLab runtime issues
            raise PDFRenderError(str(exc)) from exc
        return target.stat().st_size if target.exists() else 0

    def _build_story(self, context: Mapping[str, object]) -> List[object]:
        student_name = str(context.get("student_name", ""))
        job_name = str(context.get("job_name", ""))
        generated_at = str(context.get("generated_at", ""))
        overall = context.get("eval", {})
        overall_points = ""
        if isinstance(overall, Mapping):
            overall_section = overall.get("overall")
            if isinstance(overall_section, Mapping):
                earned = overall_section.get("points_earned")
                possible = overall_section.get("points_possible")
                if earned is not None and possible is not None:
                    overall_points = f"{earned} / {possible}"
        criteria_rows = context.get("criteria_rows", [])
        flags = context.get("flags", {})
        story: List[object] = []

        story.append(Paragraph(html.escape(student_name), self._header_style))
        info_lines: List[str] = []
        if job_name:
            info_lines.append(f"Job: {html.escape(job_name)}")
        if generated_at:
            info_lines.append(f"Generated: {html.escape(generated_at)}")
        if self.settings.course_name or self.settings.teacher_name:
            footer_bits = [bit for bit in [self.settings.course_name, self.settings.teacher_name] if bit]
            if footer_bits:
                info_lines.append(" — ".join(html.escape(bit) for bit in footer_bits))
        for line in info_lines:
            story.append(Paragraph(line, self._body_style))
        story.append(Spacer(1, 0.2 * inch))

        story.append(Paragraph("Overall Score", self._subheader_style))
        score_display = html.escape(overall_points) if overall_points else "—"
        story.append(Paragraph(score_display, self._body_style))
        story.append(Spacer(1, 0.15 * inch))

        if isinstance(criteria_rows, Sequence) and criteria_rows:
            story.append(Paragraph("Criteria", self._subheader_style))
            story.append(Spacer(1, 0.05 * inch))
            for entry in criteria_rows:
                if not isinstance(entry, Mapping):
                    continue
                criterion_id = html.escape(str(entry.get("id", "")))
                name = html.escape(str(entry.get("name", "")))
                score = html.escape(str(entry.get("score", "")))
                header_bits = [bit for bit in [criterion_id, name] if bit]
                header_text = " — ".join(header_bits)
                if score:
                    header_text = f"{header_text} (Score: {score})" if header_text else f"Score: {score}"
                story.append(Paragraph(f"<b>{header_text}</b>", self._body_style))

                for label, key in (
                    ("Evidence", "evidence"),
                    ("Explanation", "explanation"),
                    ("Advice", "advice"),
                ):
                    value = entry.get(key)
                    if not value:
                        continue
                    text = html.escape(str(value)).replace("\n", "<br/>")
                    story.append(
                        Paragraph(
                            f"<b>{label}:</b> {text}",
                            self._body_style,
                        )
                    )
                story.append(Spacer(1, 0.12 * inch))
            story.append(Spacer(1, 0.1 * inch))

        if isinstance(flags, Mapping) and any(flags.values()):
            story.append(Paragraph("Notes", self._subheader_style))
            bullet_style = ParagraphStyle(
                "SummaryBullet",
                parent=self._body_style,
                leftIndent=12,
                bulletIndent=6,
            )
            for key, value in sorted(flags.items()):
                if not value:
                    continue
                text = "Essay text contained limited content; evaluation may be partial." if key == "low_text_warning" else key.replace("_", " ").title()
                story.append(Paragraph(f"• {html.escape(text)}", bullet_style))
            story.append(Spacer(1, 0.1 * inch))

        footer_parts = [self.settings.course_name, self.settings.teacher_name]
        footer_parts = [part for part in footer_parts if part]
        if footer_parts:
            story.append(Spacer(1, 0.2 * inch))
            story.append(Paragraph(" — ".join(html.escape(part) for part in footer_parts), self._body_style))

        return story

    @staticmethod
    def _resolve_page_size(label: str) -> Sequence[float]:
        key = (label or "").strip().lower()
        return _PAGE_SIZES.get(key, LETTER)
