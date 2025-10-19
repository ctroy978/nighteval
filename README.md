# Batch Essay Evaluator — Phase 1

Phase 1 upgrades the prototype to process entire folders of student essays, generate batch artifacts, and expose job progress APIs. Each job copies the source PDFs, evaluates them sequentially via an OpenAI-compatible model, records results, and produces both CSV and ZIP summaries.

---

## Prerequisites

- Python 3.10+
- Virtual environment (recommended)
- `.env` populated with your AI credentials (see configuration below)

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Configuration

| Variable                | Purpose                                      |
| ----------------------- | -------------------------------------------- |
| `AI_PROVIDER_URL`       | OpenAI-compatible base URL (optional)        |
| `AI_MODEL`              | Model identifier (e.g. `gpt-4-turbo`)        |
| `AI_API_KEY`            | API key for the selected provider            |
| `OUTPUT_BASE`           | Root directory for job sessions (`/data/sessions` default) |
| `MAX_PARALLEL`          | Reserved for future parallelism (not used)   |
| `AI_TIMEOUT_SECONDS`    | Request timeout in seconds (default 60)      |
| `AI_RETRY`              | Reserved for future use (Phase 2+)           |
| `ZIP_INCLUDE_PRINTABLE` | Reserved for printable summaries (Phase 4)   |

Prompts live under `./prompts/` and are rendered with Jinja2. Edit these Markdown templates to tweak the evaluator without touching Python code.

---

## Running the API

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

All job artifacts are written to `${OUTPUT_BASE}/{timestamp}-{job_name}/` (the job name is optional).

---

## Batch Workflow

### 1. Start a job

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
        "essays_folder": "/home/pi/essays",
        "rubric_path": "/home/pi/rubrics/english_rubric.json",
        "job_name": "period1-oct"
      }'
```

Sample response:

```json
{
  "job_id": "20240101-120501-period1-oct",
  "status": "running",
  "total": 32,
  "processed": 0
}
```

### 2. Poll job status

```bash
curl http://localhost:8000/jobs/20240101-120501-period1-oct
```

Example response:

```json
{
  "job_id": "20240101-120501-period1-oct",
  "status": "completed",
  "total": 32,
  "processed": 32,
  "succeeded": 30,
  "failed": 2,
  "artifacts": {
    "csv": "/data/sessions/20240101-120501-period1-oct/outputs/summary.csv",
    "zip": "/data/sessions/20240101-120501-period1-oct/outputs/evaluations.zip"
  },
  "started_at": "2024-01-01T17:05:01.123456",
  "finished_at": "2024-01-01T17:12:42.987654",
  "error": null
}
```

### 3. Download artifacts

```bash
curl -O http://localhost:8000/jobs/20240101-120501-period1-oct/download/csv
curl -O http://localhost:8000/jobs/20240101-120501-period1-oct/download/zip
```

---

## Job Directory Layout

```
${OUTPUT_BASE}/{timestamp}-{job_name}/
 ├── inputs/
 │    ├── essays/               # copied source PDFs
 │    └── rubric.json
 ├── outputs/
 │    ├── json/                 # per-student JSON results (success or error)
 │    ├── summary.csv           # aggregate scores
 │    └── evaluations.zip       # archive of JSON results
 └── logs/
      ├── job.log               # timestamp | student | status | ms | retries
      ├── results.jsonl         # per-essay metadata (timings, tokens, failures)
      └── state.json            # current job snapshot used by the API
```

`summary.csv` lists every student alphabetically with total points earned, total points possible (sum of rubric `max_score` values), and one column per criterion (`criterion_<ID>_score`). Missing or invalid evaluations leave the score cells blank.

`evaluations.zip` contains one `{Student Name}.json` per essay. Failed evaluations include an `error` message so downstream tooling can flag them.

---

## Prompt Templates

Template files live in `./prompts/`:

- `system.md` — high-level system instruction for the model.
- `rubric_evaluator.md` — main evaluation template with placeholders for the rubric, essay text, and schema.
- `retry_context.md` — appended to the chat when the first response is invalid.
- `readme.md` — editing guidance.

Templates support the placeholders `{{ rubric_json }}`, `{{ essay_text }}`, and `{{ schema_json }}`. Add additional files as future phases expand the AI workflow.

---

## Definition of Done (Phase 1)

- Accepts a folder of PDFs plus `rubric.json` via `POST /jobs`.
- Processes essays sequentially, saving per-student JSON immediately.
- Validates model output (structure + required fields) and retries once on invalid JSON.
- Continues processing after individual failures, logging them and leaving CSV cells blank.
- Produces `summary.csv` and `evaluations.zip` in the outputs directory.
- Streams job progress through `GET /jobs/{job_id}` and exposes artifacts for download.
- Writes `job.log`, `results.jsonl`, and `state.json` for observability.
- Loads prompt text dynamically from `/prompts/`.
