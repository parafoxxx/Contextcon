"""Pulse web app.  Run:  .venv/Scripts/python -m uvicorn app:app --port 8000 --reload"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

import re

from pramaan import companies, config, guard, pulse, pulse_batch
from pramaan.crustdata import CallLog

app = FastAPI(title="Pulse")
WEB = Path(__file__).parent / "web"
SAMPLE = Path(__file__).parent / "sample" / "crm_sample.csv"
EVENTS: dict[str, list[dict]] = {}


def _emitter(job_id: str):
    EVENTS[job_id] = []

    def emit(kind: str, msg: str, level: str = "info", data=None):
        EVENTS[job_id].append({"t": round(time.time(), 2), "kind": kind, "msg": msg, "level": level, "data": data})
    return emit


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "pulse.html").read_text(encoding="utf-8")


@app.get("/api/config")
def cfg():
    return {"mode": config.MODE, "llm": config.LLM_PROVIDER}


@app.get("/api/stream/{job_id}")
async def stream(job_id: str):
    """Live progress for a run, as server-sent events."""
    async def gen():
        i = 0
        while True:
            evs = EVENTS.get(job_id, [])
            while i < len(evs):
                yield f"data: {json.dumps(evs[i])}\n\n"
                if evs[i]["kind"] == "done":
                    return
                i += 1
            await asyncio.sleep(0.25)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/pulse")
async def pulse_start(file: UploadFile = File(None), sample: bool = Form(False)):
    """Check a whole CRM export (CSV), or the bundled sample."""
    text = SAMPLE.read_text(encoding="utf-8") if (sample or file is None) else \
        (await file.read()).decode("utf-8", errors="replace")
    rows = pulse_batch.parse_csv(text)
    if not rows:
        raise HTTPException(400, "No accounts found. The CSV needs an 'account' (or 'company') column, and ideally 'website'.")
    job_id = "P" + uuid.uuid4().hex[:8]
    (pulse_batch.PULSE_DIR / job_id).mkdir(parents=True, exist_ok=True)
    (pulse_batch.PULSE_DIR / job_id / "input.csv").write_text(text, encoding="utf-8")  # kept so a stopped run can resume
    _start(job_id, text)
    return {"job_id": job_id, "count": len(rows)}


def _start(job_id, text):
    emit = _emitter(job_id)

    def work():
        try:
            pulse_batch.run(text, emit=emit, job_id=job_id)
        except Exception as e:  # noqa: BLE001
            emit("error", f"Run crashed: {type(e).__name__}: {e}", level="error")
            emit("done", "Stopped", data={"job_id": job_id})
    threading.Thread(target=work, daemon=True).start()


@app.post("/api/pulse/{job_id}/resume")
def pulse_resume(job_id: str):
    """After a crash or restart: accounts already checked are reused, actions already taken are not repeated."""
    f = pulse_batch.PULSE_DIR / job_id / "input.csv"
    if not f.exists():
        raise HTTPException(404)
    _start(job_id, f.read_text(encoding="utf-8"))
    return {"job_id": job_id, "resumed": True}


@app.get("/api/pulse/{job_id}")
def pulse_get(job_id: str):
    r = pulse_batch.load(job_id)
    if not r:
        raise HTTPException(404, "Not finished or no such job")
    return JSONResponse(r)


@app.get("/api/pulse/{job_id}/file/{name}")
def pulse_file(job_id: str, name: str):
    if name not in ("cleaned.csv", "corrections.jsonl", "report.json"):
        raise HTTPException(404)
    p = pulse_batch.PULSE_DIR / job_id / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=f"{job_id}_{name}")


@app.get("/api/pulses")
def pulse_list():
    out = []
    for d in sorted([d for d in pulse_batch.PULSE_DIR.iterdir() if d.is_dir()], key=lambda p: -p.stat().st_mtime)[:20]:
        r = pulse_batch.load(d.name)
        out.append({"job_id": d.name, "done": bool(r), "summary": (r or {}).get("summary")})
    return out


@app.post("/api/pulse_one")
def pulse_one(name: str = Form(...), domain: str = Form("")):
    """Check a single company someone names on the spot."""
    log = CallLog()
    dom = domain.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0] or None
    r = pulse.check_company(name.strip(), dom, companies.CompanyCache(log), log)
    r["credits"] = log.credits
    _save_one(r)
    return JSONResponse(r)


def _save_one(r: dict):
    """Keep single checks so the guard can answer for them later."""
    slug = re.sub(r"[^a-z0-9]+", "_", (r["input"].get("domain") or r["input"]["name"]).lower()).strip("_")[:60] or "x"
    (pulse_batch.PULSE_DIR / f"one_{slug}.json").write_text(json.dumps(r, ensure_ascii=False, default=str), encoding="utf-8")


@app.get("/api/guard")
def guard_check(to: str, check: bool = False):
    """For outreach agents: call before sending. `to` is an email, a domain or a company name.
    Returns decision block / hold / update / allow / unknown, with the cited reason and, if blocked, where its people went."""
    name, dom = guard.parse_target(to)
    if not name and not dom:
        raise HTTPException(400, "Pass ?to=<email, domain or company name>")
    a = guard.lookup(name, dom, guard.accounts_from(pulse_batch.PULSE_DIR))
    if a is None and check and dom not in guard.FREE_MAIL:
        log = CallLog()
        a = pulse.check_company(name or dom.split(".")[0], dom, companies.CompanyCache(log), log)
        _save_one(a)
    return JSONResponse(guard.answer(to, a))
