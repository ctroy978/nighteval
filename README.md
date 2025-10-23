# Batch Essay Evaluator — Phase 4.1

Phase 4.1 builds on the hardened batch workflow (Phase 1.2) by layering in teacher-friendly printable summaries: TXT by default, optional Markdown, and ReportLab-powered PDFs with an optional merged packet. Essays are still processed sequentially, yet every model response is validated, retried on schema errors, trimmed server-side, and now rendered into printer-ready artifacts alongside the canonical JSON.

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
| `TRIM_TEXT_FIELDS`      | Trim excerpts, comments, and suggestions server-side (default `true`) |
| `PRINT_SUMMARY_ENABLED` | Write Phase 4 plain-text summaries to `outputs/print/` (default `true`) |
| `MARKDOWN_SUMMARY`      | Also render Markdown summaries to `outputs/print_md/` (default `false`) |
| `SUMMARY_LINE_WIDTH`    | Soft wrap width for TXT summaries (default `100`) |
| `INCLUDE_ZIP_README`    | Add a README banner to `evaluations.zip` (default `false`) |
| `ZIP_README_TEMPLATE`   | Template filename for the ZIP README (default `batch_header.txt.j2`) |
| `COURSE_NAME`           | Optional course label injected into summaries and PDFs |
| `TEACHER_NAME`          | Optional teacher name injected into summaries and PDFs |
| `PDF_SUMMARY_ENABLED`   | Generate per-student PDF summaries in `outputs/print_pdf/` (default `false`) |
| `PDF_BATCH_MERGE`       | Generate `batch_all_summaries.pdf` with all students combined (default `false`) |
| `PDF_PAGE_SIZE`         | PDF page size (`letter`, `a4`, …) for ReportLab (default `letter`) |
| `PDF_FONT`              | Base font for PDF summaries (default `Helvetica`) |
| `PDF_LINE_SPACING`      | Line spacing multiplier for PDF summaries (default `1.2`) |
| `TEXT_VALIDATION_ENABLED` | Enable the Phase 2 text sufficiency gate (default `true`) |
| `MIN_TEXT_CHARS`        | Minimum total characters required before calling the AI (default `500`) |
| `MIN_CHARS_PER_PAGE`    | Minimum average characters per page (default `200`) |
| `ALLOW_PARTIAL_TEXT`    | Allow low-text PDFs through with a warning instead of rejection (default `false`) |
| `RUBRIC_EXTRACTION_ENABLED` | Enable Phase 3 rubric extraction endpoints (default `true`) |
| `RUBRIC_MAX_PAGES`      | Limit pages read from rubric PDFs before extraction (default `10`) |
| `RUBRIC_MAX_CHARS`      | Cap characters forwarded to the extractor prompt (default `40000`) |
| `RUBRIC_RETRY`          | Number of AI retries for rubric extraction (default `1`) |
| `RUBRIC_REQUIRE_TOTALS_EQUAL` | Enforce that criterion totals match the overall points (default `true`) |
| `RUBRIC_ID_MAXLEN`      | Maximum length for generated rubric IDs (default `40`) |

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

Printable summary settings accept the same overrides via a `config/summary.yaml` file:

```yaml
summary:
  enabled: true
  markdown_enabled: false
  pdf_enabled: true
  pdf_batch_merge: true
  line_width: 96
  template_dir: templates
  text_template: student_summary.txt.j2
  markdown_template: student_summary.md.j2
  readme_template: batch_header.txt.j2
  course_name: "WR 121 - College Composition"
  teacher_name: "Dr. Lee"
```

As with the text gate, ENV values win if both YAML and environment variables are set.

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

## Rubric Extraction API

- `POST /rubrics/extract` — upload a rubric PDF or JSON file. Returns the canonical JSON and on-disk path when valid, otherwise a provisional draft plus validation errors.
- `GET /rubrics/{temp_id}/fix` — single-page editor for repairing JSON with live validation (uses `POST /rubrics/{temp_id}/save`).
- `POST /rubrics/{temp_id}/save` — validate and, when valid, persist the canonical `rubric.json` (append `?validate_only=1` to just check).
- `GET /rubrics/{temp_id}/preview` — HTML summary of the canonical rubric.
- `GET /rubrics/{temp_id}/download` — download the canonical JSON for reuse or archiving.

Each extraction session writes to `<OUTPUT_BASE>/rubric-*/` with:

```
inputs/
 ├── rubric.json              # canonical output when valid
 ├── rubric_provisional.json  # latest draft (extractor or manual)
 └── rubric_source.pdf        # original upload (if PDF)
logs/
 └── rubric_extract.log       # extraction + validation history
```

Use the returned `rubric.json` path when launching `/jobs`.

The Fix JSON screen provides a single textarea editor. Use **Validate** to see issues, **Save & Use** to persist once valid, and **Download JSON** for local backups.

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
  "text_ok_count": 29,
  "low_text_warning_count": 1,
  "low_text_rejected_count": 2,
  "rubric_version_hash": "f5c8a9b420f74d0f9f0a8b23d1f9b0d7e5f1e21f4879f8620d84f6c2ef2d4c10",
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
 │    ├── print/                # Phase 4 plain-text summaries (one per student when enabled)
 │    ├── print_md/             # Phase 4 Markdown summaries (optional)
 │    ├── print_pdf/            # Phase 4.1 PDF summaries (optional)
 │    ├── batch_all_summaries.pdf   # Phase 4.1 merged PDF (when PDF_BATCH_MERGE=true)
 │    ├── summary.csv           # aggregate scores
 │    └── evaluations.zip       # archive of JSON + printable artifacts
└── logs/
      ├── job.log               # timestamp | student | status | ms | retries
      ├── results.jsonl         # per-essay metadata (timings, validation status, text metrics, schema errors)
      ├── state.json            # current job snapshot used by the API (includes `rubric_version_hash`)
      └── rubric_extract.log    # present when the rubric came from a Phase 3 extraction session
```

`summary.csv` lists every student alphabetically with total points earned, total points possible (sum of rubric `max_score` values), and one column per criterion (`criterion_<ID>_score`). Missing or invalid evaluations leave the score cells blank.

`evaluations.zip` always contains one `{Student Name}.json` per validated essay. When printable modes are enabled it also bundles the contents of `print/`, `print_md/`, and `print_pdf/`, plus an optional `README.txt` rendered from `templates/batch_header.txt.j2`.

If `PDF_BATCH_MERGE=true`, the combined PDF lives at `outputs/batch_all_summaries.pdf` and is exposed via the API as well as on-disk.

### Printable Summary Endpoints

| Endpoint | Description |
| -------- | ----------- |
| `GET /jobs/{job_id}/students/{student}/summary.txt` | Streams the plain-text summary (404 if printable summaries are disabled or the student failed validation). |
| `GET /jobs/{job_id}/students/{student}/summary.md` | Streams the Markdown summary when `MARKDOWN_SUMMARY=true`. |
| `GET /jobs/{job_id}/students/{student}/summary.pdf` | Streams the PDF summary when `PDF_SUMMARY_ENABLED=true`. |
| `GET /jobs/{job_id}/batch.pdf` | Streams the merged PDF when `PDF_BATCH_MERGE=true`. |

---

## Prompt Templates

Template files live in `./prompts/`:

- `system.md` — high-level system instruction for the model.
- `rubric_evaluator.md` — main evaluation template with placeholders for the rubric, essay text, and schema.
- `retry_context.md` — appended to the chat when the first response is invalid.
- `rubric_extractor.md` — Phase 3 prompt that converts rubric text into the canonical JSON schema.
- `rubric_retry.md` — concise correction instructions when the extractor emits invalid JSON.
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

## Definition of Done (Phase 4.1)

- Renders per-student plain-text summaries (`outputs/print/*.txt`) whenever `PRINT_SUMMARY_ENABLED=true`.
- Optionally renders Markdown versions (`outputs/print_md/*.md`) behind `MARKDOWN_SUMMARY`.
- Optionally renders per-student PDF summaries (`outputs/print_pdf/*.pdf`) using ReportLab and the validated evaluation context when `PDF_SUMMARY_ENABLED=true`.
- Supports a merged `batch_all_summaries.pdf` artifact and `GET /jobs/{job_id}/batch.pdf` endpoint when `PDF_BATCH_MERGE=true`.
- Bundles printable artifacts (TXT/MD/PDF) into `evaluations.zip` alongside the JSON payloads, plus an optional ZIP README rendered from `templates/batch_header.txt.j2` when `INCLUDE_ZIP_README=true`.
- Logs printable metadata per essay in `results.jsonl` (`print_summary`, `summary_bytes`, `pdf_generated`, `pdf_bytes`, `pdf_path`) and marks `printed_pdf=true` in `job.log` for quick auditing.
- Tracks printable artifacts in `state.json`, exposing `pdf_count` and `pdf_batch_path` for API consumers.
