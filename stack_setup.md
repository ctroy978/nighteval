# Stack Setup

This guide captures the baseline environment, dependencies, and configuration required to run the Batch Essay Evaluator on a Raspberry Pi or comparable Linux host.

---

## System Requirements

- Raspberry Pi 4 B (4 GB RAM minimum; 8 GB recommended)
- 64-bit Raspberry Pi OS (Bookworm) or other Debian-based distro
- Python 3.11 (3.10+ supported; 3.11 tested)
- 8 GB free disk space under `/data` for job artifacts
- Stable LAN connection with outbound HTTPS access to the chosen AI API

### System Packages

Install core tooling and libraries:

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip build-essential libpq-dev \
                     libffi-dev libjpeg-dev zlib1g-dev libtesseract-dev tesseract-ocr \
                     poppler-utils ghostscript nginx unzip zip
```

Notes:
- `tesseract-ocr` and `libtesseract-dev` support Phase 2 OCR fallback.
- `poppler-utils` enables `pdftotext`, useful for debugging PDF extraction.
- `nginx` acts as the local reverse proxy; configure separately.

---

## Python Environment

Create a dedicated virtual environment under the project root:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### Core Python Dependencies

Install the base stack:

```bash
pip install fastapi==0.111.0 "uvicorn[standard]"==0.30.1 httpx==0.27.0 python-dotenv==1.0.1 \
            pydantic==2.7.1 pydantic-ai==0.0.11 \
            PyPDF2==3.0.1 pdfminer.six==20231228 pandas==2.2.2 \
            openpyxl==3.1.3 pytesseract==0.3.10
```

Recommended extras:
- `ruff` for linting (`pip install ruff==0.4.4`).
- `pytest` for unit tests (`pip install pytest==8.2.1`).

Freeze the environment for reproducibility (optional):

```bash
pip freeze > requirements.lock
```

---

## Environment Variables

The application reads configuration from environment variables (loaded via `.env`).

| Variable                 | Required | Description                                                        |
| ------------------------ | -------- | ------------------------------------------------------------------ |
| `APP_ENV`                | No       | Deployment mode (`development`, `production`); defaults to `dev`.  |
| `APP_BASE_DIR`           | Yes      | Root directory for sessions (e.g., `/data/sessions`).              |
| `XAI_API_BASE` / `OPENAI_API_BASE` | Yes      | Base URL of the OpenAI-compatible API endpoint.                    |
| `XAI_API_KEY` / `OPENAI_API_KEY`   | Yes      | API key/token for the model provider.                              |
| `XAI_MODEL` / `OPENAI_MODEL`       | Yes      | Model name (defaults to `grok-4-fast-reasoning`).                  |
| `AI_TIMEOUT_SECONDS`     | No       | HTTP timeout for AI calls (default `60`).                          |
| `MAX_CONCURRENT_JOBS`    | No       | Limits in-flight jobs (default `1`).                               |
| `PDF_TEXT_EXTRACTOR`     | No       | `pypdf2`, `pdfminer`, or `mixed`; default auto-select.             |
| `ENABLE_OCR_FALLBACK`    | No       | `true`/`false`; enables pytesseract pipeline.                      |
| `LOG_LEVEL`              | No       | `INFO`, `DEBUG`, etc.; defaults to `INFO`.                         |
| `UVICORN_HOST`           | No       | Host binding (default `0.0.0.0`).                                  |
| `UVICORN_PORT`           | No       | Port binding (default `8000`).                                     |
| `UVICORN_RELOAD`         | No       | `true` in development to enable auto-reload.                       |

---

## Sample `.env`

```
APP_ENV=production
APP_BASE_DIR=/data/sessions

XAI_API_BASE=https://api.x.ai/v1
XAI_API_KEY=sk-REPLACE_ME
XAI_MODEL=grok-4-fast-reasoning
AI_TIMEOUT_SECONDS=60
MAX_CONCURRENT_JOBS=1
PDF_TEXT_EXTRACTOR=pdfminer
ENABLE_OCR_FALLBACK=false

LOG_LEVEL=INFO
UVICORN_HOST=0.0.0.0
UVICORN_PORT=8000
UVICORN_RELOAD=false
```

Ensure `.env` permissions restrict the API key (`chmod 600 .env`).

---

## Directory Preparation

Pre-create the data directories with correct ownership:

```bash
sudo mkdir -p /data/sessions
sudo chown -R $USER:$USER /data
```

For job isolation, sessions follow `timestamp-jobname` naming (see `agent.md`).

---

## Running the Service

Activate the environment and start FastAPI via Uvicorn:

```bash
source .venv/bin/activate
uvicorn app:app --host ${UVICORN_HOST:-0.0.0.0} --port ${UVICORN_PORT:-8000}
```

For background operation, use `systemd` or `supervisor`. Example systemd unit:

```
[Unit]
Description=Batch Essay Evaluator
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/pi/evaluator
EnvironmentFile=/home/pi/evaluator/.env
ExecStart=/home/pi/evaluator/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Reload and enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now essay-evaluator.service
```

---

## Nginx Reverse Proxy (Optional)

Create `/etc/nginx/sites-available/essay-evaluator`:

```
server {
    listen 80;
    server_name evaluator.local;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering on;
        proxy_buffers 16 16k;
        proxy_buffer_size 16k;
    }

    location /downloads/ {
        gzip on;
        gzip_types application/zip text/plain text/csv application/json;
        root /data/sessions;
        autoindex on;
    }
}
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/essay-evaluator /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Point `evaluator.local` to the Pi via `/etc/hosts` on client machines.

---

## Validation Checklist

- [ ] `.venv` activated and dependencies installed
- [ ] `.env` populated with valid API credentials
- [ ] `/data/sessions` exists and writable
- [ ] `uvicorn app:app` serves `/health` (or equivalent) endpoint
- [ ] Nginx proxy routes requests and serves `/downloads/`

---

This setup document complements `agent.md`, enabling a new developer or automation agent to bootstrap and operate the evaluator stack reliably.
