"""FastAPI entrypoint for Phase 1 batch processing."""

from __future__ import annotations

import csv
import io
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote_plus

from datetime import datetime

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from services import EmailConfigError, EmailDeliveryService
from services.batch_runner import JobManager
from services.rubric_manager import RubricManager, RubricExtractResponse
from utils import io_utils

load_dotenv()


def _resolve_output_base() -> Path:
    candidate = os.getenv("OUTPUT_BASE") or os.getenv("APP_BASE_DIR")
    if candidate:
        return Path(candidate).expanduser()
    return Path("/data/sessions")


job_manager = JobManager(output_base=_resolve_output_base())
rubric_manager = RubricManager(base_dir=_resolve_output_base())

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

app = FastAPI(title="Batch Essay Evaluator", version="1.1.0")


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
        state = job_manager.start_job(
            essays_folder=Path(request.essays_folder),
            rubric_path=Path(request.rubric_path),
            job_name=request.job_name,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    snapshot = state.snapshot()
    return {
        "job_id": snapshot["job_id"],
        "status": snapshot["status"],
        "total": snapshot["total"],
        "processed": snapshot["processed"],
    }


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request) -> HTMLResponse:
    entries = _list_jobs(limit=40)
    return templates.TemplateResponse(
        "jobs.html",
        {
            "request": request,
            "jobs": entries,
        },
    )


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def job_status(job_id: str) -> Dict[str, Any]:
    state = job_manager.get_job(job_id)
    if state:
        snapshot = state.snapshot()
    else:
        snapshot = _load_snapshot_from_disk(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    return _format_status_response(snapshot)


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
    return templates.TemplateResponse("email_console.html", context)


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
            url=f"/jobs/{job_id}/email?error={quote_plus('CSV must be UTF-8 encoded')}",
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/jobs/{job_id}/email?error={quote_plus(str(exc))}",
            status_code=303,
        )

    target = job_dir / "inputs" / "students.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(decoded, encoding="utf-8")

    return RedirectResponse(url=f"/jobs/{job_id}/email?uploaded=1", status_code=303)


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
        report_url=f"/jobs/{job_id}/email/report",
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
        "save_url": f"/rubrics/{temp_id}/save",
        "validate_url": f"/rubrics/{temp_id}/save?validate_only=1",
        "download_url": f"/rubrics/{temp_id}/download",
    }
    return templates.TemplateResponse("rubric_fix.html", context)


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
    return templates.TemplateResponse("rubric_preview.html", context)


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
    }


def _serialize_rubric_extract(result: RubricExtractResponse) -> Dict[str, Any]:
    errors = result.errors or []
    error_models = [{"loc": item.get("loc", "__root__"), "msg": item.get("msg", "")} for item in errors]
    return {
        "temp_id": result.temp_id,
        "status": result.status,
        "canonical_json": result.canonical_json,
        "provisional_json": result.provisional_json,
        "errors": error_models,
        "error_messages": result.error_messages,
        "warnings": result.warnings,
        "log_path": result.log_path,
        "fix_url": result.fix_url,
        "save_url": result.save_url,
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
