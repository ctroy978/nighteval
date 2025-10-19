# Batch Essay Evaluator — Phase 0

This prototype processes a single essay PDF with a rubric, calls an OpenAI-compatible model (defaulting to `grok-4-fast-reasoning`), and saves the evaluation artifacts locally.

---

## Prerequisites

- Python 3.10+
- Virtual environment (recommended)
- `.env` file based on `.env.example` (set `XAI_API_KEY`, `XAI_API_BASE`, `XAI_MODEL`)

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Running the API

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Artifacts are stored under `/data/sessions/<timestamp>/` by default. If `/data` is not writable on your machine, set `APP_BASE_DIR` to another directory (for example `APP_BASE_DIR=./data/sessions`).

---

## Sample Request

```bash
curl -X POST http://localhost:8000/evaluate \
  -H "Content-Type: application/json" \
  -d '{
        "essay_path": "/home/pi/essays/jane_doe.pdf",
        "rubric_path": "/home/pi/rubrics/english_rubric.json"
      }'
```

### Sample Response

```json
{
  "overall": {
    "points_earned": 14,
    "points_possible": 20
  },
  "criteria": [
    {
      "id": "THESIS",
      "score": 3,
      "evidence": {
        "quote": "The introduction establishes the central claim..."
      },
      "explanation": "Clear thesis with relevant context but limited nuance.",
      "advice": "Deepen the thesis by clarifying scope and stakes."
    }
  ]
}
```

---

## Definition of Done

- Extracts essay text from a single PDF
- Reads rubric JSON from disk
- Calls the AI model and validates JSON output
- Persists essay, rubric, and evaluation under a timestamped session directory
- Returns the evaluation JSON via `POST /evaluate`

Phase 0 (single-essay workflow) is complete. Next up: Phase 1 to enable batch submissions, CSV summaries, and ZIP packaging.
