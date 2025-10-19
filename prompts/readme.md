# Prompt Templates

Edit the Markdown files in this directory to adjust the evaluator's behaviour without touching application code. Templates use the Jinja2 syntax and support these placeholders:

- `rubric_json` – JSON string of the rubric file supplied with the job.
- `essay_text` – Raw text extracted from the student's PDF.
- `schema_json` – JSON structure that the AI must return.
- `criterion_ids` – Comma-separated list of rubric criterion IDs for coverage reminders.

Add additional templates as future phases introduce new model calls or retry strategies.
