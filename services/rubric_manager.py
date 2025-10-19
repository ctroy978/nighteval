"""Rubric extraction and canonicalisation workflow for Phase 3."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from utils import ai_client, io_utils, pdf_tools
from utils.rubric_normalization import (
    CanonicalizationConfig,
    CanonicalizationResult,
    canonicalize_rubric,
)


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


@dataclass
class RubricExtractionConfig:
    """Environment-driven runtime settings."""

    enabled: bool = True
    max_pages: int = 10
    max_chars: int = 40000
    retry: int = 1
    require_totals_equal: bool = True
    id_max_length: int = 40

    @classmethod
    def load(cls) -> "RubricExtractionConfig":
        return cls(
            enabled=_bool_env("RUBRIC_EXTRACTION_ENABLED", True),
            max_pages=max(_int_env("RUBRIC_MAX_PAGES", 10), 0),
            max_chars=max(_int_env("RUBRIC_MAX_CHARS", 40000), 0),
            retry=max(_int_env("RUBRIC_RETRY", 1), 0),
            require_totals_equal=_bool_env("RUBRIC_REQUIRE_TOTALS_EQUAL", True),
            id_max_length=max(_int_env("RUBRIC_ID_MAXLEN", 40), 1),
        )


@dataclass
class RubricSession:
    """Information captured for an in-flight rubric extraction."""

    temp_id: str
    job_name: Optional[str]
    base_dir: Path
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    status: Literal["pending", "valid", "needs_fix", "failed"] = "pending"
    canonical: Optional[Dict[str, Any]] = None
    provisional: Optional[Dict[str, Any]] = None
    errors: List[Dict[str, str]] = field(default_factory=list)
    error_messages: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    canonical_path: Optional[Path] = None
    version_hash: Optional[str] = None
    source_path: Optional[Path] = None

    def inputs_dir(self) -> Path:
        return self.base_dir / "inputs"

    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    def log_path(self) -> Path:
        return self.logs_dir() / "rubric_extract.log"


@dataclass
class RubricExtractResponse:
    temp_id: str
    status: Literal["valid", "needs_fix", "failed"]
    canonical_json: Optional[Dict[str, Any]]
    provisional_json: Optional[Dict[str, Any]]
    errors: List[Dict[str, str]]
    error_messages: List[str]
    warnings: List[str]
    log_path: str
    fix_url: Optional[str]
    save_url: Optional[str]
    canonical_path: Optional[str]


class RubricManager:
    """Coordinates rubric extraction and persistence."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.config = RubricExtractionConfig.load()
        self._sessions: Dict[str, RubricSession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        *,
        filename: str,
        content: bytes,
        job_name: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> RubricExtractResponse:
        if not self.config.enabled:
            raise RuntimeError("Rubric extraction is disabled via configuration")

        temp_id = self._create_temp_id(job_name)
        session = self._create_session(temp_id=temp_id, job_name=job_name)
        logger = _RubricLogger(session.log_path())
        logger.log("rubric_upload_received", extra={"filename": filename})

        suffix = Path(filename or "rubric").suffix.lower()
        inferred_json = (content_type or "").lower().startswith("application/json")
        inferred_pdf = (content_type or "").lower().endswith("pdf")

        if suffix == ".json" or inferred_json:
            return self._handle_json(session, content, logger)
        if suffix == ".pdf" or inferred_pdf:
            return self._handle_pdf(session, content, logger)
        else:
            logger.log(
                "rubric_unsupported_format",
                extra={"filename": filename, "suffix": suffix or ""},
            )
            session.status = "failed"
            self._store_session(session)
            return self._response_for_session(session)

    def get_session(self, temp_id: str) -> Optional[RubricSession]:
        with self._lock:
            return self._sessions.get(temp_id)

    def validate_and_save(
        self,
        temp_id: str,
        payload: Any,
        *,
        validate_only: bool = False,
    ) -> Tuple[RubricSession, CanonicalizationResult]:
        session = self.get_session(temp_id)
        if not session:
            raise FileNotFoundError(f"Rubric session '{temp_id}' not found")

        normalization = canonicalize_rubric(
            payload,
            config=CanonicalizationConfig(
                id_max_length=self.config.id_max_length,
                require_totals_equal=self.config.require_totals_equal,
            ),
        )

        logger = _RubricLogger(session.log_path())
        if normalization.is_valid and not validate_only:
            self._persist_canonical(session, normalization.canonical or {})
            session.status = "valid"
            session.errors = []
            session.error_messages = []
            session.warnings = normalization.warnings
            logger.log("rubric_save_success", extra={"temp_id": temp_id})
        else:
            if normalization.is_valid and session.canonical_path:
                session.status = "valid"
            else:
                session.status = "needs_fix"
            session.errors = normalization.errors
            session.error_messages = normalization.error_messages
            session.warnings = normalization.warnings
            if normalization.is_valid:
                logger.log(
                    "rubric_validation_ok",
                    extra={"temp_id": temp_id, "validate_only": validate_only},
                )
            else:
                logger.log(
                    "rubric_validation_failed",
                    extra={"errors": normalization.error_messages},
                )

        with self._lock:
            self._sessions[temp_id] = session

        return session, normalization

    def record_manual_payload(self, temp_id: str, payload: Any) -> None:
        session = self.get_session(temp_id)
        if not session:
            raise FileNotFoundError(f"Rubric session '{temp_id}' not found")
        session.provisional = payload if isinstance(payload, dict) else None
        with self._lock:
            self._sessions[temp_id] = session

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_json(
        self, session: RubricSession, content: bytes, logger: "_RubricLogger"
    ) -> RubricExtractResponse:
        try:
            data = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError as exc:
            logger.log("rubric_json_decode_error", extra={"msg": exc.msg})
            session.status = "failed"
            session.errors = [{"loc": "__root__", "msg": exc.msg}]
            session.error_messages = [f"Invalid JSON: {exc.msg}"]
            self._store_session(session)
            return self._response_for_session(session)

        session.inputs_dir().mkdir(parents=True, exist_ok=True)
        session.logs_dir().mkdir(parents=True, exist_ok=True)
        provisional_path = session.inputs_dir() / "rubric_provisional.json"
        io_utils.write_json(provisional_path, data)
        logger.log("rubric_json_uploaded", extra={"path": str(provisional_path)})

        normalization = canonicalize_rubric(
            data,
            config=CanonicalizationConfig(
                id_max_length=self.config.id_max_length,
                require_totals_equal=self.config.require_totals_equal,
            ),
        )

        session.provisional = normalization.normalized
        session.errors = normalization.errors
        session.error_messages = normalization.error_messages
        session.warnings = normalization.warnings

        if normalization.is_valid and normalization.canonical:
            self._persist_canonical(session, normalization.canonical)
            session.status = "valid"
            logger.log("rubric_json_valid", extra={"temp_id": session.temp_id})
        else:
            session.status = "needs_fix"
            logger.log(
                "rubric_json_needs_fix",
                extra={"errors": normalization.error_messages},
            )

        with self._lock:
            self._sessions[session.temp_id] = session
        return self._response_for_session(session)

    def _handle_pdf(
        self, session: RubricSession, content: bytes, logger: "_RubricLogger"
    ) -> RubricExtractResponse:
        source_path = session.inputs_dir() / "rubric_source.pdf"
        session.inputs_dir().mkdir(parents=True, exist_ok=True)
        session.logs_dir().mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(content)
        session.source_path = source_path
        logger.log("rubric_pdf_saved", extra={"path": str(source_path)})

        try:
            extraction = pdf_tools.extract_text_with_metadata(
                str(source_path),
                max_pages=self.config.max_pages,
                max_chars=self.config.max_chars,
            )
        except (FileNotFoundError, pdf_tools.PDFExtractionError) as exc:
            session.status = "failed"
            session.error_messages = [str(exc)]
            session.errors = [{"loc": "__root__", "msg": str(exc)}]
            logger.log("rubric_pdf_extract_failed", extra={"error": str(exc)})
            self._store_session(session)
            return self._response_for_session(session)

        logger.log(
            "rubric_pdf_text_extracted",
            extra={
                "page_count": extraction.page_count,
                "chars": len(extraction.text or ""),
            },
        )

        if not extraction.text.strip():
            message = "No selectable text found in rubric PDF"
            session.status = "failed"
            session.error_messages = [message]
            session.errors = [{"loc": "__root__", "msg": message}]
            logger.log("rubric_pdf_no_text")
            self._store_session(session)
            return self._response_for_session(session)

        ai_result = ai_client.extract_rubric_json(
            extraction.text, retry_attempts=self.config.retry
        )
        logger.log(
            "rubric_ai_attempt",
            extra={"attempts": ai_result.attempts, "status": ai_result.status},
        )

        provisional_payload = ai_result.payload
        if provisional_payload is None and ai_result.raw_text:
            try:
                provisional_payload = json.loads(ai_result.raw_text)
            except json.JSONDecodeError:
                provisional_payload = None

        if provisional_payload is not None:
            session.provisional = provisional_payload
            provisional_path = session.inputs_dir() / "rubric_provisional.json"
            io_utils.write_json(provisional_path, provisional_payload)
            logger.log(
                "rubric_provisional_written", extra={"path": str(provisional_path)}
            )
        else:
            session.provisional = None

        normalization = canonicalize_rubric(
            provisional_payload or {},
            config=CanonicalizationConfig(
                id_max_length=self.config.id_max_length,
                require_totals_equal=self.config.require_totals_equal,
            ),
        )

        session.errors = normalization.errors
        session.error_messages = normalization.error_messages
        session.warnings = normalization.warnings

        if normalization.is_valid and normalization.canonical:
            self._persist_canonical(session, normalization.canonical)
            session.status = "valid"
            logger.log("rubric_pdf_valid", extra={"temp_id": session.temp_id})
        else:
            session.status = "needs_fix"
            if ai_result.errors:
                logger.log("rubric_ai_errors", extra={"errors": ai_result.errors})
            logger.log(
                "rubric_pdf_needs_fix",
                extra={"errors": normalization.error_messages},
            )

        self._store_session(session)
        return self._response_for_session(session)

    def _persist_canonical(self, session: RubricSession, payload: Dict[str, Any]) -> None:
        session.inputs_dir().mkdir(parents=True, exist_ok=True)
        session.logs_dir().mkdir(parents=True, exist_ok=True)
        target = session.inputs_dir() / "rubric.json"
        io_utils.write_json(target, payload)
        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        session.canonical = payload
        session.canonical_path = target
        session.version_hash = sha256(payload_bytes).hexdigest()

    def _response_for_session(self, session: RubricSession) -> RubricExtractResponse:
        status: Literal["valid", "needs_fix", "failed"]
        if session.status == "valid":
            status = "valid"
        elif session.status == "failed":
            status = "failed"
        else:
            status = "needs_fix"

        return RubricExtractResponse(
            temp_id=session.temp_id,
            status=status,
            canonical_json=session.canonical,
            provisional_json=session.provisional,
            errors=session.errors,
            error_messages=session.error_messages,
            warnings=session.warnings,
            log_path=str(session.log_path()),
            fix_url=f"/rubrics/{session.temp_id}/fix" if status != "failed" else None,
            save_url=f"/rubrics/{session.temp_id}/save",
            canonical_path=str(session.canonical_path) if session.canonical_path else None,
        )

    def _store_session(self, session: RubricSession) -> None:
        with self._lock:
            self._sessions[session.temp_id] = session

    def _create_session(self, temp_id: str, job_name: Optional[str]) -> RubricSession:
        base_dir = self.base_dir / temp_id
        base_dir.mkdir(parents=True, exist_ok=True)
        return RubricSession(temp_id=temp_id, job_name=job_name, base_dir=base_dir)

    def _create_temp_id(self, job_name: Optional[str]) -> str:
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        slug = _slugify(job_name) if job_name else None
        key = f"rubric-{timestamp}-{slug}" if slug else f"rubric-{timestamp}"
        with self._lock:
            counter = 1
            candidate = key
            while candidate in self._sessions or (self.base_dir / candidate).exists():
                counter += 1
                candidate = f"{key}-{counter}"
            return candidate


def _slugify(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    trimmed = name.strip()
    if not trimmed:
        return None
    slug = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in trimmed)
    slug = slug.strip("-_")
    return slug or None


class _RubricLogger:
    """Structured logger that writes to rubric_extract.log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        timestamp = datetime.utcnow().isoformat()
        payload = {"event": event}
        if extra:
            payload.update(extra)
        line = json.dumps({"timestamp": timestamp, **payload}, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
