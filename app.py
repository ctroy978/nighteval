"""FastAPI entrypoint for Phase 1 batch processing."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from services import EmailConfigError, EmailDeliveryService
from services.batch_runner import JobManager
from services.rubric_manager import RubricManager, RubricExtractResponse

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


class EmailPreviewStudent(BaseModel):
    student_name: str
    email: str
    section: Optional[str]
    status: str
    reason: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    attachments: List[str] = Field(default_factory=list)
    evaluation_found: bool


class EmailPreviewResponse(BaseModel):
    job_id: str
    job_name: Optional[str]
    dry_run: bool = True
    students: List[EmailPreviewStudent]
    unmatched_evaluations: List[str] = Field(default_factory=list)


class EmailSendRequest(BaseModel):
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
    total: int
    sent: int
    failed: int
    results: List[EmailSendResult]
    unmatched_evaluations: List[str] = Field(default_factory=list)


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


@app.post("/jobs/{job_id}/email/preview", response_model=EmailPreviewResponse)
async def email_preview(job_id: str) -> EmailPreviewResponse:
    job_dir, snapshot = _resolve_job_context(job_id)

    try:
        service = EmailDeliveryService(job_id=job_id, job_dir=job_dir, snapshot=snapshot)
        preparation = await run_in_threadpool(service.prepare)
    except EmailConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    students = [
        EmailPreviewStudent(
            student_name=item.student.student_name,
            email=item.student.email,
            section=item.student.section,
            status=item.status,
            reason=item.reason,
            subject=item.subject,
            body=item.body,
            attachments=item.attachment_labels(),
            evaluation_found=item.evaluation_found,
        )
        for item in preparation.prepared
    ]

    return EmailPreviewResponse(
        job_id=job_id,
        job_name=service.job_name,
        dry_run=True,
        students=students,
        unmatched_evaluations=preparation.unmatched_evaluations,
    )


@app.post("/jobs/{job_id}/email/send", response_model=EmailSendResponse)
async def email_send(job_id: str, request: EmailSendRequest) -> EmailSendResponse:
    if request.dry_run:
        raise HTTPException(
            status_code=400,
            detail="Set 'dry_run' to false to send emails or use the preview endpoint.",
        )

    job_dir, snapshot = _resolve_job_context(job_id)

    try:
        service = EmailDeliveryService(job_id=job_id, job_dir=job_dir, snapshot=snapshot)
        preparation = await run_in_threadpool(service.prepare)
    except EmailConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = await run_in_threadpool(service.send, preparation.prepared)
    report_path = await run_in_threadpool(service.write_report, rows)
    sent_count = sum(1 for row in rows if row["status"] == "sent")
    failed_count = sum(1 for row in rows if row["status"] != "sent")
    results = [EmailSendResult(**row) for row in rows]

    return EmailSendResponse(
        job_id=job_id,
        job_name=service.job_name,
        dry_run=False,
        report_path=str(report_path),
        total=len(rows),
        sent=sent_count,
        failed=failed_count,
        results=results,
        unmatched_evaluations=preparation.unmatched_evaluations,
    )


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


def _resolve_job_context(job_id: str) -> tuple[Path, Dict[str, Any]]:
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
    if status in {"running", "pending"}:
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' is not ready for email delivery (status: {status})",
        )

    if snapshot.get("validated", 0) <= 0:
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
