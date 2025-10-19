"""FastAPI entrypoint for Phase 0 single-essay evaluation."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

load_dotenv()

from utils import ai_client, io_utils, pdf_tools  # noqa: E402 (after load_dotenv)


class EvaluateRequest(BaseModel):
    essay_path: str = Field(..., description="Filesystem path to the student essay PDF")
    rubric_path: str = Field(..., description="Filesystem path to the rubric JSON")


app = FastAPI(title="Batch Essay Evaluator", version="0.1.0")


@app.post("/evaluate")
async def evaluate(request: EvaluateRequest) -> Dict[str, Any]:
    base_dir = Path(os.getenv("APP_BASE_DIR", "/data/sessions"))
    try:
        session_dir = _create_session_dir(base_dir)
    except PermissionError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Cannot create session directory under '{base_dir}'. "
                "Set APP_BASE_DIR to a writable location."
            ),
        ) from exc

    try:
        essay_text = pdf_tools.extract_text(request.essay_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except pdf_tools.PDFExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        rubric_json = io_utils.read_json_file(request.rubric_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid rubric JSON: {exc}") from exc

    io_utils.write_text(session_dir / "essay.txt", essay_text)
    io_utils.write_json(session_dir / "rubric.json", rubric_json)

    try:
        evaluation = ai_client.evaluate_essay(essay_text=essay_text, rubric=rubric_json)
    except ai_client.AIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    io_utils.write_json(session_dir / "evaluation.json", evaluation)
    return evaluation


def _create_session_dir(base_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir = base_dir / timestamp
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir
