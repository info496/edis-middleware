#!/usr/bin/env bash
set -e

# (i browser sono già nell'immagine grazie al Dockerfile)
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}"
