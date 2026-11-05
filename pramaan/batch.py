"""Run a whole pile of applications: verify each one, then analyse them together."""
from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from . import checks, config, outreach, pipeline, rings
from .crustdata import CallLog
from .rings import Applicant

BATCH_DIR = config.DATA / "batches"
BATCH_DIR.mkdir(parents=True, exist_ok=True)


def pdf_meta(data: bytes, filename: str) -> dict:
    if not filename.lower().endswith(".pdf"):
        return {}
    try:
        from pypdf import PdfReader
        info = PdfReader(io.BytesIO(data)).metadata or {}
        return {"creator": str(info.get("/Creator") or ""), "producer": str(info.get("/Producer") or ""),
                "created": str(info.get("/CreationDate") or "")}
    except Exception:  # noqa: BLE001 - metadata is a nice-to-have
        return {}


def save(b: dict):
    (BATCH_DIR / f"{b['batch_id']}.json").write_text(json.dumps(b, indent=1, default=str), encoding="utf-8")


def load(batch_id: str) -> Optional[dict]:
    f = BATCH_DIR / f"{batch_id}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def verify_batch(files: list[tuple[bytes, str]], *, consent: bool, emit: Callable = lambda *a, **k: None,
                 batch_id: Optional[str] = None, claims_override: Optional[dict] = None, workers: int = 3,
                 do_outreach: bool = True) -> dict:
    """claims_override: {filename: ResumeClaims} to skip PDF reading (tests)."""
    t0 = time.time()
    batch_id = batch_id or "B" + uuid.uuid4().hex[:8]
    prev = load(batch_id) or {}
    b = {"batch_id": batch_id, "created_at": prev.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
         "files": [f for _, f in files], "runs": prev.get("runs", {}), "status": "running"}
    save(b)
    emit("batch", f"Batch {batch_id}: {len(files)} applications.")

    def one(item):
        data, fn = item
        run_id = f"{batch_id}_{hashlib.sha256(fn.encode()).hexdigest()[:8]}"
        if fn in b["runs"] and (pipeline.load(b["runs"][fn]) or None) and pipeline.load(b["runs"][fn]).overall != "pending":
            r = pipeline.load(b["runs"][fn])  # resume: already done before a crash
            emit("resume", f"{r.candidate}: already checked, reusing.")
        else:
            r = pipeline.verify(data, fn, consent=consent, run_id=run_id, do_outreach=False,
                                claims=(claims_override or {}).get(fn),
                                emit=lambda k, m, level="info", data=None: emit("applicant_error", f"[{fn}] {m}", level="warn") if k == "error" else None)
            emit("applicant", f"{r.candidate}: {r.summary or r.overall}", data={"run_id": r.run_id, "overall": r.overall})
        b["runs"][fn] = r.run_id
        save(b)
        return Applicant(run_id=r.run_id, name=r.candidate, report=r, pdf_meta=pdf_meta(data, fn))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        apps = list(ex.map(one, files))

    emit("graph", "Linking applicants: shared employers, references, contact details, text, PDF metadata…")
    cache = checks.CompanyCache(CallLog())
    result = rings.analyse(batch_id, apps, lambda name, cid=None: cache.get(name, cid)[0] if name else None)
    for c in result["clusters"]:
        lvl = {"ring": "bad", "explained_cohort": "good", "watch": "warn"}.get(c["type"], "info")
        emit("cluster", f"{c['type'].replace('_', ' ').upper()} {c['id']} ({len(c['members'])} applicants): {c['reason']}", level=lvl,
             data=c)

    # Act: employer outreach only for VERIFY. HOLD goes to a person; nothing is sent to a suspected ring.
    if do_outreach:
        for a in apps:
            if result["decisions"][a.run_id]["decision"] == "VERIFY":
                a.report.outreach = outreach.plan_and_send(a.report, CallLog())
                pipeline.save(a.report)

    for a in apps:
        pipeline.save(a.report)  # includes known-fake verdicts added during analysis
    b.update(result)
    b["applicants"] = {a.run_id: {"name": a.name, "overall": a.report.overall, "summary": a.report.summary,
                                  "file": next(f for f, rid in b["runs"].items() if rid == a.run_id)} for a in apps}
    counts = {}
    for d in result["decisions"].values():
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
    b.update(status="done", counts=counts, duration_s=round(time.time() - t0, 1))
    save(b)
    emit("done", f"Batch done in {b['duration_s']}s: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())),
         data={"batch_id": batch_id, "counts": counts})
    return b
