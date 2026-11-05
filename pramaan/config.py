import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip().strip('"').strip("'")



ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
CRUSTDATA_API_KEY = _env("CRUSTDATA_API_KEY", "")
MODE = _env("PRAMAAN_MODE", "live").strip().lower()  # live | replay

MODEL = "claude-opus-5-5"
LLM_PROVIDER = _env("LLM_PROVIDER", "anthropic").strip().lower()   # anthropic | gemini
GEMINI_API_KEY = _env("GEMINI_API_KEY", "")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_FALLBACKS = [m.strip() for m in _env("GEMINI_FALLBACKS", "gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-3.8-flash").split(",") if m.strip()]

DATA = ROOT / "data"
CACHE_DIR = DATA / "cache"
for d in (CACHE_DIR,):
    d.mkdir(parents=True, exist_ok=True)
