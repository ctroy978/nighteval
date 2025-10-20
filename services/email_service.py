"""Email delivery utilities for Phase 5."""

from __future__ import annotations

import csv
import json
import os
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from jinja2 import Environment, FileSystemLoader, TemplateError

try:  # Optional dependency: YAML metadata support
    import yaml
except ImportError:  # pragma: no cover - YAML support is optional
    yaml = None


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


@dataclass(slots=True)
class StudentRecord:
    """Entry from students.csv."""

    student_name: str
    email: str
    section: Optional[str]
    key: str


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

    def attachment_labels(self) -> List[str]:
        return [item.label for item in self.attachments]


@dataclass(slots=True)
class PreparationResult:
    """Aggregate result used by preview and send flows."""

    prepared: List[PreparedEmail]
    unmatched_evaluations: List[str] = field(default_factory=list)


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

    def prepare(self) -> PreparationResult:
        students = self._load_students()
        evaluations = self._load_evaluations()
        prepared: List[PreparedEmail] = []

        for student in students:
            record = evaluations.get(student.key)
            if not record:
                prepared.append(
                    PreparedEmail(
                        student=student,
                        subject=None,
                        body=None,
                        attachments=[],
                        evaluation_found=False,
                        status="missing_eval",
                        reason="Validated evaluation not found",
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
                        evaluation_found=True,
                        status="template_error",
                        reason=str(exc),
                        evaluation_name=record.student_name,
                    )
                )
                continue

            attachment_result = self._collect_attachments(record)
            if attachment_result["missing"]:
                missing_labels = ", ".join(sorted(attachment_result["missing"]))
                prepared.append(
                    PreparedEmail(
                        student=student,
                        subject=subject,
                        body=body,
                        attachments=attachment_result["attachments"],
                        evaluation_found=True,
                        status="missing_attachment",
                        reason=f"Required attachment(s) missing: {missing_labels}",
                        evaluation_name=record.student_name,
                    )
                )
                continue

            prepared.append(
                PreparedEmail(
                    student=student,
                    subject=subject,
                    body=body,
                    attachments=attachment_result["attachments"],
                    evaluation_found=True,
                    status="ready",
                    evaluation_name=record.student_name,
                )
            )

        unmatched = [
            record.student_name
            for key, record in evaluations.items()
            if key not in {student.key for student in students}
        ]
        unmatched.sort(key=str.casefold)
        return PreparationResult(prepared=prepared, unmatched_evaluations=unmatched)

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

            for row in reader:
                student_name = (row.get(name_field) or "").strip()
                email = (row.get(email_field) or "").strip()
                section = (row.get(section_field) or "").strip() if section_field else None
                if not student_name or not email:
                    continue
                records.append(
                    StudentRecord(
                        student_name=student_name,
                        email=email,
                        section=section if section else None,
                        key=_normalize_name(student_name),
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
        for json_path in sorted(json_dir.glob("*.json")):
            try:
                with json_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except Exception:  # pragma: no cover - IO or JSON problems
                continue
            if not isinstance(payload, dict):
                continue
            if "overall" not in payload or "criteria" not in payload:
                continue
            student_name = json_path.stem
            key = _normalize_name(student_name)
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

        return context

    def _collect_attachments(self, record: EvaluationRecord) -> Dict[str, Any]:
        attachments: List[AttachmentDescriptor] = []
        missing: List[str] = []
        config = self.attachment_config

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

        return {"attachments": attachments, "missing": missing}

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

