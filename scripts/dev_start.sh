#!/usr/bin/env bash
# Local development startup (backend + frontend dev servers).
# Requires: PostgreSQL at 127.0.0.1:5432 (db/user/password = ccdev), Python 3.12, Node 22.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d backend/.venv ]; then
  /opt/python-3.12/bin/python3 -m venv backend/.venv
  backend/.venv/bin/pip install -r backend/requirements.txt
fi

if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm install)
fi

(cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000) &
BACKEND_PID=$!
trap 'kill $BACKEND_PID 2>/dev/null || true' EXIT

(cd frontend && npm run dev)
