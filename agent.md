# `agent.md`

## 🧠 Project Overview — Batch Essay Evaluator

**Purpose:**
A lightweight web application that assists teachers in **evaluating student essays** using a provided **rubric**.
The system processes a **folder of PDF essays**, sends each essay through an **AI model**, and produces:

1. **Per-student evaluations** (JSON + printable text)
2. **A CSV summary** of scores across all students
3. **A ZIP archive** of all individual evaluations

Later phases add OCR fallback, rubric extraction, printable reports, and automated student emails.

---

## 🎯 Primary Goals

* Automate repetitive grading using AI + teacher-provided rubrics.
* Keep everything local, transparent, and teacher-friendly.
* Avoid complexity: simple UI, static files, no accounts or cloud dependencies.
* Run efficiently on a **Raspberry Pi** behind a local Nginx reverse proxy.

---

## 🧩 System Architecture

### Core Concept

```
[PDF Essays Folder] + [Rubric] 
         ↓
 [AI Evaluation Pipeline]
         ↓
  ├─ Individual JSON Evaluations
  ├─ summary.csv (aggregate)
  ├─ evaluations.zip (all results)
  └─ Optional per-student printables
```

### Components

| Layer                         | Purpose                                                            |
| ----------------------------- | ------------------------------------------------------------------ |
| **FastAPI Backend**           | Core web server, job orchestration, and API endpoints              |
| **PDF Extraction**            | Converts each student’s PDF into raw text (PyPDF2 / pdfminer.six)  |
| **AI Evaluation Engine**      | Calls an OpenAI-compatible endpoint using structured prompts       |
| **Validation Layer (future)** | Uses **PydanticAI** to enforce valid JSON structure                |
| **Artifact Generator**        | Writes validated results to disk, builds CSV + ZIP outputs         |
| **Job Manager**               | Tracks per-batch progress, logs, retries, and failures             |
| **Optional UI Layer**         | Minimal HTML pages rendered by FastAPI (status/progress/downloads) |

---

## 🧱 Tech Stack

### Core Technologies

| Category                        | Tool                        | Notes                                                      |
| ------------------------------- | --------------------------- | ---------------------------------------------------------- |
| **Backend Framework**           | FastAPI                     | Lightweight, async-capable, easy to serve behind Nginx     |
| **Web Server**                  | Uvicorn + Nginx             | Uvicorn for ASGI; Nginx reverse proxy for local deployment |
| **AI Client**                   | OpenAI-compatible API       | Defaults to `grok-4-fast-reasoning` (xAI) via compatible endpoint |
| **Schema Enforcement (future)** | **PydanticAI**              | Strict JSON structure + auto-reprompt                      |
| **PDF Handling**                | PyPDF2 / pdfminer.six       | Reliable, pure-Python extraction                           |
| **OCR (future)**                | pytesseract                 | For scanned PDFs (Phase 2)                                 |
| **Data Serialization**          | JSON, CSV, ZIP              | Standard outputs, easy to parse or share                   |
| **Config Management**           | python-dotenv / YAML        | ENV-based configuration for portability                    |
| **Logging**                     | Standard logging + job logs | Simple, text-based per-job reporting                       |

---

## ⚙️ Deployment

**Environment:**

* Raspberry Pi 4 B (4–8 GB RAM) or similar small Linux server
* No external dependencies beyond AI API access
* Local Nginx proxy with gzip enabled for artifact downloads
* Accessible via LAN (no authentication required)

**Folder Layout:**

```
/data/
 └── sessions/
      └── <timestamp>-<job_name>/
           ├── inputs/
           │    ├── essays/
           │    └── rubric.json
           ├── outputs/
           │    ├── json/
           │    ├── text/
           │    ├── summary.csv
           │    └── evaluations.zip
           └── logs/
```

---

## 🚀 Phase Summary

| Phase   | Focus                                                      | Status              |
| ------- | ---------------------------------------------------------- | ------------------- |
| **0**   | Single essay → AI → validated JSON output                  | ✅ Baseline workflow |
| **1**   | Batch processing, CSV + ZIP, progress endpoint             | ✅ Completed (Phase 1) |
| **1.2** | Integrate **PydanticAI** for structured output enforcement | ✅ Completed |
| **2**   | OCR fallback for scanned PDFs                              | 🚧 Text validation gate (no OCR) |
| **3**   | Rubric PDF → JSON extraction + correction UI               | 🔜                  |
| **4**   | Printable summaries (txt/pdf) per student                  | 🔜                  |
| **5**   | Email results (optional SMTP config)                       | 🔜                  |

Phase 0 is complete and verified in the walking skeleton. Phases 1 and 1.2 are stable and in production. Phase 2 is underway with the new text validation gate and logging enhancements; OCR fallback remains on deck for a later sprint.

---

## 📝 Phase 2 Status

- Text validation gate rejects low-text PDFs before the AI call and records remediation advice alongside the job logs.
- Per-essay text dumps now land in `outputs/text/` to make low-text diagnoses reproducible.
- Job status exposes `text_ok_count`, `low_text_warning_count`, and `low_text_rejected_count` via the API and `state.json`.
- Thresholds are configurable through ENV or `config/text_validation.yaml`, keeping deployments flexible without code edits.

---

## 🔒 Design Principles

* **Local-first:** Runs entirely on a teacher’s LAN.
* **Fail-soft:** One failed essay never breaks a batch.
* **Transparent:** AI output saved verbatim as JSON.
* **Deterministic:** File names and results are stable and reproducible.
* **Simple deployment:** `uvicorn app:app --host 0.0.0.0 --port 8000`

---

## 🧰 Future Considerations

* Replace direct AI calls with a **queue-based agent** for longer batches.
* Add **CLI mode** for headless use (no web UI).
* Consider **SQLite job tracking** if scaling beyond a few hundred essays.
* Explore **fine-tuned rubric scoring** models for consistent grading.

---

## ✅ Deliverable Expectations

A developer or AI agent reading this file should be able to:

1. Stand up a local FastAPI service that can evaluate essays.
2. Understand how outputs are stored and validated.
3. Extend later phases (OCR, PydanticAI, email) without architectural changes.

---

Would you like me to follow this with a **`stack_setup.md`** file that lists exact versions, environment variables, and a sample `.env` layout for the Pi deployment? It would complement `agent.md` nicely.
