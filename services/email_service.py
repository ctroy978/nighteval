"""Email delivery utilities for Phase 5."""

from __future__ import annotations

import csv
import json
import os
import re
import smtplib
import ssl
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import textwrap

from jinja2 import Environment, FileSystemLoader, TemplateError

try:  # Optional dependency: YAML metadata support
    import yaml
except ImportError:  # pragma: no cover - YAML support is optional
    yaml = None


_EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)

class EmailServiceError(RuntimeError):
    """Base error for email-related failures."""


class EmailConfigError(EmailServiceError):
    """Raised when required configuration is missing or invalid."""


@dataclass(slots=True)
class SMTPConfig:
    """Runtime configuration for Brevo SMTP delivery."""

    host: str
    port: int
    use_tls: bool
    username: str
    password: str
    from_email: str
    from_name: Optional[str]
    emails_per_min: int = 20
    max_retries_per_email: int = 2


@dataclass(slots=True)
class AttachmentConfig:
    """Attachment preferences for outgoing emails."""

    attach_txt: bool = True
    attach_pdf: bool = True
    attach_json: bool = False

    def intended_labels(self) -> List[str]:
        labels: List[str] = []
        if self.attach_txt:
            labels.append("txt")
        if self.attach_pdf:
            labels.append("pdf")
        if self.attach_json:
            labels.append("json")
        return labels


@dataclass(slots=True)
class StudentRecord:
    """Entry from students.csv."""

    student_name: str
    email: str
    section: Optional[str]
    extras: Dict[str, Any]
    key: str
    raw_email: str
    email_status: str
    email_reason: Optional[str]
    row_number: int


@dataclass(slots=True)
class EvaluationRecord:
    """Validated evaluation payload and related artifacts."""

    student_name: str
    payload: Dict[str, Any]
    key: str
    attachments: Dict[str, Path]


@dataclass(slots=True)
class AttachmentDescriptor:
    """Attachment metadata used when building the message."""

    label: str
    path: Path
    maintype: str
    subtype: str


@dataclass(slots=True)
class PreparedEmail:
    """Fully prepared outbound email (ready or blocked)."""

    student: StudentRecord
    subject: Optional[str]
    body: Optional[str]
    attachments: List[AttachmentDescriptor]
    evaluation_found: bool
    status: str
    reason: Optional[str] = None
    evaluation_name: Optional[str] = None
    intended_attachments: List[str] = field(default_factory=list)
    overall: Optional[Dict[str, Any]] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    def attachment_labels(self) -> List[str]:
        return [item.label for item in self.attachments]

    def intended_labels(self) -> List[str]:
        return list(self.intended_attachments)


@dataclass(slots=True)
class PreparationResult:
    """Aggregate result used by preview and send flows."""

    prepared: List[PreparedEmail]
    unmatched_evaluations: List[str] = field(default_factory=list)
    total_students: int = 0
    attachment_config: AttachmentConfig = field(default_factory=AttachmentConfig)


class EmailTemplateRenderer:
    """Render subject and body templates for student emails."""

    def __init__(self, template_dir: Path) -> None:
        email_dir = template_dir / "email"
        if not email_dir.exists():
            raise EmailConfigError(f"Email template directory not found: {email_dir}")
        self._env = Environment(
            loader=FileSystemLoader(str(email_dir)),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self._env.filters.setdefault("wrap_lines", self._wrap_lines)
        self._env.globals.setdefault("wrap_lines", self._wrap_lines)
        self._subject_template = self._load_template("subject.txt.j2")
        self._body_template = self._load_template("body.txt.j2")

    def render_subject(self, context: Dict[str, Any]) -> str:
        rendered = self._subject_template.render(context)
        return " ".join(rendered.strip().splitlines()).strip()

    def render_body(self, context: Dict[str, Any]) -> str:
        rendered = self._body_template.render(context)
        return rendered.rstrip("\n") + "\n"

    def _load_template(self, name: str):
        try:
            return self._env.get_template(name)
        except TemplateError as exc:
            raise EmailConfigError(f"Failed to load template '{name}': {exc}") from exc

    @staticmethod
    def _wrap_lines(
        text: str,
        *,
        width: int = 80,
        max_lines: Optional[int] = None,
    ) -> List[str]:
        sanitized = (text or "").strip()
        if not sanitized:
            return [""]
        segments = [segment.strip() for segment in sanitized.splitlines() if segment.strip()]
        if not segments:
            segments = [sanitized]
        wrapped: List[str] = []
        for segment in segments:
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
        if max_lines is not None:
            wrapped = wrapped[:max_lines]
        return wrapped or [""]


class EmailDeliveryService:
    """High-level API used by FastAPI endpoints to preview or send emails."""

    REPORT_HEADERS = [
        "student_name",
        "email",
        "status",
        "attachments",
        "reason",
        "attempts",
        "timestamp",
    ]

    def __init__(self, *, job_id: str, job_dir: Path, snapshot: Dict[str, Any]) -> None:
        self.job_id = job_id
        self.job_dir = job_dir
        self.snapshot = snapshot
        self.job_name = snapshot.get("job_name") or job_id
        self.template_renderer = EmailTemplateRenderer(self._project_templates_root())
        self.metadata = self._load_job_metadata()
        self.smtp_config = self._load_smtp_config()
        self.attachment_config = self._load_attachment_config()
        self._students: Optional[List[StudentRecord]] = None
        self._evaluations: Optional[Dict[str, EvaluationRecord]] = None
        self._evaluation_duplicates: set[str] = set()

    def prepare(
        self, attachment_overrides: Optional[Dict[str, bool]] = None
    ) -> PreparationResult:
        students = self._load_students()
        evaluations = self._load_evaluations()
        config = self._resolve_attachment_config(attachment_overrides)
        prepared: List[PreparedEmail] = []

        duplicate_student_map = _find_duplicate_keys(students)

        for student in students:
            duplicates = duplicate_student_map.get(student.key, [])
            record = evaluations.get(student.key)
            evaluation_duplicate = student.key in self._evaluation_duplicates

            if student.email_status == "invalid_email":
                prepared.append(
                    PreparedEmail(
                        student=student,
                        subject=None,
                        body=None,
                        attachments=[],
                        intended_attachments=config.intended_labels(),
                        evaluation_found=bool(record),
                        status="invalid_email",
                        reason=student.email_reason,
                        evaluation_name=record.student_name if record else None,
                        overall=record.payload.get("overall") if record else None,
                        extras=student.extras,
                    )
                )
                continue

            if student.email_status == "ambiguous_email":
                prepared.append(
                    PreparedEmail(
                        student=student,
                        subject=None,
                        body=None,
                        attachments=[],
                        intended_attachments=config.intended_labels(),
                        evaluation_found=bool(record),
                        status="ambiguous_email",
                        reason=student.email_reason,
                        evaluation_name=record.student_name if record else None,
                        overall=record.payload.get("overall") if record else None,
                        extras=student.extras,
                    )
                )
                continue

            if duplicates:
                prepared.append(
                    PreparedEmail(
                        student=student,
                        subject=None,
                        body=None,
                        attachments=[],
                        intended_attachments=config.intended_labels(),
                        evaluation_found=bool(record),
                        status="ambiguous_match",
                        reason="Name appears multiple times in roster",
                        evaluation_name=record.student_name if record else None,
                        overall=record.payload.get("overall") if record else None,
                        extras=student.extras,
                    )
                )
                continue

            if evaluation_duplicate:
                prepared.append(
                    PreparedEmail(
                        student=student,
                        subject=None,
                        body=None,
                        attachments=[],
                        intended_attachments=config.intended_labels(),
                        evaluation_found=True,
                        status="ambiguous_match",
                        reason="Multiple validated evaluations share this name",
                        evaluation_name=record.student_name if record else None,
                        overall=record.payload.get("overall"),
                        extras=student.extras,
                    )
                )
                continue

            if not record:
                prepared.append(
                    PreparedEmail(
                        student=student,
                        subject=None,
                        body=None,
                        attachments=[],
                        intended_attachments=config.intended_labels(),
                        evaluation_found=False,
                        status="missing_eval",
                        reason="Validated evaluation not found",
                        extras=student.extras,
                    )
                )
                continue

            context = self._build_template_context(student, record.payload)
            try:
                subject = self.template_renderer.render_subject(context)
                body = self.template_renderer.render_body(context)
            except Exception as exc:  # pragma: no cover - template runtime errors
                prepared.append(
                    PreparedEmail(
                        student=student,
                        subject=None,
                        body=None,
                        attachments=[],
                        intended_attachments=config.intended_labels(),
                        evaluation_found=True,
                        status="template_error",
                        reason=str(exc),
                        evaluation_name=record.student_name,
                        overall=record.payload.get("overall"),
                        extras=student.extras,
                    )
                )
                continue

            attachment_result = self._collect_attachments(record, config)
            if attachment_result["missing"]:
                missing_labels = ", ".join(sorted(attachment_result["missing"]))
                prepared.append(
                    PreparedEmail(
                        student=student,
                        subject=subject,
                        body=body,
                        attachments=attachment_result["attachments"],
                        intended_attachments=attachment_result["intended"],
                        evaluation_found=True,
                        status="missing_attachment",
                        reason=f"Required attachment(s) missing: {missing_labels}",
                        evaluation_name=record.student_name,
                        overall=record.payload.get("overall"),
                        extras=student.extras,
                    )
                )
                continue

            prepared.append(
                PreparedEmail(
                    student=student,
                    subject=subject,
                    body=body,
                    attachments=attachment_result["attachments"],
                    intended_attachments=attachment_result["intended"],
                    evaluation_found=True,
                    status="ready",
                    evaluation_name=record.student_name,
                    overall=record.payload.get("overall"),
                    extras=student.extras,
                )
            )

        unmatched = [
            record.student_name
            for key, record in evaluations.items()
            if key not in {student.key for student in students}
        ]
        unmatched.sort(key=str.casefold)
        return PreparationResult(
            prepared=prepared,
            unmatched_evaluations=unmatched,
            total_students=len(students),
            attachment_config=config,
        )

    @staticmethod
    def summarize_prepared(prepared: Sequence[PreparedEmail]) -> Dict[str, int]:
        status_counts = Counter(item.status for item in prepared)
        matched = sum(1 for item in prepared if item.evaluation_found)
        total = len(prepared)
        return {
            "total": total,
            "matched": matched,
            "unmatched": max(total - matched, 0),
            "ready": status_counts.get("ready", 0),
            "missing_eval": status_counts.get("missing_eval", 0),
            "missing_attachment": status_counts.get("missing_attachment", 0),
            "template_error": status_counts.get("template_error", 0),
            "invalid_email": status_counts.get("invalid_email", 0),
            "ambiguous_match": status_counts.get("ambiguous_match", 0),
            "ambiguous_email": status_counts.get("ambiguous_email", 0),
        }

    def send(self, prepared: Sequence[PreparedEmail]) -> List[Dict[str, Any]]:
        """Send prepared emails and return rows suitable for CSV reporting."""

        rows: List[Dict[str, Any]] = []
        for item in prepared:
            timestamp = datetime.utcnow().replace(microsecond=0).isoformat()
            attachments_csv = ",".join(item.attachment_labels()) if item.attachments else ""
            if item.status != "ready" or not item.subject or not item.body:
                rows.append(
                    {
                        "student_name": item.student.student_name,
                        "email": item.student.email,
                        "status": item.status,
                        "attachments": attachments_csv,
                        "reason": item.reason or "",
                        "attempts": 0,
                        "timestamp": timestamp,
                    }
                )
                continue

            attempts = 0
            last_error: Optional[Exception] = None
            success = False
            max_attempts = max(self.smtp_config.max_retries_per_email, 0) + 1
            while attempts < max_attempts:
                attempts += 1
                try:
                    self._respect_rate_limit()
                    message = self._build_email_message(item)
                    self._send_message(message)
                    success = True
                    break
                except Exception as exc:  # pragma: no cover - SMTP runtime errors
                    last_error = exc
                    time.sleep(1)

            status = "sent" if success else "failed_smtp"
            reason = "" if success else (str(last_error) if last_error else "Unknown error")
            rows.append(
                {
                    "student_name": item.student.student_name,
                    "email": item.student.email,
                    "status": status,
                    "attachments": attachments_csv,
                    "reason": reason,
                    "attempts": attempts,
                    "timestamp": timestamp,
                }
            )
        return rows

    def write_report(self, rows: Iterable[Dict[str, Any]]) -> Path:
        target = self.job_dir / "outputs" / "email_report.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.REPORT_HEADERS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return target

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _project_templates_root(self) -> Path:
        return Path(__file__).resolve().parent.parent / "templates"

    def _load_job_metadata(self) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        candidates: List[Path] = [
            self.job_dir / "metadata.json",
            self.job_dir / "job.json",
            self.job_dir / "job_metadata.json",
            self.job_dir / "metadata" / "job.json",
        ]
        for path in candidates:
            if path.exists():
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        payload = json.load(handle)
                except Exception:  # pragma: no cover - malformed metadata
                    continue
                metadata.update(self._coerce_metadata(payload))

        if yaml is not None:
            yaml_candidates = [
                self.job_dir / "metadata.yaml",
                self.job_dir / "metadata.yml",
                self.job_dir / "job.yaml",
                self.job_dir / "job.yml",
            ]
            for path in yaml_candidates:
                if not path.exists():
                    continue
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        payload = yaml.safe_load(handle) or {}
                except Exception:  # pragma: no cover - malformed metadata
                    continue
                if isinstance(payload, dict):
                    metadata.update(self._coerce_metadata(payload))

        return metadata

    def _coerce_metadata(self, payload: Any) -> Dict[str, Any]:
        return payload if isinstance(payload, dict) else {}

    def _load_smtp_config(self) -> SMTPConfig:
        host = os.getenv("SMTP_HOST")
        port = _int_env("SMTP_PORT", 587)
        use_tls = _bool_env("SMTP_TLS", True)
        username = os.getenv("SMTP_USER")
        password = os.getenv("SMTP_PASS")
        from_email = os.getenv("FROM_EMAIL")
        from_name = os.getenv("FROM_NAME")
        emails_per_min = _int_env("EMAILS_PER_MIN", 20)
        max_retries = _int_env("MAX_RETRIES_PER_EMAIL", 2)

        if not host or not username or not password or not from_email:
            raise EmailConfigError("Missing SMTP configuration values in environment")

        if emails_per_min <= 0:
            emails_per_min = 1
        if max_retries < 0:
            max_retries = 0

        metadata_email = self.metadata.get("email")
        if isinstance(metadata_email, dict):
            emails_per_min = _int_value(metadata_email.get("emails_per_min"), emails_per_min)
            max_retries = _int_value(metadata_email.get("max_retries_per_email"), max_retries)
            override_from = metadata_email.get("from_email")
            if isinstance(override_from, str) and override_from:
                from_email = override_from
            override_name = metadata_email.get("from_name")
            if isinstance(override_name, str) and override_name:
                from_name = override_name

        config = SMTPConfig(
            host=host,
            port=port,
            use_tls=use_tls,
            username=username,
            password=password,
            from_email=from_email,
            from_name=from_name,
            emails_per_min=emails_per_min,
            max_retries_per_email=max_retries,
        )
        self._rate_interval = 60.0 / float(max(emails_per_min, 1))
        self._last_send_ts: Optional[float] = None
        return config

    def _load_attachment_config(self) -> AttachmentConfig:
        attach_txt = _bool_env("ATTACH_TXT", True)
        attach_pdf = _bool_env("ATTACH_PDF", True)
        attach_json = _bool_env("ATTACH_JSON", False)

        metadata_email = self.metadata.get("email")
        if isinstance(metadata_email, dict):
            attach_txt = _bool_value(metadata_email.get("attach_txt"), attach_txt)
            attach_pdf = _bool_value(metadata_email.get("attach_pdf"), attach_pdf)
            attach_json = _bool_value(metadata_email.get("attach_json"), attach_json)

        return AttachmentConfig(
            attach_txt=attach_txt,
            attach_pdf=attach_pdf,
            attach_json=attach_json,
        )

    def _load_students(self) -> List[StudentRecord]:
        if self._students is not None:
            return self._students

        csv_path = self._resolve_students_csv()
        if not csv_path.exists():
            raise EmailConfigError(f"students.csv not found for job: {csv_path}")

        records: List[StudentRecord] = []
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise EmailConfigError("students.csv has no header row")
            normalized_headers = {name.lower().strip(): name for name in reader.fieldnames}
            name_field = normalized_headers.get("student_name")
            email_field = normalized_headers.get("email")
            section_field = normalized_headers.get("section")
            if not name_field or not email_field:
                raise EmailConfigError("students.csv must include 'student_name' and 'email' columns")

            for index, row in enumerate(reader, start=2):
                student_name = (row.get(name_field) or "").strip()
                raw_email = (row.get(email_field) or "").strip()
                section = (row.get(section_field) or "").strip() if section_field else None
                if not student_name or not raw_email:
                    continue

                extras = {}
                for header, value in row.items():
                    if header is None:
                        continue
                    header_key = header.strip()
                    if header_key in {name_field, email_field}:
                        continue
                    if section_field and header_key == section_field:
                        continue
                    extras[header_key] = value.strip() if isinstance(value, str) else value

                email, email_status, email_reason = _parse_email_cell(raw_email)
                normalized_section = section if section else None
                student_key = _normalize_name(student_name)
                records.append(
                    StudentRecord(
                        student_name=student_name,
                        email=email,
                        section=normalized_section,
                        extras=extras,
                        key=student_key,
                        raw_email=raw_email,
                        email_status=email_status,
                        email_reason=email_reason,
                        row_number=index,
                    )
                )

        if not records:
            raise EmailConfigError("students.csv does not contain any valid rows")

        self._students = records
        return records

    def _resolve_students_csv(self) -> Path:
        candidates = [
            self.job_dir / "inputs" / "students.csv",
            self.job_dir / "students.csv",
            self.job_dir / "metadata" / "students.csv",
        ]
        for path in candidates:
            if path.exists():
                return path
        return candidates[0]

    def _load_evaluations(self) -> Dict[str, EvaluationRecord]:
        if self._evaluations is not None:
            return self._evaluations

        json_dir = self.job_dir / "outputs" / "json"
        if not json_dir.exists():
            raise EmailConfigError("Validated evaluations directory not found")

        evaluations: Dict[str, EvaluationRecord] = {}
        self._evaluation_duplicates = set()
        for json_path in sorted(json_dir.glob("*.json")):
            try:
                with json_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except Exception:  # pragma: no cover - IO or JSON problems
                continue
            if not isinstance(payload, dict):
                continue
            required_keys = {"criteria", "overall_score", "summary"}
            if not all(key in payload for key in required_keys):
                continue
            student_name = json_path.stem
            key = _normalize_name(student_name)
            if key in evaluations:
                self._evaluation_duplicates.add(key)
                continue
            attachments = {
                "json": json_path,
                "txt": json_path.parent.parent / "print" / f"{student_name}.txt",
                "pdf": json_path.parent.parent / "print_pdf" / f"{student_name}.pdf",
            }
            evaluations[key] = EvaluationRecord(
                student_name=student_name,
                payload=payload,
                key=key,
                attachments=attachments,
            )

        self._evaluations = evaluations
        return evaluations

    def _build_template_context(self, student: StudentRecord, evaluation: Dict[str, Any]) -> Dict[str, Any]:
        context: Dict[str, Any] = {
            "student_name": student.student_name,
            "student_email": student.email,
            "section": student.section,
            "job_name": self.job_name,
            "from_name": self.smtp_config.from_name,
            "from_email": self.smtp_config.from_email,
            "eval": evaluation,
            "job_id": self.job_id,
            "metadata": self.metadata,
        }

        for key, value in self.metadata.items():
            context.setdefault(key, value)

        if student.section is not None:
            context.setdefault("section", student.section)
        for key, value in student.extras.items():
            context.setdefault(key, value)

        return context

    def _resolve_attachment_config(
        self, overrides: Optional[Dict[str, bool]]
    ) -> AttachmentConfig:
        if not overrides:
            return AttachmentConfig(
                attach_txt=self.attachment_config.attach_txt,
                attach_pdf=self.attachment_config.attach_pdf,
                attach_json=self.attachment_config.attach_json,
            )

        config = AttachmentConfig(
            attach_txt=self.attachment_config.attach_txt,
            attach_pdf=self.attachment_config.attach_pdf,
            attach_json=self.attachment_config.attach_json,
        )
        for key, value in overrides.items():
            if value is None:
                continue
            if key == "attach_txt":
                config.attach_txt = bool(value)
            elif key == "attach_pdf":
                config.attach_pdf = bool(value)
            elif key == "attach_json":
                config.attach_json = bool(value)
        return config

    def _collect_attachments(
        self, record: EvaluationRecord, config: AttachmentConfig
    ) -> Dict[str, Any]:
        attachments: List[AttachmentDescriptor] = []
        missing: List[str] = []

        if config.attach_txt:
            txt_path = record.attachments.get("txt")
            if txt_path and txt_path.exists():
                attachments.append(
                    AttachmentDescriptor(
                        label="txt",
                        path=txt_path,
                        maintype="text",
                        subtype="plain",
                    )
                )
            else:
                missing.append("txt")

        if config.attach_pdf:
            pdf_path = record.attachments.get("pdf")
            if pdf_path and pdf_path.exists():
                attachments.append(
                    AttachmentDescriptor(
                        label="pdf",
                        path=pdf_path,
                        maintype="application",
                        subtype="pdf",
                    )
                )
            else:
                missing.append("pdf")

        if config.attach_json:
            json_path = record.attachments.get("json")
            if json_path and json_path.exists():
                attachments.append(
                    AttachmentDescriptor(
                        label="json",
                        path=json_path,
                        maintype="application",
                        subtype="json",
                    )
                )
            else:
                missing.append("json")

        return {
            "attachments": attachments,
            "missing": missing,
            "intended": config.intended_labels(),
        }

    def _build_email_message(self, item: PreparedEmail) -> EmailMessage:
        assert item.subject is not None and item.body is not None
        message = EmailMessage()
        message["Subject"] = item.subject
        if self.smtp_config.from_name:
            message["From"] = formataddr((self.smtp_config.from_name, self.smtp_config.from_email))
        else:
            message["From"] = self.smtp_config.from_email
        message["To"] = item.student.email
        if item.student.section:
            message["X-Student-Section"] = item.student.section
        message.set_content(item.body, subtype="plain", charset="utf-8")

        for attachment in item.attachments:
            data = attachment.path.read_bytes()
            filename = attachment.path.name
            message.add_attachment(
                data,
                maintype=attachment.maintype,
                subtype=attachment.subtype,
                filename=filename,
            )

        return message

    def _send_message(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.smtp_config.host, self.smtp_config.port, timeout=30) as client:
            if self.smtp_config.use_tls:
                context = ssl.create_default_context()
                client.starttls(context=context)
            client.login(self.smtp_config.username, self.smtp_config.password)
            client.send_message(message)
        self._last_send_ts = time.monotonic()

    def _respect_rate_limit(self) -> None:
        now = time.monotonic()
        last = getattr(self, "_last_send_ts", None)
        if last is None:
            return
        sleep_for = self._rate_interval - (now - last)
        if sleep_for > 0:
            time.sleep(sleep_for)


def _parse_email_cell(value: str) -> Tuple[str, str, Optional[str]]:
    normalized = (value or "").strip()
    if not normalized:
        return "", "invalid_email", "Email address is required"

    parts = [part.strip() for part in re.split(r"[;,]", normalized) if part.strip()]
    ambiguous = False
    if parts:
        email_candidate = parts[0]
        if len(parts) > 1:
            ambiguous = True
    else:
        email_candidate = normalized

    if " " in email_candidate:
        # Handle space separated addresses
        pieces = [piece for piece in email_candidate.split() if piece.strip()]
        if pieces:
            email_candidate = pieces[0].strip()
            if len(pieces) > 1:
                ambiguous = True

    status = "ok"
    reason: Optional[str] = None
    if ambiguous:
        status = "ambiguous_email"
        reason = "Multiple emails provided; using the first entry"

    if not _EMAIL_RE.fullmatch(email_candidate):
        status = "invalid_email"
        reason = "Invalid email format"

    return email_candidate, status, reason


def _find_duplicate_keys(students: Sequence[StudentRecord]) -> Dict[str, List[StudentRecord]]:
    buckets: Dict[str, List[StudentRecord]] = defaultdict(list)
    duplicates: Dict[str, List[StudentRecord]] = {}
    for record in students:
        buckets[record.key].append(record)
    for key, bucket in buckets.items():
        if len(bucket) > 1:
            duplicates[key] = bucket
    return duplicates


def _normalize_name(value: str) -> str:
    collapsed = " ".join(value.split())
    return collapsed.casefold()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return _parse_bool(value, default)


def _bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _parse_bool(value, default)
    return default


def _parse_bool(value: str, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return _int_value(value, default)


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
