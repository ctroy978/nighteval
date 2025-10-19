"""Batch job runner for Phase 1 processing."""

from __future__ import annotations

import csv
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

from utils import ai_client, io_utils, pdf_tools, validation


@dataclass
class JobState:
    """Mutable snapshot of a running or completed batch job."""

    job_id: str
    job_dir: Path
    total: int
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    status: str = "running"
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: Optional[str] = None
    error: Optional[str] = None
    artifacts: Dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> Dict[str, Any]:
        """Return an immutable view appropriate for API responses."""

        with self.lock:
            data: Dict[str, Any] = {
                "job_id": self.job_id,
                "status": self.status,
                "total": self.total,
                "processed": self.processed,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "artifacts": self.artifacts.copy(),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
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

        state = JobState(job_id=job_id, job_dir=job_dir, total=len(pdf_paths))

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
    logs_dir = state.job_dir / "logs"

    essays_dir.mkdir(parents=True, exist_ok=True)
    outputs_json_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    try:
        rubric = io_utils.read_json_file(str(rubric_path))
        shutil.copy2(rubric_path, inputs_dir / "rubric.json")
    except Exception as exc:  # pragma: no cover - IO edge cases
        _finalise_state(state, "failed", error=str(exc))
        return

    copied_paths = _copy_essays(pdf_paths, essays_dir)
    _write_state_snapshot(state)

    summary_builder = _SummaryBuilder(rubric)
    job_log_path = logs_dir / "job.log"
    results_log_path = logs_dir / "results.jsonl"

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

            try:
                essay_text = pdf_tools.extract_text(str(essay_path))
                response = ai_client.evaluate_essay(essay_text=essay_text, rubric=rubric)
                attempts = response.attempts
                raw_response = response.raw_text
                usage = response.usage
                payload = response.content
                validation.validate_evaluation_payload(payload, rubric)
                _write_student_json(outputs_json_dir, student_name, payload)
                summary_builder.add_success(student_name, payload)
            except (FileNotFoundError, pdf_tools.PDFExtractionError) as exc:
                status = "failed"
                error = str(exc)
                summary_builder.add_failure(student_name)
                _write_failure_json(outputs_json_dir, student_name, error)
            except ai_client.AIClientError as exc:
                status = "failed"
                error = str(exc)
                summary_builder.add_failure(student_name)
                _write_failure_json(outputs_json_dir, student_name, error, raw_response)
            except validation.EvaluationValidationError as exc:
                status = "invalid"
                error = str(exc)
                summary_builder.add_failure(student_name)
                _write_failure_json(outputs_json_dir, student_name, error, raw_response)
            except Exception as exc:  # pragma: no cover - defensive
                status = "failed"
                error = str(exc)
                summary_builder.add_failure(student_name)
                _write_failure_json(outputs_json_dir, student_name, error, raw_response)

            duration_ms = int((time.perf_counter() - start_time) * 1000)
            retries = max(attempts - 1, 0)
            _append_job_log(job_log, student_name, status, duration_ms, retries)
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
                essay_source=str(source_folder / f"{student_name}.pdf"),
            )

            _update_counters(state, status)
            _write_state_snapshot(state)

    try:
        summary_path = outputs_dir / "summary.csv"
        _write_summary_csv(summary_path, summary_builder)
        zip_path = outputs_dir / "evaluations.zip"
        _write_zip_archive(zip_path, outputs_json_dir)
        with state.lock:
            state.artifacts["csv"] = str(summary_path)
            state.artifacts["zip"] = str(zip_path)
    except Exception as exc:  # pragma: no cover - disk issues
        _finalise_state(state, "failed", error=str(exc))
        return

    _finalise_state(state, "completed")


def _append_job_log(handle, student: str, status: str, ms: int, retries: int) -> None:
    timestamp = datetime.utcnow().isoformat()
    handle.write(f"{timestamp} | {student} | {status} | {ms} | {retries}\n")
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
    essay_source: str,
) -> None:
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "student_name": student,
        "status": status,
        "duration_ms": duration_ms,
        "attempts": attempts,
        "error": error,
        "essay_source": essay_source,
    }
    if usage:
        entry["usage"] = usage
    if payload:
        entry["evaluation"] = payload
    if raw_response:
        entry["raw"] = raw_response

    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    handle.flush()


def _write_state_snapshot(state: JobState) -> None:
    snapshot = state.snapshot()
    state_path = state.job_dir / "logs" / "state.json"
    io_utils.write_json(state_path, snapshot)


def _update_counters(state: JobState, status: str) -> None:
    with state.lock:
        state.processed += 1
        if status == "success":
            state.succeeded += 1
        else:
            state.failed += 1


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
) -> None:
    target_path = outputs_dir / f"{student_name}.json"
    failure_payload = {"status": "error", "error": error}
    if raw_response:
        failure_payload["raw_response"] = raw_response
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


def _write_zip_archive(target: Path, json_dir: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for json_file in sorted(json_dir.glob("*.json")):
            archive.write(json_file, arcname=json_file.name)


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

    def __init__(self, rubric: Dict[str, Any]) -> None:
        criteria = rubric.get("criteria", []) if isinstance(rubric, dict) else []
        self.criteria_order: List[str] = [str(item.get("id")) for item in criteria if item.get("id")]
        self.max_scores = {
            str(item.get("id")): float(item.get("max_score", 0))
            for item in criteria
            if item.get("id") is not None
        }
        self.overall_possible = sum(self.max_scores.values())
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
