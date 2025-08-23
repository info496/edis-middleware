#!/usr/bin/env bash
set -e
python -m playwright install chromium || true
python -m playwright install-deps || true
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
