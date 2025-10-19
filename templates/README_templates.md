# Summary Templates

The printable summaries use Jinja2 templates stored in this directory. Templates receive the following context:

- `student_name`: display name derived from the PDF file stem.
- `job_name`: user supplied label or job id.
- `generated_at`: UTC timestamp in ISO format.
- `eval`: validated evaluation dictionary (`overall`, `criteria`, etc.).
- `rubric`: canonical rubric JSON.
- `criteria_rows`: convenience list with `id`, `score`, `evidence`, `explanation`, and `advice` strings.
- `flags`: optional dictionary for contextual warnings (for example `low_text_warning`).
- `COURSE_NAME` / `TEACHER_NAME`: pulled from configuration for footer text.
- `SUMMARY_LINE_WIDTH`: configured soft wrap width.
- Helpers `wrap_lines(value, width=..., max_lines=...)` and `wrap_text(value, width=...)` are available to keep content within printer friendly widths.

Default templates:

- `student_summary.txt.j2`: plain text layout with fixed width columns.
- `student_summary.md.j2`: optional Markdown layout enabled when `MARKDOWN_SUMMARY=true`.
- `batch_header.txt.j2`: optional README placed at the top of `evaluations.zip` when `INCLUDE_ZIP_README=true`.

Update these templates to adjust the tone or layout of the generated summaries.
