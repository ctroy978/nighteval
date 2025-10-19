# Batch Essay Evaluator — Phase 1.2

Phase 1.2 keeps the batch workflow from Phase 1 but hardens reliability with structured output. Essays are still processed sequentially, yet every model response is now validated against Pydantic models, auto-retried on schema errors, and trimmed server-side before being written to disk.

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
| `AI_TIMEOUT_SECONDS`    | Request timeout in seconds (default 120)     |
| `STRUCTURED_OUTPUT`     | Enable Pydantic-validated JSON output (default `true`) |
| `VALIDATION_RETRY`      | Number of schema retries per essay (default `1`) |
| `TRIM_TEXT_FIELDS`      | Trim quotes/explanations/advice server-side (default `true`) |
| `ZIP_INCLUDE_PRINTABLE` | Reserved for printable summaries (Phase 4)   |
| `TEXT_VALIDATION_ENABLED` | Enable the Phase 2 text sufficiency gate (default `true`) |
| `MIN_TEXT_CHARS`        | Minimum total characters required before calling the AI (default `500`) |
| `MIN_CHARS_PER_PAGE`    | Minimum average characters per page (default `200`) |
| `ALLOW_PARTIAL_TEXT`    | Allow low-text PDFs through with a warning instead of rejection (default `false`) |

The text validation settings can also be provided via a lightweight YAML file. Place a `config/text_validation.yaml` file alongside the app with:

```yaml
text_validation:
  enabled: true
  min_text_chars: 500
  min_chars_per_page: 200
  allow_partial_text: false
```

Environment variables take precedence over YAML values when both are present.

Prompts live under `./prompts/` and are rendered with Jinja2. Edit these Markdown templates to tweak the evaluator without touching Python code.

---

## File Rules

- Export essays directly to PDF from Google Docs or Word. Avoid scans or photos.
- Verify you can select/copy text inside each PDF; scanned images will be rejected.
- Keep filenames stable—each PDF becomes `<Student Name>.pdf` in the job outputs.

---

## Rubric JSON Format

- `criteria`: list of objects with `id` (string, required, unique), `max_score` (positive integer), optional `name`, and optional `descriptors` (mapping rubric levels to guidance).
- `overall_points_possible` (optional) must equal the sum of all `max_score` values when present.
- Extra keys at the top level or in `criteria` objects are rejected.

Example payload:

```json
{
  "criteria": [
    {
      "id": "content",
      "name": "Content",
      "max_score": 4,
      "descriptors": {
        "4": "Thesis is clear and well supported.",
        "3": "Thesis is present but supporting evidence is thin.",
        "2": "Thesis is unclear or poorly supported.",
        "1": "Response lacks a defensible thesis."
      }
    },
    {
      "id": "mechanics",
      "name": "Mechanics",
      "max_score": 3
    }
  ],
  "overall_points_possible": 7
}
```

Keep rubric files in JSON format; YAML is not currently supported.

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
  "validated": 30,
  "schema_fail": 1,
  "retries_used": 4,
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
 │    ├── json/                 # validated per-student JSON results
 │    ├── json_failed/          # schema-corrective failures for diagnostics
 │    ├── text/                 # extracted essay text used for validation
 │    ├── summary.csv           # aggregate scores
 │    └── evaluations.zip       # archive of JSON results
└── logs/
      ├── job.log               # timestamp | student | status | ms | retries
      ├── results.jsonl         # per-essay metadata (timings, validation status, text metrics, schema errors)
      └── state.json            # current job snapshot used by the API
```

`summary.csv` lists every student alphabetically with total points earned, total points possible (sum of rubric `max_score` values), and one column per criterion (`criterion_<ID>_score`). Missing or invalid evaluations leave the score cells blank.

`evaluations.zip` contains one `{Student Name}.json` per validated essay. Schema failures are left in `outputs/json_failed/` with the raw response and error list for manual follow-up.

---

## Prompt Templates

Template files live in `./prompts/`:

- `system.md` — high-level system instruction for the model.
- `rubric_evaluator.md` — main evaluation template with placeholders for the rubric, essay text, and schema.
- `retry_context.md` — appended to the chat when the first response is invalid.
- `readme.md` — editing guidance.

Templates support the placeholders `{{ rubric_json }}`, `{{ essay_text }}`, `{{ schema_json }}`, and `{{ criterion_ids }}`. Add additional files as future phases expand the AI workflow.

---

## Definition of Done (Phase 1.2)

- Accepts a folder of PDFs plus `rubric.json` via `POST /jobs`.
- Parses the rubric and evaluation results with Pydantic models, enforcing criterion coverage and score bounds.
- Retries each essay once on schema errors, logging `validation_status`, `schema_errors`, and `retries_used`.
- Writes trimmed, validated JSON per student (successes in `outputs/json/`, schema failures in `outputs/json_failed/`).
- Builds `summary.csv` from validated results only and packages them into `evaluations.zip`.
- Streams progress, counters, and artifact locations via `GET /jobs/{job_id}`.
- Persists observability artifacts: `job.log`, `results.jsonl`, and `state.json` with validation counters.
- Loads prompt templates dynamically from `/prompts/`.
