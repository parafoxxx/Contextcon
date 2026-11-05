import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CRUSTDATA_API_KEY = os.getenv("CRUSTDATA_API_KEY", "")
MODE = os.getenv("PRAMAAN_MODE", "fixture").strip().lower()  # live | fixture | replay

MODEL = "claude-opus-5-5"

DATA = ROOT / "data"
CACHE_DIR = DATA / "cache"
RUNS_DIR = DATA / "runs"
OUTBOX_DIR = DATA / "outbox"
UPLOADS_DIR = DATA / "uploads"
FIXTURES_DIR = ROOT / "fixtures"
for d in (CACHE_DIR, RUNS_DIR, OUTBOX_DIR, UPLOADS_DIR):
    d.mkdir(parents=True, exist_ok=True)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")
DEMO_INBOX = os.getenv("DEMO_INBOX", "")

# Tolerances that protect honest candidates
DATE_TOLERANCE_MONTHS = 2
OVERLAP_TOLERANCE_MONTHS = 3
