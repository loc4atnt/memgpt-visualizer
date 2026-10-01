#!/usr/bin/env bash
# Tạo môi trường ảo (lần đầu), cài thư viện, rồi chạy server.
# Dùng: ./run.sh                 (LIVE nếu .env có OPENAI_API_KEY, ngược lại SCRIPTED)
#       ./run.sh --scripted      (luôn dùng LLM mô phỏng)
#       ./run.sh --replay runs/sample_scripted.jsonl
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "→ Tạo môi trường ảo .venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "→ Cài thư viện"
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
  touch .venv/.installed
fi
exec python server.py "$@"
