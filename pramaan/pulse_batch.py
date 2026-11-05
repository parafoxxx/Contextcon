"""Pulse over a CRM export: one canonical record per company, a status for every account, and actions.

Outputs (data/pulse/<job>/):
  cleaned.csv        the CRM with status, since, reason, sources, canonical id, merged-into
  corrections.jsonl  a correction feed a data provider can ingest (zombie records, with evidence)
  actions.json       CRM actions taken exactly once (pause sequences, mark closed, merge duplicates)
  report.json        everything, for the UI
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Optional

from . import companies, config, pulse
from .crustdata import CallLog

PULSE_DIR = config.DATA / "pulse"
PULSE_DIR.mkdir(parents=True, exist_ok=True)
LEDGER = PULSE_DIR / "crm_ledger.json"
NAME_COLS = ("account", "company", "name", "account name", "company name", "organization")
DOMAIN_COLS = ("website", "domain", "url", "company website")


def idem_key(job_id: str, kind: str, company: str, rows: list[str]) -> str:
    raw = "|".join([job_id, kind, (company or "").lower(), ",".join(sorted(rows))])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def act_once(key: str, action: dict) -> str:
    """Record a CRM action exactly once. A retry or a re-run never repeats it."""
    led = _ledger()
    if key in led:
        return f"already done at {led[key]['at']}"
    led[key] = {**action, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(led, indent=1), encoding="utf-8")
    tmp.replace(LEDGER)
    return "done"


def parse_csv(text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    out = []
    for i, r in enumerate(rows, 1):
        low = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        name = next((low[c] for c in NAME_COLS if low.get(c)), "")
        dom = next((low[c] for c in DOMAIN_COLS if low.get(c)), "")
        dom = dom.lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0] or None
        if name or dom:
            out.append({"row": i, "name": name or dom, "domain": dom, "raw": r})
    return out


def run(csv_text: str, *, emit: Callable = lambda *a, **k: None, job_id: Optional[str] = None, workers: int = 3) -> dict:
    t0 = time.time()
    job_id = job_id or "P" + uuid.uuid4().hex[:8]
    jdir = PULSE_DIR / job_id
    jdir.mkdir(exist_ok=True)
    rows = parse_csv(csv_text)
    emit("start", f"{len(rows)} accounts in the CRM export.")
    log = CallLog()
    cache = companies.CompanyCache(log)
    prev = {}
    if (jdir / "report.json").exists():  # resume: accounts already checked are reused
        prev = {a["row"]: a for a in json.loads((jdir / "report.json").read_text(encoding="utf-8")).get("accounts", [])}

    def one(r):
        if r["row"] in prev and prev[r["row"]].get("status"):
            emit("account", f"{r['name']}: {prev[r['row']]['status']} (already checked)", data=prev[r["row"]])
            return prev[r["row"]]
        try:
            res = pulse.check_company(r["name"], r["domain"], cache, log)
        except Exception as e:  # noqa: BLE001 - one bad account never stops the run
            res = {"input": {"name": r["name"], "domain": r["domain"]}, "canonical": {}, "status": "CANT_TELL",
                   "why": f"Check failed: {type(e).__name__}: {e}", "since": None, "zombie": False, "people": {}, "signals": []}
        res["row"] = r["row"]
        res["crm"] = r["raw"]
        lvl = {"DEAD": "bad", "INSOLVENT": "bad", "ACQUIRED": "warn", "RENAMED": "warn", "DISTRESSED": "warn",
               "REVIEW": "warn", "ALIVE": "good"}.get(res["status"], "info")
        emit("account", f"{r['name']}: {res['status']}{' · ZOMBIE in the data' if res.get('zombie') else ''} — {res['why']}",
             level=lvl, data={k: res.get(k) for k in ("row", "status", "zombie", "since")})
        return res

    with ThreadPoolExecutor(max_workers=workers) as ex:
        accounts = list(ex.map(one, rows))

    # One company under two names stays one record: merge CRM rows that resolve to the same canonical company
    from .matching import same_company
    kept: list[dict] = []
    for a in accounts:
        cid = (a.get("canonical") or {}).get("crustdata_company_id")
        twin = next((k for k in kept if (cid and (k.get("canonical") or {}).get("crustdata_company_id") == cid)
                     or same_company(k["input"]["name"], a["input"]["name"])), None)
        if not twin:
            kept.append(a)
            continue
        # keep the richer record (larger headcount) as the survivor
        if ((a.get("canonical") or {}).get("headcount_now") or 0) > ((twin.get("canonical") or {}).get("headcount_now") or 0):
            kept[kept.index(twin)] = a
            survivor, dup = a, twin
        else:
            survivor, dup = twin, a
        dup["merged_into_row"] = survivor["row"]
        emit("merge", f"Row {dup['row']} '{dup['input']['name']}' is the same company as row {survivor['row']} "
                      f"'{survivor['input']['name']}' (crustdata_company_id={(survivor.get('canonical') or {}).get('crustdata_company_id')}).")

    # Actions, each exactly once
    actions = []
    for a in accounts:
        crm = a.get("crm") or {}
        seq = crm.get("active_sequence_contacts") or crm.get("contacts_in_sequence") or "0"
        try:
            seq = int(float(seq))
        except ValueError:
            seq = 0
        todo = []
        if a.get("merged_into_row"):
            todo.append(("merge_duplicate", f"Merge row {a['row']} into row {a['merged_into_row']}"))
        if a["status"] in ("DEAD", "INSOLVENT") and seq:
            todo.append(("pause_sequence", f"Pause outreach to {seq} contact(s) at {a['input']['name']}"))
        if a["status"] in ("DEAD",):
            todo.append(("mark_closed", f"Mark {a['input']['name']} as closed (since {a.get('since') or 'unknown'})"))
        if a["status"] in ("ACQUIRED", "RENAMED"):
            todo.append(("update_identity", a["why"]))
        for kind, text in todo:
            key = idem_key(job_id, kind, a["input"]["name"], [str(a["row"])])
            res = act_once(key, {"kind": kind, "row": a["row"], "text": text})
            actions.append({"key": key, "kind": kind, "row": a["row"], "text": text, "result": res})
            emit("action", f"✔ {text} ({res})" if res == "done" else f"↺ {text} ({res})")

    # Correction feed for the data provider
    corrections = []
    for a in accounts:
        c = a.get("canonical") or {}
        if a["status"] in ("DEAD", "ACQUIRED", "RENAMED", "INSOLVENT") and c.get("crustdata_company_id")                 and not a.get("merged_into_row") and c.get("crustdata_status") in (None, "active", "operating"):
            corrections.append({
                "crustdata_company_id": c["crustdata_company_id"], "name": c.get("name"),
                "field": "basic_info.status", "current_value": c.get("crustdata_status"),
                "proposed_value": {"DEAD": "closed", "INSOLVENT": "insolvency_proceedings", "ACQUIRED": "acquired",
                                   "RENAMED": "renamed"}[a["status"]],
                "effective_since": a.get("since"),
                "people_still_listing_as_current": (a.get("people") or {}).get("still_current"),
                "confidence": a.get("confidence"),
                # only the evidence that decided the verdict, each with who said it and when
                "evidence": [{"says": s["says"], "publisher": s.get("publisher"), "tier": s.get("tier"),
                              "published": s.get("published"), "url": s.get("url"), "quote": s.get("quote"),
                              "retrieved_at": s["retrieved_at"]} for s in a["signals"] if s.get("used")][:8],
                "reason": a["why"],
            })

    # Files
    with open(jdir / "corrections.jsonl", "w", encoding="utf-8") as f:
        for c in corrections:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    cols = list((rows[0]["raw"].keys()) if rows else []) + ["pulse_status", "pulse_confidence", "pulse_since", "pulse_reason",
                                                           "pulse_sources", "crustdata_company_id", "merged_into_row"]
    with open(jdir / "cleaned.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for a in accounts:
            used = [s for s in a["signals"] if s.get("used") and s.get("url") and s["kind"] != "alive"]
            w.writerow({**(a.get("crm") or {}), "pulse_status": a["status"], "pulse_confidence": a.get("confidence") or "",
                        "pulse_since": a.get("since") or "", "pulse_reason": a["why"],
                        "pulse_sources": " ; ".join(f"{s.get('publisher')}"
                                                    f"{' (' + s['published'] + ')' if s.get('published') else ''}: {s['url']}"
                                                    for s in used)[:1500],
                        "crustdata_company_id": (a.get("canonical") or {}).get("crustdata_company_id") or "",
                        "merged_into_row": a.get("merged_into_row") or ""})
    counts = {}
    for a in accounts:
        if not a.get("merged_into_row"):
            counts[a["status"]] = counts.get(a["status"], 0) + 1
    zombies = [a for a in accounts if a.get("zombie") and not a.get("merged_into_row")]
    summary = {
        "accounts": len(accounts), "unique_companies": len(accounts) - sum(1 for a in accounts if a.get("merged_into_row")),
        "counts": counts, "zombies": len(zombies),
        "zombie_people_still_current": sum(int((a.get("people") or {}).get("still_current") or 0) for a in zombies),
        "actions_done": sum(1 for x in actions if x["result"] == "done"), "duplicates_merged": sum(1 for a in accounts if a.get("merged_into_row")),
        "contacts_paused": sum(int(x["text"].split(" to ")[1].split()[0]) for x in actions if x["kind"] == "pause_sequence"),
        "credits": log.credits, "api_calls": len(log.calls), "duration_s": round(time.time() - t0, 1),
    }
    report = {"job_id": job_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "summary": summary,
              "accounts": accounts, "actions": actions, "corrections": corrections}
    (jdir / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    emit("done", f"Done in {summary['duration_s']}s: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) +
         f" · {len(zombies)} zombie records · {summary['contacts_paused']} contacts paused",
         data={"job_id": job_id, "summary": summary})
    return report


def load(job_id: str) -> Optional[dict]:
    f = PULSE_DIR / job_id / "report.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
