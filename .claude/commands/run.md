---
description: Start the Pulse app and confirm it responds
---
1. If `.venv` is missing, create it and install: `python -m venv .venv && .venv/Scripts/pip install -r requirements.txt`.
2. If `.env` is missing, tell the user to copy `.env.example` to `.env` and fill in the keys. Never read or print `.env`.
3. Check whether something already answers on port 8000 (`curl -s localhost:8000/api/config`). If not, start
   `.venv/Scripts/python -m uvicorn app:app --port 8000 --reload` in the background.
4. Confirm `/api/config` responds and tell the user to open http://localhost:8000.
