"""Utilities for extracting text from PDF essays."""

from dataclasses import dataclass
from pathlib import Path

from PyPDF2 import PdfReader


class PDFExtractionError(Exception):
    """Raised when PDF text cannot be extracted."""


@dataclass
class PDFTextExtraction:
    """Container for extracted PDF text and basic metadata."""

    text: str
    page_count: int


def extract_text_with_metadata(pdf_path: str) -> PDFTextExtraction:
    """Return extracted text and page count for the provided PDF file."""

    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # pragma: no cover - defensive for corrupted files
        raise PDFExtractionError(f"Failed to open PDF: {pdf_path}") from exc

    chunks: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pragma: no cover - extraction edge cases
            raise PDFExtractionError(f"Failed to extract text from {pdf_path}") from exc
        chunks.append(text.strip())

    content = "\n\n".join(chunk for chunk in chunks if chunk)
    return PDFTextExtraction(text=content, page_count=len(reader.pages))


def extract_text(pdf_path: str) -> str:
    """Backward-compatible helper that returns text only."""

    return extract_text_with_metadata(pdf_path).text
