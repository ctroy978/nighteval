"""Batch job runner for Phase 1 processing."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import ValidationError

from models import RubricModel

from utils import ai_client, io_utils, pdf_tools, validation

from .pdf_renderer import PDFRenderError, PDFSettings, PDFSummaryRenderer
from .summary_renderer import SummaryRenderError, SummaryRenderer, SummarySettings

try:  # Optional dependency loaded lazily to keep startup cheap.
    import yaml
except ImportError:  # pragma: no cover - fallback when PyYAML is absent.
    yaml = None


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass
class TextValidationConfig:
    """Runtime settings for the PDF text gate."""

    enabled: bool = True
    min_text_chars: int = 500
    min_chars_per_page: int = 200
    allow_partial_text: bool = False

    @property
    def thresholds(self) -> Dict[str, int]:
        return {
            "min_text_chars": self.min_text_chars,
            "min_chars_per_page": self.min_chars_per_page,
        }


def _load_text_validation_config() -> TextValidationConfig:
    """Load text validation settings from ENV and optional YAML."""

    config = TextValidationConfig(
        enabled=_bool_env("TEXT_VALIDATION_ENABLED", True),
        min_text_chars=_int_env("MIN_TEXT_CHARS", 500),
        min_chars_per_page=_int_env("MIN_CHARS_PER_PAGE", 200),
        allow_partial_text=_bool_env("ALLOW_PARTIAL_TEXT", False),
    )

    yaml_paths: List[Path] = []
    explicit_path = os.getenv("TEXT_VALIDATION_CONFIG")
    if explicit_path:
        yaml_paths.append(Path(explicit_path).expanduser())
    yaml_paths.append(Path("config/text_validation.yaml"))
    yaml_paths.append(Path("config.yaml"))

    for candidate in yaml_paths:
        if not candidate or not candidate.exists():
            continue
        if yaml is None:
            break
        try:
            with candidate.open("r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle) or {}
        except Exception:  # pragma: no cover - malformed YAML edge cases
            continue

        section = payload.get("text_validation") if isinstance(payload, dict) else None
        if not isinstance(section, dict):
            continue

        config.enabled = _coalesce_bool(section.get("enabled"), config.enabled)
        config.min_text_chars = _coalesce_int(
            section.get("min_text_chars"), config.min_text_chars
        )
        config.min_chars_per_page = _coalesce_int(
            section.get("min_chars_per_page"), config.min_chars_per_page
        )
        config.allow_partial_text = _coalesce_bool(
            section.get("allow_partial_text"), config.allow_partial_text
        )
        break

    return config


def _load_summary_settings() -> SummarySettings:
    """Load printable summary settings from ENV and optional YAML files."""

    settings = SummarySettings(
        enabled=_bool_env("PRINT_SUMMARY_ENABLED", True),
        markdown_enabled=_bool_env("MARKDOWN_SUMMARY", False),
        line_width=max(_int_env("SUMMARY_LINE_WIDTH", 100), 40),
        include_zip_readme=_bool_env("INCLUDE_ZIP_README", False),
        readme_template=os.getenv("ZIP_README_TEMPLATE", "batch_header.txt.j2"),
        template_dir=Path(os.getenv("SUMMARY_TEMPLATE_DIR", "templates")).expanduser(),
        text_template=os.getenv("SUMMARY_TEXT_TEMPLATE", "student_summary.txt.j2"),
        markdown_template=os.getenv("SUMMARY_MARKDOWN_TEMPLATE", "student_summary.md.j2"),
        course_name=os.getenv("COURSE_NAME", ""),
        teacher_name=os.getenv("TEACHER_NAME", ""),
        pdf_enabled=_bool_env("PDF_SUMMARY_ENABLED", False),
        pdf_batch_merge=_bool_env("PDF_BATCH_MERGE", False),
        pdf_page_size=os.getenv("PDF_PAGE_SIZE", "letter"),
        pdf_font=os.getenv("PDF_FONT", "Helvetica"),
        pdf_line_spacing=_float_env("PDF_LINE_SPACING", 1.2),
    )

    yaml_paths: List[Path] = []
    explicit_path = os.getenv("SUMMARY_CONFIG")
    if explicit_path:
        yaml_paths.append(Path(explicit_path).expanduser())
    yaml_paths.append(Path("config/summary.yaml"))
    yaml_paths.append(Path("config.yaml"))

    for candidate in yaml_paths:
        if not candidate or not candidate.exists():
            continue
        if yaml is None:
            break
        try:
            with candidate.open("r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle) or {}
        except Exception:  # pragma: no cover - malformed YAML edge cases
            continue

        section = payload.get("summary") if isinstance(payload, dict) else None
        if not isinstance(section, dict):
            continue

        settings.enabled = _coalesce_bool(section.get("enabled"), settings.enabled)
        settings.markdown_enabled = _coalesce_bool(
            section.get("markdown_enabled"), settings.markdown_enabled
        )
        settings.line_width = max(
            _coalesce_int(section.get("line_width"), settings.line_width), 40
        )
        settings.include_zip_readme = _coalesce_bool(
            section.get("include_zip_readme"), settings.include_zip_readme
        )
        text_template = section.get("text_template")
        if isinstance(text_template, str):
            settings.text_template = text_template
        markdown_template = section.get("markdown_template")
        if isinstance(markdown_template, str):
            settings.markdown_template = markdown_template
        readme_template = section.get("readme_template")
        if isinstance(readme_template, str):
            settings.readme_template = readme_template
        template_dir = section.get("template_dir")
        if isinstance(template_dir, str):
            settings.template_dir = Path(template_dir).expanduser()
        course_name = section.get("course_name")
        if isinstance(course_name, str):
            settings.course_name = course_name
        teacher_name = section.get("teacher_name")
        if isinstance(teacher_name, str):
            settings.teacher_name = teacher_name
        settings.pdf_enabled = _coalesce_bool(section.get("pdf_enabled"), settings.pdf_enabled)
        settings.pdf_batch_merge = _coalesce_bool(
            section.get("pdf_batch_merge"), settings.pdf_batch_merge
        )
        pdf_page_size = section.get("pdf_page_size")
        if isinstance(pdf_page_size, str):
            settings.pdf_page_size = pdf_page_size
        pdf_font = section.get("pdf_font")
        if isinstance(pdf_font, str):
            settings.pdf_font = pdf_font
        pdf_line_spacing = section.get("pdf_line_spacing")
        if isinstance(pdf_line_spacing, (int, float, str)):
            try:
                settings.pdf_line_spacing = float(pdf_line_spacing)
            except (TypeError, ValueError):
                pass
        break

    settings.template_dir = settings.template_dir.resolve()
    return settings


FRIENDLY_FIX_MESSAGE_TEMPLATE = (
    "\"{filename}\" appears to contain little or no selectable text. Please export "
    "from Google Docs/Word using File → Download → PDF (not a scan or photo). You "
    "should be able to select/copy text in the PDF."
)


def _friendly_fix_message(student_name: str) -> str:
    filename = f"{student_name}.pdf"
    return FRIENDLY_FIX_MESSAGE_TEMPLATE.format(filename=filename)


def _coalesce_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return fallback


def _coalesce_int(value: Any, fallback: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return fallback
    return fallback


@dataclass
class JobState:
    """Mutable snapshot of a running or completed batch job."""

    job_id: str
    job_dir: Path
    total: int
    job_name: Optional[str] = None
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    validated: int = 0
    schema_fail: int = 0
    retries_used: int = 0
    text_ok_count: int = 0
    low_text_warning_count: int = 0
    low_text_rejected_count: int = 0
    rubric_version_hash: Optional[str] = None
    status: str = "running"
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: Optional[str] = None
    error: Optional[str] = None
    artifacts: Dict[str, str] = field(default_factory=dict)
    pdf_count: int = 0
    pdf_batch_path: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> Dict[str, Any]:
        """Return an immutable view appropriate for API responses."""

        with self.lock:
            data: Dict[str, Any] = {
                "job_id": self.job_id,
                "job_name": self.job_name,
                "status": self.status,
                "total": self.total,
                "processed": self.processed,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "validated": self.validated,
                "schema_fail": self.schema_fail,
                "retries_used": self.retries_used,
                "text_ok_count": self.text_ok_count,
                "low_text_warning_count": self.low_text_warning_count,
                "low_text_rejected_count": self.low_text_rejected_count,
                "rubric_version_hash": self.rubric_version_hash,
                "artifacts": self.artifacts.copy(),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "pdf_count": self.pdf_count,
                "pdf_batch_path": self.pdf_batch_path,
            }
            if self.error:
                data["error"] = self.error
            return data


class JobManager:
    """Coordinates background execution of batch jobs."""

    def __init__(self, output_base: Path) -> None:
        self.output_base = output_base
        self.output_base.mkdir(parents=True, exist_ok=True)
        self._jobs: Dict[str, JobState] = {}
        self._lock = threading.Lock()

    def start_job(
        self, essays_folder: Path, rubric_path: Path, job_name: Optional[str] = None
    ) -> JobState:
        pdf_paths = _collect_pdf_files(essays_folder)
        if not pdf_paths:
            raise FileNotFoundError(
                f"No PDF essays found in folder: {essays_folder}"
            )

        rubric_file = rubric_path
        if not rubric_file.exists():
            raise FileNotFoundError(f"Rubric file not found: {rubric_path}")

        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        slug = _slugify(job_name) if job_name else None
        job_id = f"{timestamp}-{slug}" if slug else timestamp
        job_dir = self.output_base / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        state = JobState(job_id=job_id, job_dir=job_dir, job_name=job_name, total=len(pdf_paths))

        with self._lock:
            if job_id in self._jobs:
                raise ValueError(f"Job with id '{job_id}' is already running")
            self._jobs[job_id] = state

        thread = threading.Thread(
            target=_run_job,
            name=f"job-runner-{job_id}",
            daemon=True,
            args=(state, pdf_paths, rubric_file, essays_folder),
        )
        thread.start()

        return state

    def get_job(self, job_id: str) -> Optional[JobState]:
        with self._lock:
            return self._jobs.get(job_id)


def _run_job(
    state: JobState,
    pdf_paths: List[Path],
    rubric_path: Path,
    source_folder: Path,
) -> None:
    inputs_dir = state.job_dir / "inputs"
    essays_dir = inputs_dir / "essays"
    outputs_dir = state.job_dir / "outputs"
    outputs_json_dir = outputs_dir / "json"
    outputs_failed_dir = outputs_dir / "json_failed"
    outputs_text_dir = outputs_dir / "text"
    outputs_print_dir = outputs_dir / "print"
    outputs_markdown_dir = outputs_dir / "print_md"
    outputs_pdf_dir = outputs_dir / "print_pdf"
    logs_dir = state.job_dir / "logs"

    summary_settings = _load_summary_settings()

    essays_dir.mkdir(parents=True, exist_ok=True)
    outputs_json_dir.mkdir(parents=True, exist_ok=True)
    outputs_failed_dir.mkdir(parents=True, exist_ok=True)
    outputs_text_dir.mkdir(parents=True, exist_ok=True)
    if summary_settings.enabled:
        outputs_print_dir.mkdir(parents=True, exist_ok=True)
    if summary_settings.markdown_enabled:
        outputs_markdown_dir.mkdir(parents=True, exist_ok=True)
    if summary_settings.pdf_enabled:
        outputs_pdf_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    try:
        rubric_payload = io_utils.read_json_file(str(rubric_path))
        rubric_model = validation.parse_rubric(rubric_payload)
        rubric_dump = rubric_model.model_dump(mode="json")
        rubric_hash = hashlib.sha256(
            json.dumps(rubric_dump, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with state.lock:
            state.rubric_version_hash = rubric_hash
        shutil.copy2(rubric_path, inputs_dir / "rubric.json")
    except ValidationError as exc:
        _finalise_state(state, "failed", error=f"Invalid rubric: {exc}")
        return
    except Exception as exc:  # pragma: no cover - IO edge cases
        _finalise_state(state, "failed", error=str(exc))
        return

    copied_paths = _copy_essays(pdf_paths, essays_dir)
    _write_state_snapshot(state)

    summary_builder = _SummaryBuilder(rubric_model)
    summary_renderer = SummaryRenderer(rubric_model, summary_settings)
    printed_students: List[str] = []
    job_display_name = state.job_name or state.job_id
    pdf_renderer: Optional[PDFSummaryRenderer] = None
    pdf_contexts: List[tuple[str, Dict[str, Any]]] = []
    pdf_generated_count = 0
    batch_pdf_path: Optional[Path] = None
    if summary_settings.pdf_enabled or summary_settings.pdf_batch_merge:
        pdf_renderer = PDFSummaryRenderer(
            PDFSettings(
                page_size=summary_settings.pdf_page_size,
                font=summary_settings.pdf_font,
                line_spacing=summary_settings.pdf_line_spacing,
                course_name=summary_settings.course_name,
                teacher_name=summary_settings.teacher_name,
            )
        )
    job_log_path = logs_dir / "job.log"
    results_log_path = logs_dir / "results.jsonl"

    structured_output = _bool_env("STRUCTURED_OUTPUT", True)
    validation_retry = max(_int_env("VALIDATION_RETRY", 1), 0)
    if not structured_output:
        validation_retry = 0
    trim_text_fields = _bool_env("TRIM_TEXT_FIELDS", True)
    text_validation_config = _load_text_validation_config()

    with job_log_path.open("a", encoding="utf-8") as job_log, results_log_path.open(
        "a", encoding="utf-8"
    ) as results_log:
        for student_name, essay_path in copied_paths:
            start_time = time.perf_counter()
            attempts = 0
            status = "success"
            error: Optional[str] = None
            payload: Optional[Dict[str, Any]] = None
            raw_response: Optional[str] = None
            usage: Optional[Dict[str, Any]] = None
            validation_status = "not_run"
            schema_errors: List[str] = []
            retries_used = 0
            text_length = 0
            chars_per_page_avg = 0.0
            page_count = 0
            text_validation_status = "ok"
            text_validation_message: Optional[str] = None
            thresholds = text_validation_config.thresholds
            summary_modes: Optional[str] = None
            summary_bytes = 0
            printed_flag = False
            summary_context: Dict[str, Any] = {}
            pdf_generated = False
            pdf_bytes = 0
            pdf_path: Optional[Path] = None
            pdf_error: Optional[str] = None

            try:
                extraction = pdf_tools.extract_text_with_metadata(str(essay_path))
                essay_text = extraction.text
                page_count = extraction.page_count
                text_length = len(essay_text)
                chars_per_page_avg = (
                    float(text_length) / float(max(page_count, 1)) if text_length else 0.0
                )

                io_utils.write_text(outputs_text_dir / f"{student_name}.txt", essay_text)

                if text_validation_config.enabled:
                    below_total = text_length < text_validation_config.min_text_chars
                    below_per_page = chars_per_page_avg < text_validation_config.min_chars_per_page
                    if below_total or below_per_page:
                        if text_validation_config.allow_partial_text:
                            text_validation_status = "low_text_warning"
                        else:
                            text_validation_status = "low_text_rejected"
                            text_validation_message = _friendly_fix_message(student_name)
                    else:
                        text_validation_status = "ok"
                else:
                    text_validation_status = "ok"

                if text_validation_status == "low_text_rejected":
                    status = "low_text_rejected"
                    error = text_validation_message
                else:
                    result = ai_client.evaluate_essay(
                        essay_text=essay_text,
                        rubric=rubric_model,
                        validation_retry=validation_retry,
                        trim_text_fields=trim_text_fields,
                    )
                    attempts = result.attempts
                    raw_response = result.raw_text
                    usage = result.usage
                    validation_status = result.status
                    schema_errors = result.schema_errors
                    retries_used = max(attempts - 1, 0)

                    if result.status in {"ok", "retry_ok"} and result.payload is not None:
                        payload = result.payload
                        try:
                            (
                                summary_modes,
                                summary_bytes,
                                printed_flag,
                                summary_context,
                            ) = _generate_printable_summaries(
                                summary_renderer,
                                payload,
                                student_name=student_name,
                                job_name=job_display_name,
                                outputs_print_dir=outputs_print_dir,
                                outputs_markdown_dir=outputs_markdown_dir,
                                text_validation_status=text_validation_status,
                            )
                            if printed_flag:
                                printed_students.append(student_name)
                            context_for_pdf = summary_context.copy() if summary_context else {}
                            if pdf_renderer:
                                if summary_settings.pdf_enabled:
                                    try:
                                        outputs_pdf_dir.mkdir(parents=True, exist_ok=True)
                                        target_pdf = outputs_pdf_dir / f"{student_name}.pdf"
                                        pdf_bytes = pdf_renderer.generate_student_pdf(
                                            context_for_pdf, target_pdf
                                        )
                                        if pdf_bytes > 0:
                                            pdf_generated = True
                                            pdf_path = target_pdf
                                            pdf_generated_count += 1
                                            with state.lock:
                                                state.pdf_count = pdf_generated_count
                                    except (PDFRenderError, OSError) as exc:
                                        pdf_error = str(exc)
                                        pdf_generated = False
                                        pdf_bytes = 0
                                        pdf_path = None
                                if summary_settings.pdf_batch_merge:
                                    pdf_contexts.append((student_name, context_for_pdf))
                            elif summary_settings.pdf_batch_merge:
                                pdf_contexts.append((student_name, summary_context))
                        except (SummaryRenderError, OSError) as exc:
                            status = "failed"
                            error = f"Printable summary failed: {exc}"
                            validation_status = "error"
                            summary_builder.add_failure(student_name)
                            _write_failure_json(
                                outputs_failed_dir,
                                student_name,
                                str(error),
                                raw_response,
                                schema_errors,
                            )
                            payload = None
                            summary_modes = None
                            summary_bytes = 0
                            printed_flag = False
                        else:
                            _write_student_json(outputs_json_dir, student_name, payload)
                            summary_builder.add_success(student_name, payload)
                    elif result.status == "schema_fail":
                        status = "schema_fail"
                        error = (
                            "; ".join(schema_errors)
                            if schema_errors
                            else "Schema validation failed"
                        )
                        summary_builder.add_failure(student_name)
                        _write_failure_json(
                            outputs_failed_dir,
                            student_name,
                            error,
                            raw_response,
                            schema_errors,
                        )
                    else:
                        status = "failed"
                        error = "Evaluation failed"
                        validation_status = "error"
                        summary_builder.add_failure(student_name)
                        _write_failure_json(
                            outputs_failed_dir,
                            student_name,
                            error,
                            raw_response,
                            schema_errors,
                        )
            except (FileNotFoundError, pdf_tools.PDFExtractionError) as exc:
                status = "failed"
                error = str(exc)
                validation_status = "error"
                summary_builder.add_failure(student_name)
                _write_failure_json(outputs_failed_dir, student_name, error)
            except ai_client.AIClientError as exc:
                status = "failed"
                error = str(exc)
                validation_status = "error"
                summary_builder.add_failure(student_name)
                _write_failure_json(
                    outputs_failed_dir,
                    student_name,
                    error,
                    raw_response,
                    schema_errors,
                )
            except Exception as exc:  # pragma: no cover - defensive
                status = "failed"
                error = str(exc)
                validation_status = "error"
                summary_builder.add_failure(student_name)
                _write_failure_json(
                    outputs_failed_dir,
                    student_name,
                    error,
                    raw_response,
                    schema_errors,
                )

            duration_ms = int((time.perf_counter() - start_time) * 1000)
            retries = max(attempts - 1, 0)
            extra_fields = None
            if status == "low_text_rejected":
                extra_fields = [
                    f"chars={text_length}",
                    f"pages={page_count}",
                    f"avg={chars_per_page_avg:.1f}",
                ]
            elif status == "success":
                extra_fields = [
                    str(duration_ms),
                    str(retries),
                    f"printed={'true' if printed_flag else 'false'}",
                    f"printed_pdf={'true' if pdf_generated else 'false'}",
                ]
            _append_job_log(
                job_log,
                student_name,
                status,
                duration_ms,
                retries,
                extra_fields=extra_fields,
            )
            _append_results_log(
                results_log,
                student_name,
                status,
                duration_ms,
                attempts,
                error,
                payload,
                usage,
                raw_response,
                validation_status,
                schema_errors,
                retries,
                essay_source=str(source_folder / f"{student_name}.pdf"),
                text_length=text_length,
                chars_per_page_avg=chars_per_page_avg,
                text_validation_status=text_validation_status,
                text_validation_thresholds=thresholds,
                text_validation_message=text_validation_message,
                print_summary=summary_modes,
                summary_bytes=summary_bytes,
                pdf_generated=pdf_generated,
                pdf_bytes=pdf_bytes,
                pdf_path=str(pdf_path) if pdf_path else None,
                pdf_error=pdf_error,
            )

            _update_counters(
                state,
                status,
                validation_status,
                retries,
                text_validation_status,
            )
            _write_state_snapshot(state)

    if summary_settings.pdf_batch_merge and pdf_renderer and pdf_contexts:
        sorted_contexts = [
            context for _, context in sorted(pdf_contexts, key=lambda item: item[0].casefold())
        ]
        if sorted_contexts:
            candidate = outputs_dir / "batch_all_summaries.pdf"
            try:
                batch_bytes = pdf_renderer.generate_batch_pdf(sorted_contexts, candidate)
            except PDFRenderError as exc:
                batch_pdf_path = None
                batch_bytes = 0
            else:
                if batch_bytes > 0:
                    batch_pdf_path = candidate

    try:
        summary_path = outputs_dir / "summary.csv"
        _write_summary_csv(summary_path, summary_builder)
        zip_path = outputs_dir / "evaluations.zip"
        zip_readme = summary_renderer.render_batch_header(
            job_name=job_display_name,
            generated_at=datetime.utcnow(),
            students=printed_students,
        )
        _write_zip_archive(
            zip_path,
            outputs_json_dir,
            text_dir=outputs_print_dir if summary_settings.enabled else None,
            markdown_dir=outputs_markdown_dir if summary_settings.markdown_enabled else None,
            pdf_dir=outputs_pdf_dir if summary_settings.pdf_enabled else None,
            readme_content=zip_readme,
        )
        with state.lock:
            state.artifacts["csv"] = str(summary_path)
            state.artifacts["zip"] = str(zip_path)
            state.pdf_count = pdf_generated_count
            if batch_pdf_path and batch_pdf_path.exists():
                state.pdf_batch_path = str(batch_pdf_path)
                state.artifacts["pdf_batch"] = str(batch_pdf_path)
    except Exception as exc:  # pragma: no cover - disk issues
        _finalise_state(state, "failed", error=str(exc))
        return

    _finalise_state(state, "completed")


def _append_job_log(
    handle,
    student: str,
    status: str,
    ms: int,
    retries: int,
    *,
    extra_fields: Optional[List[str]] = None,
) -> None:
    timestamp = datetime.utcnow().isoformat()
    parts: List[str] = [timestamp, student, status]
    if extra_fields:
        parts.extend(extra_fields)
    else:
        parts.extend([str(ms), str(retries)])
    handle.write(" | ".join(parts) + "\n")
    handle.flush()


def _append_results_log(
    handle,
    student: str,
    status: str,
    duration_ms: int,
    attempts: int,
    error: Optional[str],
    payload: Optional[Dict[str, Any]],
    usage: Optional[Dict[str, Any]],
    raw_response: Optional[str],
    validation_status: str,
    schema_errors: List[str],
    retries_used: int,
    essay_source: str,
    *,
    text_length: int,
    chars_per_page_avg: float,
    text_validation_status: str,
    text_validation_thresholds: Dict[str, int],
    text_validation_message: Optional[str],
    print_summary: Optional[str] = None,
    summary_bytes: int = 0,
    pdf_generated: bool = False,
    pdf_bytes: int = 0,
    pdf_path: Optional[str] = None,
    pdf_error: Optional[str] = None,
) -> None:
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "student_name": student,
        "status": status,
        "duration_ms": duration_ms,
        "attempts": attempts,
        "error": error,
        "essay_source": essay_source,
        "validation_status": validation_status,
        "schema_errors": schema_errors,
        "retries_used": retries_used,
        "text_length": text_length,
        "chars_per_page_avg": chars_per_page_avg,
        "text_validation_status": text_validation_status,
        "text_validation_thresholds": text_validation_thresholds,
    }
    if usage:
        entry["usage"] = usage
    if payload:
        entry["evaluation"] = payload
    if raw_response:
        entry["raw"] = raw_response
    if text_validation_message:
        entry["text_validation_message"] = text_validation_message
    if print_summary:
        entry["print_summary"] = print_summary
    if summary_bytes:
        entry["summary_bytes"] = summary_bytes
    entry["pdf_generated"] = bool(pdf_generated)
    if pdf_bytes:
        entry["pdf_bytes"] = pdf_bytes
    if pdf_path:
        entry["pdf_path"] = pdf_path
    if pdf_error:
        entry["pdf_error"] = pdf_error

    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    handle.flush()


def _write_state_snapshot(state: JobState) -> None:
    snapshot = state.snapshot()
    state_path = state.job_dir / "logs" / "state.json"
    io_utils.write_json(state_path, snapshot)


def _update_counters(
    state: JobState,
    status: str,
    validation_status: str,
    retries: int,
    text_status: str,
) -> None:
    with state.lock:
        state.processed += 1
        state.retries_used += max(retries, 0)
        if status == "success":
            state.succeeded += 1
        else:
            state.failed += 1
        if validation_status in {"ok", "retry_ok"}:
            state.validated += 1
        elif validation_status == "schema_fail":
            state.schema_fail += 1
        if text_status == "ok":
            state.text_ok_count += 1
        elif text_status == "low_text_warning":
            state.low_text_warning_count += 1
        elif text_status == "low_text_rejected":
            state.low_text_rejected_count += 1


def _finalise_state(state: JobState, status: str, error: Optional[str] = None) -> None:
    with state.lock:
        state.status = status
        state.finished_at = datetime.utcnow().isoformat()
        state.error = error
    _write_state_snapshot(state)


def _write_student_json(outputs_dir: Path, student_name: str, payload: Dict[str, Any]) -> None:
    target_path = outputs_dir / f"{student_name}.json"
    io_utils.write_json(target_path, payload)


def _write_failure_json(
    outputs_dir: Path,
    student_name: str,
    error: str,
    raw_response: Optional[str] = None,
    schema_errors: Optional[List[str]] = None,
) -> None:
    target_path = outputs_dir / f"{student_name}.json"
    failure_payload = {"status": "error", "error": error}
    if raw_response:
        failure_payload["raw_response"] = raw_response
    if schema_errors:
        failure_payload["schema_errors"] = schema_errors
    io_utils.write_json(target_path, failure_payload)


def _write_summary_csv(target: Path, summary: "_SummaryBuilder") -> None:
    rows = summary.rows()
    columns = summary.headers
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_zip_archive(
    target: Path,
    json_dir: Path,
    *,
    text_dir: Optional[Path] = None,
    markdown_dir: Optional[Path] = None,
    pdf_dir: Optional[Path] = None,
    readme_content: Optional[str] = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        if readme_content:
            archive.writestr("README.txt", readme_content)
        for json_file in sorted(json_dir.glob("*.json")):
            archive.write(json_file, arcname=f"json/{json_file.name}")
        if text_dir and text_dir.exists():
            for txt_file in sorted(text_dir.glob("*.txt")):
                archive.write(txt_file, arcname=f"print/{txt_file.name}")
        if markdown_dir and markdown_dir.exists():
            for md_file in sorted(markdown_dir.glob("*.md")):
                archive.write(md_file, arcname=f"print_md/{md_file.name}")
        if pdf_dir and pdf_dir.exists():
            for pdf_file in sorted(pdf_dir.glob("*.pdf")):
                archive.write(pdf_file, arcname=f"print_pdf/{pdf_file.name}")


def _copy_essays(files: Iterable[Path], dest_dir: Path) -> List[tuple[str, Path]]:
    copied: List[tuple[str, Path]] = []
    for path in files:
        target = dest_dir / path.name
        shutil.copy2(path, target)
        copied.append((path.stem, target))
    return copied


def _collect_pdf_files(folder: Path) -> List[Path]:
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"Essays folder not found: {folder}")

    pdfs = [
        path
        for path in sorted(folder.iterdir())
        if path.is_file() and path.suffix.lower() == ".pdf"
    ]
    return pdfs


def _slugify(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    slug = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in name.strip())
    slug = slug.strip("-_")
    return slug or None


class _SummaryBuilder:
    """Accumulates rows for the summary CSV."""

    def __init__(self, rubric: RubricModel) -> None:
        self.criteria_order: List[str] = [criterion.id for criterion in rubric.criteria]
        self.max_scores = {criterion.id: float(criterion.max_score) for criterion in rubric.criteria}
        self.overall_possible = float(rubric.points_possible)
        self.headers: List[str] = [
            "student_name",
            "overall_points_earned",
            "overall_points_possible",
        ] + [f"criterion_{crit}_score" for crit in self.criteria_order]
        self._rows: List[Dict[str, object]] = []

    def add_success(self, student_name: str, evaluation: Dict[str, Any]) -> None:
        scores = self._extract_scores(evaluation)
        total_earned = sum(scores.values()) if scores else None
        row = {
            "student_name": student_name,
            "overall_points_earned": self._format_number(total_earned),
            "overall_points_possible": self._format_number(self.overall_possible),
        }
        for crit in self.criteria_order:
            row[f"criterion_{crit}_score"] = self._format_number(scores.get(crit))
        self._rows.append(row)

    def add_failure(self, student_name: str) -> None:
        row = {
            "student_name": student_name,
            "overall_points_earned": "",
            "overall_points_possible": self._format_number(self.overall_possible),
        }
        for crit in self.criteria_order:
            row[f"criterion_{crit}_score"] = ""
        self._rows.append(row)

    def rows(self) -> List[Dict[str, object]]:
        return sorted(self._rows, key=lambda value: value["student_name"].casefold())

    def _extract_scores(self, evaluation: Dict[str, Any]) -> Dict[str, float]:
        scores: Dict[str, float] = {}
        criteria = evaluation.get("criteria", []) if isinstance(evaluation, dict) else []
        if not isinstance(criteria, list):
            return scores
        for entry in criteria:
            if not isinstance(entry, dict):
                continue
            crit_id = str(entry.get("id")) if entry.get("id") is not None else None
            if not crit_id or crit_id not in self.criteria_order:
                continue
            score = entry.get("score")
            if isinstance(score, (int, float)):
                scores[crit_id] = float(score)
        return scores

    @staticmethod
    def _format_number(value: Optional[float]) -> str:
        if value is None:
            return ""
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return f"{value:.2f}"
        return str(value)


def _generate_printable_summaries(
    renderer: SummaryRenderer,
    payload: Dict[str, Any],
    *,
    student_name: str,
    job_name: str,
    outputs_print_dir: Path,
    outputs_markdown_dir: Path,
    text_validation_status: str,
) -> tuple[Optional[str], int, bool, Dict[str, Any]]:
    """Render and persist printable summaries for a student."""

    flags: Dict[str, Any] = {}
    payload_flags = payload.get("flags") if isinstance(payload, dict) else None
    if isinstance(payload_flags, dict):
        flags.update(payload_flags)
    if text_validation_status == "low_text_warning":
        flags.setdefault("low_text_warning", True)

    rendered_at = datetime.utcnow()
    result = renderer.render_student(
        student_name=student_name,
        evaluation=payload,
        job_name=job_name,
        generated_at=rendered_at,
        flags=flags,
    )

    summary_bytes = 0
    summary_modes: Optional[str] = None
    printed = False

    if result.text and result.text.strip():
        outputs_print_dir.mkdir(parents=True, exist_ok=True)
        target = outputs_print_dir / f"{student_name}.txt"
        io_utils.write_text(target, result.text)
        summary_bytes += len(result.text.encode("utf-8"))
        summary_modes = "txt"
        printed = True

    if result.markdown and result.markdown.strip():
        outputs_markdown_dir.mkdir(parents=True, exist_ok=True)
        target = outputs_markdown_dir / f"{student_name}.md"
        io_utils.write_text(target, result.markdown)
        summary_bytes += len(result.markdown.encode("utf-8"))
        summary_modes = "txt,md" if summary_modes == "txt" else "md"
        printed = True

    return summary_modes, summary_bytes, printed, result.context
