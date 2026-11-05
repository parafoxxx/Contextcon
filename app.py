"""Pramaan web app.  Run:  .venv/Scripts/python -m uvicorn app:app --reload --port 8000"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from pramaan import config, pipeline

app = FastAPI(title="Pramaan")
WEB = Path(__file__).parent / "web"
EVENTS: dict[str, list[dict]] = {}


def _emitter(run_id: str):
    EVENTS[run_id] = []

    def emit(kind: str, msg: str, level: str = "info", data=None):
        EVENTS[run_id].append({"t": round(time.time(), 2), "kind": kind, "msg": msg, "level": level, "data": data})
    return emit


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def cfg():
    return {"mode": config.MODE, "smtp": bool(config.SMTP_HOST), "demo_inbox": config.DEMO_INBOX or None}


@app.post("/api/verify")
async def verify(file: UploadFile = File(...), consent: bool = Form(False)):
    if not consent:
        raise HTTPException(400, "Candidate consent is required before a background check can start.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    run_id = uuid.uuid4().hex[:12]
    (config.UPLOADS_DIR / f"{run_id}_{Path(file.filename).name}").write_bytes(data)
    emit = _emitter(run_id)

    def work():
        try:
            pipeline.verify(data, file.filename, consent=True, emit=emit, run_id=run_id)
        except Exception as e:  # noqa: BLE001
            emit("error", f"Run crashed: {type(e).__name__}: {e}", level="error")
            emit("done", "Stopped", data={"overall": "failed"})
    threading.Thread(target=work, daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/stream/{run_id}")
async def stream(run_id: str):
    async def gen():
        i = 0
        while True:
            evs = EVENTS.get(run_id, [])
            while i < len(evs):
                yield f"data: {json.dumps(evs[i])}\n\n"
                if evs[i]["kind"] == "done":
                    return
                i += 1
            await asyncio.sleep(0.25)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/report/{run_id}")
def report(run_id: str):
    r = pipeline.load(run_id)
    if not r:
        raise HTTPException(404, "No such run")
    return JSONResponse(json.loads(r.model_dump_json()))


@app.get("/api/runs")
def runs():
    out = []
    for f in sorted(config.RUNS_DIR.glob("*.json"), key=lambda p: -p.stat().st_mtime)[:30]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"run_id": d["run_id"], "candidate": d["candidate"], "overall": d["overall"], "created_at": d["created_at"]})
        except Exception:  # noqa: BLE001
            continue
    return out


@app.post("/api/reply/{run_id}/{outreach_id}")
def reply(run_id: str, outreach_id: str, text: str = Form(...)):
    try:
        r = pipeline.apply_reply(run_id, outreach_id, text)
    except StopIteration:
        raise HTTPException(404, "Unknown outreach id")
    return JSONResponse(json.loads(r.model_dump_json()))


@app.get("/explain/{run_id}", response_class=HTMLResponse)
def explain_page(run_id: str):
    r = pipeline.load(run_id)
    if not r:
        raise HTTPException(404)
    items = [v for v in r.verdicts if v.status.value in ("discrepancy", "review") and v.next_action == "ask_candidate"]
    rows = "".join(
        f"""<form class=item method=post action="/explain/{run_id}" enctype="multipart/form-data">
        <b>{v.claim_text}</b><p>{v.reason}</p><input type=hidden name=claim_id value="{v.claim_id}">
        <input type=file name=file required accept=".pdf,.png,.jpg,.jpeg"> <button>Upload</button></form>""" for v in items)
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
    <title>Explain discrepancies</title><style>body{{font:15px system-ui;max-width:680px;margin:32px auto;padding:0 16px}}
    .item{{border:1px solid #ddd;border-radius:10px;padding:14px;margin:12px 0}}</style>
    <h2>Hi {r.candidate.split()[0]}, help us verify a few details</h2>
    <p>Nothing is shared with your employer until you've had a chance to respond. An offer letter, relieving letter or payslip works.</p>
    {rows or '<p>Nothing needs your input. Thank you!</p>'}"""


@app.post("/explain/{run_id}", response_class=HTMLResponse)
async def explain_upload(run_id: str, claim_id: str = Form(...), file: UploadFile = File(...)):
    data = await file.read()
    r = pipeline.apply_document(run_id, claim_id, data, file.filename)
    v = next(v for v in r.verdicts if v.claim_id == claim_id)
    return f"<meta charset=utf-8><p style='font:15px system-ui;margin:32px'>Thanks. Received. Status: <b>{v.status.value}</b>. {v.reason}</p><a href='/explain/{run_id}'>Back</a>"


@app.get("/api/scoreboard")
def scoreboard():
    f = config.DATA / "scoreboard.json"
    return JSONResponse(json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"runs": []})


@app.get("/static/{name}")
def static(name: str):
    p = WEB / name
    if not p.exists() or p.parent != WEB:
        raise HTTPException(404)
    return FileResponse(p)
