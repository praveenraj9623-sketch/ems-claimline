#!/usr/bin/env sh
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
exec .venv/bin/python -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501
