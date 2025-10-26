"""FastAPI entrypoint for Phase 1 batch processing."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote_plus
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, File, Form, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from services import EmailConfigError, EmailDeliveryService
from services.batch_runner import JobManager
from services.rubric_manager import RubricManager, RubricExtractResponse
from utils import io_utils, validation

load_dotenv()


def _normalize_root_path(value: Optional[str]) -> str:
    """Normalize a configured root path into '/prefix' form or empty string."""
    if not value:
        return ""
    value = value.strip()
    if not value or value == "/":
        return ""
    if not value.startswith("/"):
        value = f"/{value}"
    return value.rstrip("/")


APP_ROOT_PATH = _normalize_root_path(os.getenv("APP_ROOT_PATH"))


def _with_root(path: str) -> str:
    """Prefix path with configured root path so links respect reverse proxies."""
    if not path.startswith("/"):
        path = f"/{path}"
    if not APP_ROOT_PATH:
        return path
    return f"{APP_ROOT_PATH}{path}"


def _resolve_output_base() -> Path:
    candidate = os.getenv("OUTPUT_BASE") or os.getenv("APP_BASE_DIR")
    if candidate:
        return Path(candidate).expanduser()
    return Path("/data/sessions")


def _resolve_essay_upload_base() -> Path:
    base = _resolve_output_base() / "essay_uploads"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _slugify_upload_name(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    cleaned = cleaned.strip("-_")
    return cleaned or None


def _allocate_essay_upload_dir(desired_name: Optional[str]) -> Path:
    base = _resolve_essay_upload_base()
    slug = _slugify_upload_name(desired_name)
    if slug:
        target = base / slug
        if target.exists():
            raise ValueError(
                f"Upload destination already exists: {target}. Choose a different folder name."
            )
        target.mkdir(parents=True, exist_ok=False)
        return target

    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    target = base / timestamp
    counter = 1
    while target.exists():
        target = base / f"{timestamp}-{counter}"
        counter += 1
    target.mkdir(parents=True, exist_ok=False)
    return target


def _safe_zip_member_path(member: str) -> Optional[Path]:
    candidate = PurePosixPath(member)
    parts = [part for part in candidate.parts if part not in {"", ".", ".."}]
    if not parts:
        return None
    return Path(*parts)


async def _persist_essay_archive(
    upload_file: UploadFile, *, folder_name: Optional[str]
) -> tuple[Path, int]:
    filename = (upload_file.filename or "").lower()
    if not filename.endswith(".zip"):
        raise ValueError("Please upload a .zip file containing PDF essays.")

    raw = await upload_file.read()
    if not raw:
        raise ValueError("Essay archive upload was empty.")

    try:
        archive = ZipFile(io.BytesIO(raw))
    except BadZipFile as exc:
        raise ValueError("Unable to read archive; ensure it is a valid .zip file.") from exc

    target_dir = _allocate_essay_upload_dir(folder_name)
    extracted = 0

    try:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if not info.filename.lower().endswith(".pdf"):
                continue
            relative = _safe_zip_member_path(info.filename)
            if relative is None:
                continue
            destination = target_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            extracted += 1
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    finally:
        archive.close()

    if extracted <= 0:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise ValueError("The archive did not contain any PDF files.")

    return target_dir, extracted


job_manager = JobManager(output_base=_resolve_output_base())
rubric_manager = RubricManager(base_dir=_resolve_output_base())

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

STATUS_POLL_SECONDS = max(int(os.getenv("STATUS_POLL_SECONDS", "3")), 1)


app = FastAPI(title="Batch Essay Evaluator", version="1.1.0", root_path=APP_ROOT_PATH)


def _context_with_base(context: Dict[str, Any], request: Request) -> Dict[str, Any]:
    base = (request.scope.get("root_path") or "").rstrip("/")
    context["base_url"] = base if base else ""
    return context


class JobRequest(BaseModel):
    essays_folder: str = Field(..., description="Absolute path to folder containing PDF essays")
    rubric_path: str = Field(..., description="Absolute path to rubric JSON file")
    job_name: Optional[str] = Field(None, description="Optional label included in the job id")


class JobResponse(BaseModel):
    job_id: str
    status: str
    total: int
    processed: int


class JobStatusResponse(JobResponse):
    succeeded: int
    failed: int
    validated: int
    schema_fail: int
    retries_used: int
    text_ok_count: int
    low_text_warning_count: int
    low_text_rejected_count: int
    rubric_version_hash: Optional[str]
    artifacts: Dict[str, Optional[str]]
    started_at: Optional[str]
    finished_at: Optional[str]
    error: Optional[str] = None
    archived: bool = False


class RubricErrorModel(BaseModel):
    loc: str
    msg: str


class RubricExtractResponseModel(BaseModel):
    temp_id: str
    status: Literal["valid", "needs_fix", "failed"]
    canonical_json: Optional[Dict[str, Any]] = None
    provisional_json: Optional[Dict[str, Any]] = None
    errors: List[RubricErrorModel] = Field(default_factory=list)
    error_messages: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    log_path: str
    fix_url: Optional[str] = None
    save_url: Optional[str] = None
    canonical_path: Optional[str] = None


class RubricSaveResponse(BaseModel):
    ok: bool
    validate_only: bool
    errors: List[RubricErrorModel] = Field(default_factory=list)
    error_messages: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    canonical_path: Optional[str]
    version_hash: Optional[str]


class EmailAttachmentOptions(BaseModel):
    attach_txt: Optional[bool] = None
    attach_pdf: Optional[bool] = None
    attach_json: Optional[bool] = None


class EmailPreviewRequest(EmailAttachmentOptions):
    pass


class EmailPreviewItem(BaseModel):
    student_name: str
    email: str
    section: Optional[str]
    match_status: str
    reason: Optional[str] = None
    intended_attachments: List[str] = Field(default_factory=list)
    attachments_ready: List[str] = Field(default_factory=list)
    evaluation_found: bool
    overall_points_earned: Optional[int] = None
    overall_points_possible: Optional[int] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    extras: Dict[str, Any] = Field(default_factory=dict)


class EmailPreviewResponse(BaseModel):
    job_id: str
    job_name: Optional[str]
    dry_run: bool = True
    total_csv: int
    matched: int
    unmatched: int
    ready_to_send: int
    skipped_no_eval: int
    skipped_no_match: int
    invalid_email: int
    missing_attachment: int
    failed_template: int
    ambiguous_email: int
    items: List[EmailPreviewItem]
    unmatched_evaluations: List[str] = Field(default_factory=list)
    attachment_config: Dict[str, bool]
    samples: List[Dict[str, str]] = Field(default_factory=list)


class EmailSendRequest(EmailAttachmentOptions):
    dry_run: bool = Field(False, description="Set to false to execute the send operation.")


class EmailSendResult(BaseModel):
    student_name: str
    email: str
    status: str
    attachments: str
    reason: Optional[str] = None
    attempts: int
    timestamp: str


class EmailSendResponse(BaseModel):
    job_id: str
    job_name: Optional[str]
    dry_run: bool = False
    report_path: str
    report_url: Optional[str]
    total: int
    sent: int
    failed: int
    status_counts: Dict[str, int]
    results: List[EmailSendResult]
    unmatched_evaluations: List[str] = Field(default_factory=list)
    attachment_config: Dict[str, bool]


@app.post("/jobs", response_model=JobResponse)
async def create_job(request: JobRequest) -> Dict[str, Any]:
    try:
        return _start_job_response(
            essays_folder=Path(request.essays_folder),
            rubric_path=Path(request.rubric_path),
            job_name=request.job_name,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/jobs/new", response_class=HTMLResponse)
async def new_job_form(request: Request) -> HTMLResponse:
    context = _job_form_context(request)
    return templates.TemplateResponse("job_new.html", _context_with_base(context, request))


@app.post("/jobs/new", response_class=HTMLResponse)
async def submit_job_form(
    request: Request,
    essays_folder: str = Form(""),
    rubric_path: str = Form(""),
    job_name: str = Form(""),
    rubric_file: Optional[UploadFile] = File(None),
) -> Response:
    errors: Dict[str, str] = {}
    general_error: Optional[str] = None

    essays_folder_raw = (essays_folder or "").strip()
    rubric_path_raw = (rubric_path or "").strip()
    job_name_raw = (job_name or "").strip()
    job_name_value = job_name_raw or None

    essays_path: Optional[Path] = None
    rubric_source: Optional[Path] = None

    if not essays_folder_raw:
        errors["essays_folder"] = "Provide the path to a folder of student PDFs."
    else:
        try:
            essays_path = _validate_essays_folder(Path(essays_folder_raw))
        except ValueError as exc:
            errors["essays_folder"] = str(exc)

    upload_file = rubric_file if rubric_file and (rubric_file.filename or "").strip() else None
    try:
        rubric_source = await _resolve_rubric_source(
            upload_file=upload_file,
            rubric_path=rubric_path_raw,
        )
    except ValueError as exc:
        errors["rubric"] = str(exc)

    if errors:
        context = _job_form_context(
            request,
            values={
                "essays_folder": essays_folder_raw,
                "rubric_path": rubric_path_raw,
                "job_name": job_name_raw,
            },
            errors=errors,
        )
        return templates.TemplateResponse(
            "job_new.html", _context_with_base(context, request), status_code=400
        )

    assert essays_path is not None and rubric_source is not None

    try:
        response = _start_job_response(
            essays_folder=essays_path,
            rubric_path=rubric_source,
            job_name=job_name_value,
        )
    except (FileNotFoundError, ValueError) as exc:
        general_error = str(exc)
        context = _job_form_context(
            request,
            values={
                "essays_folder": essays_folder_raw,
                "rubric_path": rubric_path_raw,
                "job_name": job_name_raw,
            },
            errors=errors,
            general_error=general_error,
        )
        return templates.TemplateResponse(
            "job_new.html", _context_with_base(context, request), status_code=400
        )

    job_id = response.get("job_id")
    if not job_id:
        general_error = "Unable to start job; missing identifier."
        context = _job_form_context(
            request,
            values={
                "essays_folder": essays_folder_raw,
                "rubric_path": rubric_path_raw,
                "job_name": job_name_raw,
            },
            errors=errors,
            general_error=general_error,
        )
        return templates.TemplateResponse(
            "job_new.html", _context_with_base(context, request), status_code=500
        )

    return RedirectResponse(url=_with_root(f"/jobs/{job_id}"), status_code=303)


@app.post("/jobs/upload-essays", response_class=HTMLResponse)
async def upload_essays_archive(
    request: Request,
    essay_zip: UploadFile = File(...),
    target_folder: str = Form(""),
) -> HTMLResponse:
    upload_feedback: Dict[str, Any]
    values = {
        "essays_folder": "",
        "rubric_path": "",
        "job_name": "",
    }
    status_code = 200

    has_file = essay_zip is not None and (essay_zip.filename or "").strip()
    if not has_file:
        upload_feedback = {
            "status": "error",
            "message": "Select a .zip file that contains your essay PDFs.",
        }
        status_code = 400
    else:
        try:
            folder, count = await _persist_essay_archive(
                essay_zip, folder_name=target_folder or None
            )
        except ValueError as exc:
            upload_feedback = {"status": "error", "message": str(exc)}
            status_code = 400
        else:
            upload_feedback = {
                "status": "success",
                "message": f"Uploaded {count} PDF file(s).",
                "path": str(folder),
            }
            values["essays_folder"] = str(folder)

    context = _job_form_context(
        request,
        values=values,
        upload_feedback=upload_feedback,
    )
    return templates.TemplateResponse(
        "job_new.html", _context_with_base(context, request), status_code=status_code
    )


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request) -> HTMLResponse:
    entries = _list_jobs(limit=80)
    active_jobs = [job for job in entries if not job.get("archived")]
    archived_jobs = [job for job in entries if job.get("archived")]
    context = {
        "request": request,
        "active_jobs": active_jobs,
        "archived_jobs": archived_jobs,
    }
    return templates.TemplateResponse("jobs.html", _context_with_base(context, request))


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def job_status(job_id: str, request: Request) -> Dict[str, Any] | HTMLResponse:
    state = job_manager.get_job(job_id)
    if state:
        snapshot = state.snapshot()
    else:
        snapshot = _load_snapshot_from_disk(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    if _wants_html(request):
        context = _job_status_context(request, job_id, snapshot)
        return templates.TemplateResponse("job_status.html", _context_with_base(context, request))

    return _format_status_response(snapshot)


@app.post("/jobs/{job_id}/archive")
async def archive_job(job_id: str, request: Request):
    archived_flag = await _extract_archive_flag(request)
    snapshot = _set_job_archived(job_id, archived_flag)

    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        return {"job_id": job_id, "archived": snapshot.get("archived", False)}

    referer = request.headers.get("referer") or _with_root("/jobs")
    return RedirectResponse(url=referer, status_code=303)


@app.get("/jobs/{job_id}/download/{artifact}")
async def job_artifact(job_id: str, artifact: str):  # type: ignore[override]
    snapshot = _load_snapshot_from_disk(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    artifacts = snapshot.get("artifacts", {}) or {}
    if artifact not in {"csv", "zip"}:
        raise HTTPException(status_code=404, detail="Unknown artifact requested")

    path_str = artifacts.get(artifact)
    if not path_str:
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact}' not ready for job '{job_id}'")

    path = Path(path_str)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Artifact file missing on disk")

    return FileResponse(path)


@app.get("/jobs/{job_id}/students/{student_name}/summary.txt")
async def student_summary_txt(job_id: str, student_name: str):  # type: ignore[override]
    path = _resolve_student_summary_path(job_id, student_name, "txt")
    return FileResponse(path, media_type="text/plain", filename=path.name)


@app.get("/jobs/{job_id}/students/{student_name}/summary.md")
async def student_summary_md(job_id: str, student_name: str):  # type: ignore[override]
    path = _resolve_student_summary_path(job_id, student_name, "md")
    return FileResponse(path, media_type="text/markdown", filename=path.name)


@app.get("/jobs/{job_id}/students/{student_name}/summary.pdf")
async def student_summary_pdf(job_id: str, student_name: str):  # type: ignore[override]
    path = _resolve_student_summary_path(job_id, student_name, "pdf")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/jobs/{job_id}/batch.pdf")
async def batch_summary_pdf(job_id: str):  # type: ignore[override]
    snapshot = _load_snapshot_from_disk(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    artifacts = snapshot.get("artifacts", {}) or {}
    path_str = artifacts.get("pdf_batch") or snapshot.get("pdf_batch_path")
    if not path_str:
        raise HTTPException(status_code=404, detail="Batch PDF not available")

    path = Path(path_str)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Batch PDF missing on disk")

    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/jobs/{job_id}/logs/job.log")
async def job_log_file(job_id: str):
    base = _resolve_output_base()
    log_path = base / job_id / "logs" / "job.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(log_path, media_type="text/plain", filename="job.log")


@app.get("/jobs/{job_id}/logs/tail", response_class=PlainTextResponse)
async def job_log_tail(job_id: str, limit: int = 15) -> PlainTextResponse:
    lines = _read_log_tail(job_id, limit=limit)
    return PlainTextResponse("\n".join(lines))


@app.get("/jobs/{job_id}/email", response_class=HTMLResponse)
async def email_console(job_id: str, request: Request) -> HTMLResponse:
    try:
        job_dir, snapshot = _resolve_job_context(
            job_id, require_completed=False, require_validated=False
        )
        console_error: Optional[str] = None
    except HTTPException as exc:
        snapshot = _load_snapshot_from_disk(job_id)
        if snapshot is None:
            raise
        job_dir = _resolve_output_base() / job_id
        console_error = exc.detail

    csv_path = job_dir / "inputs" / "students.csv"
    csv_exists = csv_path.exists()
    csv_modified: Optional[str] = None
    if csv_exists:
        try:
            csv_modified = datetime.utcfromtimestamp(csv_path.stat().st_mtime).isoformat()
        except OSError:
            csv_modified = None

    email_report_path = job_dir / "outputs" / "email_report.csv"
    report_exists = email_report_path.exists()

    attachment_defaults = {
        "attach_txt": True,
        "attach_pdf": True,
        "attach_json": False,
    }
    service_error: Optional[str] = None
    try:
        service = EmailDeliveryService(job_id=job_id, job_dir=job_dir, snapshot=snapshot)
        attachment_defaults = _serialize_attachment_config(service.attachment_config)
    except EmailConfigError as exc:
        service_error = str(exc)

    context = {
        "request": request,
        "job_id": job_id,
        "snapshot": snapshot,
        "csv_exists": csv_exists,
        "csv_modified": csv_modified,
        "report_exists": report_exists,
        "attachment_defaults": attachment_defaults,
        "console_error": console_error,
        "service_error": service_error,
        "upload_message": request.query_params.get("uploaded"),
        "upload_error": request.query_params.get("error"),
    }
    return templates.TemplateResponse("email_console.html", _context_with_base(context, request))


@app.post("/jobs/{job_id}/email/upload_csv")
async def email_upload_csv(job_id: str, students_csv: UploadFile = File(...)) -> RedirectResponse:
    job_dir, _snapshot = _resolve_job_context(
        job_id, require_completed=False, require_validated=False
    )

    try:
        raw_bytes = await students_csv.read()
        if not raw_bytes:
            raise ValueError("Uploaded file is empty")
        decoded = raw_bytes.decode("utf-8-sig")
        _validate_students_csv_content(decoded)
    except UnicodeDecodeError:
        return RedirectResponse(
            url=_with_root(
                f"/jobs/{job_id}/email?error={quote_plus('CSV must be UTF-8 encoded')}"
            ),
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse(
            url=_with_root(f"/jobs/{job_id}/email?error={quote_plus(str(exc))}"),
            status_code=303,
        )

    target = job_dir / "inputs" / "students.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(decoded, encoding="utf-8")

    return RedirectResponse(url=_with_root(f"/jobs/{job_id}/email?uploaded=1"), status_code=303)


@app.post("/jobs/{job_id}/email/preview", response_model=EmailPreviewResponse)
async def email_preview(
    job_id: str,
    options: EmailPreviewRequest = Body(default=EmailPreviewRequest()),
) -> EmailPreviewResponse:
    job_dir, snapshot = _resolve_job_context(job_id)
    overrides = _extract_attachment_overrides(options)

    try:
        service = EmailDeliveryService(job_id=job_id, job_dir=job_dir, snapshot=snapshot)
        preparation = await run_in_threadpool(service.prepare, overrides)
    except EmailConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    summary = EmailDeliveryService.summarize_prepared(preparation.prepared)
    items: List[EmailPreviewItem] = []
    for entry in preparation.prepared:
        overall = entry.overall or {}
        overall_earned = overall.get("points_earned") if isinstance(overall, dict) else None
        overall_possible = overall.get("points_possible") if isinstance(overall, dict) else None
        items.append(
            EmailPreviewItem(
                student_name=entry.student.student_name,
                email=entry.student.email,
                section=entry.student.section,
                match_status=entry.status,
                reason=entry.reason,
                intended_attachments=entry.intended_labels(),
                attachments_ready=entry.attachment_labels(),
                evaluation_found=entry.evaluation_found,
                overall_points_earned=overall_earned,
                overall_points_possible=overall_possible,
                subject=entry.subject,
                body=entry.body,
                extras=entry.extras,
            )
        )

    samples: List[Dict[str, str]] = []
    for item in items:
        if item.match_status == "ready" and item.subject and item.body:
            samples.append(
                {
                    "student_name": item.student_name,
                    "subject": item.subject,
                    "body": item.body,
                }
            )
        if len(samples) >= 2:
            break

    return EmailPreviewResponse(
        job_id=job_id,
        job_name=service.job_name,
        dry_run=True,
        total_csv=preparation.total_students,
        matched=summary.get("matched", 0),
        unmatched=summary.get("unmatched", 0),
        ready_to_send=summary.get("ready", 0),
        skipped_no_eval=summary.get("missing_eval", 0),
        skipped_no_match=summary.get("ambiguous_match", 0),
        invalid_email=summary.get("invalid_email", 0),
        missing_attachment=summary.get("missing_attachment", 0),
        failed_template=summary.get("template_error", 0),
        ambiguous_email=summary.get("ambiguous_email", 0),
        items=items,
        unmatched_evaluations=preparation.unmatched_evaluations,
        attachment_config=_serialize_attachment_config(preparation.attachment_config),
        samples=samples,
    )


@app.post("/jobs/{job_id}/email/send", response_model=EmailSendResponse)
async def email_send(job_id: str, request: EmailSendRequest) -> EmailSendResponse:
    if request.dry_run:
        raise HTTPException(
            status_code=400,
            detail="Set 'dry_run' to false to send emails or use the preview endpoint.",
        )

    job_dir, snapshot = _resolve_job_context(job_id)
    overrides = _extract_attachment_overrides(request)

    try:
        service = EmailDeliveryService(job_id=job_id, job_dir=job_dir, snapshot=snapshot)
        preparation = await run_in_threadpool(service.prepare, overrides)
    except EmailConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = await run_in_threadpool(service.send, preparation.prepared)
    report_path = await run_in_threadpool(service.write_report, rows)
    status_counts = Counter(row["status"] for row in rows)
    sent_count = status_counts.get("sent", 0)
    failed_count = status_counts.get("failed_smtp", 0)
    results = [EmailSendResult(**row) for row in rows]

    await run_in_threadpool(_record_email_report, job_dir, report_path)

    return EmailSendResponse(
        job_id=job_id,
        job_name=service.job_name,
        dry_run=False,
        report_path=str(report_path),
        report_url=_with_root(f"/jobs/{job_id}/email/report"),
        total=len(rows),
        sent=sent_count,
        failed=failed_count,
        status_counts=dict(status_counts),
        results=results,
        unmatched_evaluations=preparation.unmatched_evaluations,
        attachment_config=_serialize_attachment_config(preparation.attachment_config),
    )


@app.get("/jobs/{job_id}/email/report")
async def email_report(job_id: str):
    report_path = _resolve_output_base() / job_id / "outputs" / "email_report.csv"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Email report not available")
    return FileResponse(report_path, media_type="text/csv", filename="email_report.csv")


@app.post("/rubrics/extract", response_model=RubricExtractResponseModel)
async def rubric_extract(
    rubric_file: UploadFile = File(...),
    job_name: Optional[str] = Form(None),
) -> Dict[str, Any]:
    content = await rubric_file.read()
    filename = rubric_file.filename or "rubric.pdf"
    try:
        result = rubric_manager.extract(
            filename=filename,
            content=content,
            job_name=job_name,
            content_type=rubric_file.content_type,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return _serialize_rubric_extract(result)


@app.get("/rubrics/{temp_id}/fix", response_class=HTMLResponse)
async def rubric_fix_page(temp_id: str, request: Request) -> HTMLResponse:
    session = rubric_manager.get_session(temp_id)
    if not session:
        raise HTTPException(status_code=404, detail="Rubric session not found")

    initial = session.provisional or session.canonical or {}
    try:
        initial_json = json.dumps(initial, ensure_ascii=False, indent=2)
    except TypeError:
        initial_json = "{}"

    context = {
        "request": request,
        "temp_id": temp_id,
        "initial_json": initial_json,
        "errors": session.error_messages,
        "warnings": session.warnings,
        "save_url": _with_root(f"/rubrics/{temp_id}/save"),
        "validate_url": _with_root(f"/rubrics/{temp_id}/save?validate_only=1"),
        "download_url": _with_root(f"/rubrics/{temp_id}/download"),
    }
    return templates.TemplateResponse("rubric_fix.html", _context_with_base(context, request))


@app.get("/rubrics/{temp_id}/preview", response_class=HTMLResponse)
async def rubric_preview(temp_id: str, request: Request) -> HTMLResponse:
    session = rubric_manager.get_session(temp_id)
    if not session or not session.canonical:
        raise HTTPException(status_code=404, detail="Canonical rubric not found")

    rubric = session.canonical
    criteria = rubric.get("criteria", []) if isinstance(rubric, dict) else []
    context = {
        "request": request,
        "temp_id": temp_id,
        "rubric": rubric,
        "criteria": criteria,
    }
    return templates.TemplateResponse("rubric_preview.html", _context_with_base(context, request))


@app.get("/rubrics/{temp_id}/download")
async def rubric_download(temp_id: str):
    session = rubric_manager.get_session(temp_id)
    if not session or not session.canonical_path or not session.canonical_path.exists():
        raise HTTPException(status_code=404, detail="Canonical rubric not available")
    return FileResponse(session.canonical_path, filename="rubric.json")


@app.post("/rubrics/{temp_id}/save", response_model=RubricSaveResponse)
async def rubric_save(temp_id: str, request: Request, validate_only: bool = False):
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc.msg}") from exc

    rubric_payload = payload.get("rubric") if isinstance(payload, dict) else None
    if rubric_payload is None:
        raise HTTPException(status_code=400, detail="Request body must include 'rubric'")

    try:
        rubric_manager.record_manual_payload(temp_id, rubric_payload)
        session, normalization = rubric_manager.validate_and_save(
            temp_id,
            rubric_payload,
            validate_only=validate_only,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    errors = [RubricErrorModel(**error) for error in normalization.errors]
    return RubricSaveResponse(
        ok=normalization.is_valid,
        validate_only=validate_only,
        errors=errors,
        error_messages=normalization.error_messages,
        warnings=normalization.warnings,
        canonical_path=str(session.canonical_path) if session.canonical_path else None,
        version_hash=session.version_hash,
    )


def _list_jobs(limit: int = 40) -> List[Dict[str, Any]]:
    base = _resolve_output_base()
    if not base.exists() or not base.is_dir():
        return []

    candidates: List[tuple[float, Path]] = []
    for path in base.iterdir():
        if not path.is_dir():
            continue
        try:
            timestamp = path.stat().st_mtime
        except OSError:
            continue
        candidates.append((timestamp, path))

    candidates.sort(key=lambda item: item[0], reverse=True)
    jobs: List[Dict[str, Any]] = []
    for _, path in candidates:
        job_id = path.name
        snapshot = _load_snapshot_from_disk(job_id)
        if snapshot is None:
            continue
        jobs.append(snapshot)
        if len(jobs) >= limit:
            break
    return jobs


async def _extract_archive_flag(request: Request) -> bool:
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc.msg}") from exc
        raw_value = payload.get("archived") if isinstance(payload, dict) else None
    else:
        form = await request.form()
        raw_value = form.get("archived")

    if raw_value is None:
        raise HTTPException(status_code=400, detail="Request must include an 'archived' value.")

    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False

    raise HTTPException(status_code=400, detail="Unable to interpret 'archived' value as boolean.")


def _set_job_archived(job_id: str, archived: bool) -> Dict[str, Any]:
    state = job_manager.get_job(job_id)
    if state:
        with state.lock:
            state.archived = archived
        snapshot = state.snapshot()
        job_dir = state.job_dir
    else:
        snapshot = _load_snapshot_from_disk(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        snapshot["archived"] = archived
        job_dir = _resolve_output_base() / job_id

    if not job_dir.exists():
        raise HTTPException(status_code=404, detail=f"Job directory missing for '{job_id}'")

    state_path = job_dir / "logs" / "state.json"
    io_utils.write_json(state_path, snapshot)
    return snapshot


def _start_job_response(essays_folder: Path, rubric_path: Path, job_name: Optional[str]) -> Dict[str, Any]:
    state = job_manager.start_job(
        essays_folder=essays_folder,
        rubric_path=rubric_path,
        job_name=job_name,
    )
    snapshot = state.snapshot()
    return {
        "job_id": snapshot["job_id"],
        "status": snapshot["status"],
        "total": snapshot["total"],
        "processed": snapshot["processed"],
    }


def _job_form_context(
    request: Request,
    *,
    values: Optional[Dict[str, str]] = None,
    errors: Optional[Dict[str, str]] = None,
    general_error: Optional[str] = None,
    upload_feedback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    defaults = {
        "essays_folder": "",
        "rubric_path": "",
        "job_name": "",
    }
    if values:
        defaults.update(values)
    return {
        "request": request,
        "values": defaults,
        "errors": errors or {},
        "general_error": general_error,
        "recent_jobs": _list_jobs(limit=5),
        "allowed_roots": os.getenv("ALLOWED_ROOTS", ""),
        "upload_feedback": upload_feedback,
        "essay_upload_base": str(_resolve_essay_upload_base()),
    }


def _validate_essays_folder(folder: Path) -> Path:
    candidate = folder.expanduser()
    if not candidate.exists():
        raise ValueError("Essays folder was not found.")
    if not candidate.is_dir():
        raise ValueError("Essays folder must be a directory.")

    try:
        pdf_count = sum(
            1
            for path in candidate.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        )
    except OSError as exc:
        raise ValueError(f"Unable to inspect essays folder: {exc}") from exc

    if pdf_count <= 0:
        raise ValueError("Add at least one '.pdf' file to the essays folder.")
    return candidate


async def _resolve_rubric_source(
    *,
    upload_file: Optional[UploadFile],
    rubric_path: str,
) -> Path:
    trimmed_path = rubric_path.strip()
    has_upload = upload_file is not None and (upload_file.filename or "").strip()

    if has_upload:
        return await _persist_uploaded_rubric(upload_file)

    if trimmed_path:
        return _validate_rubric_path(Path(trimmed_path))

    raise ValueError("Upload rubric.json or provide a local rubric path.")


def _validate_rubric_path(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.exists():
        raise ValueError("Rubric file path does not exist.")
    if not candidate.is_file():
        raise ValueError("Rubric path must reference a file.")
    try:
        payload = io_utils.read_json_file(str(candidate))
        validation.parse_rubric(payload)
    except (FileNotFoundError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"Invalid rubric file: {exc}") from exc
    return candidate


async def _persist_uploaded_rubric(upload_file: UploadFile) -> Path:
    raw = await upload_file.read()
    if not raw:
        raise ValueError("Rubric upload was empty.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Rubric upload must be UTF-8 text.") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Rubric JSON error: {exc.msg}") from exc

    try:
        validation.parse_rubric(payload)
    except ValidationError as exc:
        messages = validation.format_validation_errors(exc)
        raise ValueError(
            messages[0] if messages else "Uploaded rubric failed schema validation."
        ) from exc

    upload_dir = _resolve_output_base() / "_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"rubric-{uuid4().hex}.json"
    target = upload_dir / filename
    io_utils.write_json(target, payload)
    return target


def _wants_html(request: Request) -> bool:
    forced = request.query_params.get("format")
    if forced == "json":
        return False
    if forced == "html":
        return True

    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _job_status_context(
    request: Request,
    job_id: str,
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    status = (snapshot.get("status") or "unknown").lower()
    total = int(snapshot.get("total") or 0)
    processed = int(snapshot.get("processed") or 0)
    progress_pct = int((processed / total) * 100) if total else 0
    artifacts = snapshot.get("artifacts") or {}
    links = {
        "csv": _with_root(f"/jobs/{job_id}/download/csv")
        if _artifact_is_ready(artifacts.get("csv"))
        else None,
        "zip": _with_root(f"/jobs/{job_id}/download/zip")
        if _artifact_is_ready(artifacts.get("zip"))
        else None,
        "pdf_batch": _with_root(f"/jobs/{job_id}/batch.pdf")
        if _artifact_is_ready(artifacts.get("pdf_batch"))
        else None,
    }
    counts = {
        "Total essays": total,
        "Processed": processed,
        "Validated": int(snapshot.get("validated") or 0),
        "Succeeded": int(snapshot.get("succeeded") or 0),
        "Failed": int(snapshot.get("failed") or 0),
        "Schema fail": int(snapshot.get("schema_fail") or 0),
        "Low text rejected": int(snapshot.get("low_text_rejected_count") or 0),
    }
    started_display = _format_timestamp(snapshot.get("started_at"))
    finished_display = _format_timestamp(snapshot.get("finished_at"))
    log_lines = _read_log_tail(job_id, limit=15)

    return {
        "request": request,
        "job_id": job_id,
        "job_name": snapshot.get("job_name") or job_id,
        "snapshot": snapshot,
        "status": status,
        "status_label": status.capitalize(),
        "is_active": status in {"running", "pending"},
        "status_poll_seconds": STATUS_POLL_SECONDS,
        "progress_pct": progress_pct,
        "counts": counts,
        "artifact_links": links,
        "error_message": snapshot.get("error"),
        "log_lines": log_lines,
        "log_available": bool(log_lines),
        "log_download_url": _with_root(f"/jobs/{job_id}/logs/job.log"),
        "started_display": started_display,
        "finished_display": finished_display,
    }


def _format_timestamp(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


def _artifact_is_ready(path: Optional[str]) -> bool:
    if not path:
        return False
    candidate = Path(path)
    return candidate.exists()


def _read_log_tail(job_id: str, limit: int = 15) -> List[str]:
    log_path = _resolve_output_base() / job_id / "logs" / "job.log"
    if not log_path.exists():
        return []
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    if limit <= 0:
        limit = 1
    return [line.rstrip("\n") for line in lines[-limit:]]


def _serialize_attachment_config(config: Any) -> Dict[str, bool]:
    return {
        "attach_txt": bool(getattr(config, "attach_txt", False)),
        "attach_pdf": bool(getattr(config, "attach_pdf", False)),
        "attach_json": bool(getattr(config, "attach_json", False)),
    }


def _extract_attachment_overrides(options: EmailAttachmentOptions) -> Dict[str, bool]:
    overrides: Dict[str, bool] = {}
    if options.attach_txt is not None:
        overrides["attach_txt"] = bool(options.attach_txt)
    if options.attach_pdf is not None:
        overrides["attach_pdf"] = bool(options.attach_pdf)
    if options.attach_json is not None:
        overrides["attach_json"] = bool(options.attach_json)
    return overrides


def _record_email_report(job_dir: Path, report_path: Path) -> None:
    state_path = job_dir / "logs" / "state.json"
    if not state_path.exists():
        return
    try:
        with state_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return

    artifacts = data.get("artifacts") or {}
    artifacts["email_report"] = str(report_path)
    data["artifacts"] = artifacts
    try:
        io_utils.write_json(state_path, data)
    except Exception:
        pass


def _validate_students_csv_content(payload: str) -> None:
    reader = csv.DictReader(io.StringIO(payload))
    if reader.fieldnames is None:
        raise ValueError("students.csv must include a header row")

    header_lookup = {
        (name or "").lower().strip(): name for name in reader.fieldnames if name
    }
    if "student_name" not in header_lookup or "email" not in header_lookup:
        raise ValueError("students.csv must include 'student_name' and 'email' columns")

    name_header = header_lookup["student_name"]
    email_header = header_lookup["email"]
    for row in reader:
        student_name = (row.get(name_header) or "").strip()
        email = (row.get(email_header) or "").strip()
        if student_name and email:
            return
    raise ValueError("students.csv must include at least one name/email row")


def _resolve_job_context(
    job_id: str,
    *,
    require_completed: bool = True,
    require_validated: bool = True,
) -> tuple[Path, Dict[str, Any]]:
    state = job_manager.get_job(job_id)
    if state:
        snapshot = state.snapshot()
        job_dir = state.job_dir
    else:
        snapshot = _load_snapshot_from_disk(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        job_dir = _resolve_output_base() / job_id

    if not job_dir.exists():
        raise HTTPException(status_code=404, detail=f"Job directory missing for '{job_id}'")

    status = (snapshot.get("status") or "").lower()
    if require_completed and status in {"running", "pending"}:
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' is not ready for email delivery (status: {status})",
        )

    if require_validated and snapshot.get("validated", 0) <= 0:
        raise HTTPException(status_code=400, detail="No validated evaluations available for email")

    return job_dir, snapshot


def _load_snapshot_from_disk(job_id: str) -> Optional[Dict[str, Any]]:
    base = _resolve_output_base()
    state_path = base / job_id / "logs" / "state.json"
    if not state_path.exists():
        return None

    with state_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    # Ensure required keys exist for consistent responses.
    data.setdefault("artifacts", {})
    data.setdefault("succeeded", 0)
    data.setdefault("failed", 0)
    data.setdefault("validated", 0)
    data.setdefault("schema_fail", 0)
    data.setdefault("retries_used", 0)
    data.setdefault("text_ok_count", 0)
    data.setdefault("low_text_warning_count", 0)
    data.setdefault("low_text_rejected_count", 0)
    data.setdefault("rubric_version_hash", None)
    data.setdefault("processed", 0)
    data.setdefault("total", 0)
    data.setdefault("job_name", None)
    data.setdefault("pdf_count", 0)
    data.setdefault("pdf_batch_path", None)
    data.setdefault("archived", False)
    artifacts = data["artifacts"]
    if "csv" not in artifacts:
        artifacts["csv"] = None
    if "zip" not in artifacts:
        artifacts["zip"] = None
    if "pdf_batch" not in artifacts:
        artifacts["pdf_batch"] = None
    return data


def _format_status_response(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    artifacts = snapshot.get("artifacts", {}) or {}
    return {
        "job_id": snapshot.get("job_id"),
        "status": snapshot.get("status"),
        "total": snapshot.get("total", 0),
        "processed": snapshot.get("processed", 0),
        "succeeded": snapshot.get("succeeded", 0),
        "failed": snapshot.get("failed", 0),
        "validated": snapshot.get("validated", 0),
        "schema_fail": snapshot.get("schema_fail", 0),
        "retries_used": snapshot.get("retries_used", 0),
        "text_ok_count": snapshot.get("text_ok_count", 0),
        "low_text_warning_count": snapshot.get("low_text_warning_count", 0),
        "low_text_rejected_count": snapshot.get("low_text_rejected_count", 0),
        "rubric_version_hash": snapshot.get("rubric_version_hash"),
        "pdf_count": snapshot.get("pdf_count", 0),
        "pdf_batch_path": snapshot.get("pdf_batch_path"),
        "artifacts": {
            "csv": artifacts.get("csv"),
            "zip": artifacts.get("zip"),
            "pdf_batch": artifacts.get("pdf_batch"),
        },
        "started_at": snapshot.get("started_at"),
        "finished_at": snapshot.get("finished_at"),
        "error": snapshot.get("error"),
        "archived": snapshot.get("archived", False),
    }


def _serialize_rubric_extract(result: RubricExtractResponse) -> Dict[str, Any]:
    errors = result.errors or []
    error_models = [{"loc": item.get("loc", "__root__"), "msg": item.get("msg", "")} for item in errors]
    fix_url = _with_root(result.fix_url) if result.fix_url else None
    save_url = _with_root(result.save_url) if result.save_url else None
    log_path = result.log_path
    return {
        "temp_id": result.temp_id,
        "status": result.status,
        "canonical_json": result.canonical_json,
        "provisional_json": result.provisional_json,
        "errors": error_models,
        "error_messages": result.error_messages,
        "warnings": result.warnings,
        "log_path": log_path,
        "fix_url": fix_url,
        "save_url": save_url,
        "canonical_path": result.canonical_path,
    }


def _resolve_student_summary_path(job_id: str, student_name: str, extension: str) -> Path:
    safe_name = _validate_student_name(student_name)
    base = _resolve_output_base()
    outputs_dir = base / job_id / "outputs"
    if extension == "txt":
        directory = outputs_dir / "print"
    elif extension == "md":
        directory = outputs_dir / "print_md"
    elif extension == "pdf":
        directory = outputs_dir / "print_pdf"
    else:
        raise HTTPException(status_code=400, detail="Unsupported summary format requested")
    path = directory / f"{safe_name}.{extension}"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Summary not available for this student")
    return path


def _validate_student_name(student_name: str) -> str:
    if not student_name:
        raise HTTPException(status_code=400, detail="Student name must not be empty")
    candidate = Path(student_name)
    if candidate.name != student_name:
        raise HTTPException(status_code=400, detail="Invalid student name")
    if candidate.name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid student name")
    return candidate.name
