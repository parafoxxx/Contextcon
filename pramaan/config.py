import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip().strip('"').strip("'")



ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
CRUSTDATA_API_KEY = _env("CRUSTDATA_API_KEY", "")
MODE = _env("PRAMAAN_MODE", "fixture").strip().lower()  # live | fixture | replay

MODEL = "claude-opus-5-5"
LLM_PROVIDER = _env("LLM_PROVIDER", "anthropic").strip().lower()   # anthropic | gemini
GEMINI_API_KEY = _env("GEMINI_API_KEY", "")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_FALLBACKS = [m.strip() for m in _env("GEMINI_FALLBACKS", "gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-3.8-flash").split(",") if m.strip()]

DATA = ROOT / "data"
CACHE_DIR = DATA / "cache"
RUNS_DIR = DATA / "runs"
OUTBOX_DIR = DATA / "outbox"
UPLOADS_DIR = DATA / "uploads"
FIXTURES_DIR = ROOT / "fixtures"
for d in (CACHE_DIR, RUNS_DIR, OUTBOX_DIR, UPLOADS_DIR):
    d.mkdir(parents=True, exist_ok=True)

SMTP_HOST = _env("SMTP_HOST", "")
SMTP_PORT = int(_env("SMTP_PORT", "587") or 587)
SMTP_USER = _env("SMTP_USER", "")
SMTP_PASS = _env("SMTP_PASS", "").replace(" ", "")  # Gmail shows app passwords with spaces
SMTP_FROM = _env("SMTP_FROM", "")
DEMO_INBOX = _env("DEMO_INBOX", "")

# Tolerances that protect honest candidates
DATE_TOLERANCE_MONTHS = 2
OVERLAP_TOLERANCE_MONTHS = 3
