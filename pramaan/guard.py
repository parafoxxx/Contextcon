"""Pre-send guard: an outreach agent asks Pulse before it emails anyone.

    GET /api/guard?to=priya@builder.ai   ->  {"decision": "block", "why": "...", "sources": [...], "instead": [...]}

Answers come from the latest stored verdict for that company (any finished CRM run or single check), so the
call is instant and free. Unknown companies can be checked live with check=true.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from .matching import same_company

DECISION = {  # what an agent should do with a message to this company
    "DEAD": "block", "INSOLVENT": "hold", "DISTRESSED": "hold", "REVIEW": "hold", "CANT_TELL": "hold",
    "ACQUIRED": "update", "RENAMED": "update", "ALIVE": "allow",
}
ADVICE = {
    "block": "Don't send. The company has shut down; nobody will receive it.",
    "hold": "Hold. A person should look before anyone sends this.",
    "update": "Send only after updating the company name or owner in the message.",
    "allow": "OK to send.",
    "unknown": "Pulse has no verdict for this company yet. Call again with check=true to check it now.",
}
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me", "protonmail.com",
             "rediffmail.com", "live.com", "aol.com"}


def _dom(x: Optional[str]) -> str:
    return (x or "").strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]


def parse_target(to: str) -> tuple[Optional[str], Optional[str]]:
    """'priya@builder.ai' -> (None, 'builder.ai');  'builder.ai' -> (None, 'builder.ai');  'Koo' -> ('Koo', None)"""
    t = (to or "").strip()
    if "@" in t:
        return None, _dom(t.rsplit("@", 1)[1])
    if "." in t and " " not in t:
        return None, _dom(t)
    return (t or None), None


def accounts_from(reports_dir: Path) -> Iterable[dict]:
    """Every stored verdict, newest first: CRM runs (report.json) and single checks (one_*.json)."""
    files = list(reports_dir.glob("*/report.json")) + list(reports_dir.glob("one_*.json"))
    for f in sorted(files, key=lambda p: -p.stat().st_mtime):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for a in data.get("accounts", [data]):
            if a.get("status") and not a.get("merged_into_row"):
                yield {**a, "_from": data.get("job_id") or f.stem}


def lookup(name: Optional[str], domain: Optional[str], accounts: Iterable[dict]) -> Optional[dict]:
    """The newest verdict for this company: exact domain first, then strict name match (never fuzzy)."""
    for a in accounts:
        cn, inp = a.get("canonical") or {}, a.get("input") or {}
        doms = {_dom(cn.get("domain")), _dom(inp.get("domain"))} - {""}
        if domain and (domain in doms or any(domain.endswith("." + d) for d in doms)):
            return a
        if name and any(same_company(name, n) for n in (inp.get("name"), cn.get("name")) if n):
            return a
    return None


def answer(to: str, a: Optional[dict]) -> dict:
    name, domain = parse_target(to)
    if domain in FREE_MAIL:
        return {"to": to, "decision": "unknown", "advice": "Personal email address: no company to check.", "status": None}
    if not a:
        return {"to": to, "company": name or domain, "decision": "unknown", "advice": ADVICE["unknown"], "status": None}
    d = DECISION.get(a["status"], "hold")
    alumni = ((a.get("people") or {}).get("alumni") or {}).get("top_destinations") or []
    out = {
        "to": to, "company": (a.get("canonical") or {}).get("name") or a["input"]["name"],
        "decision": d, "advice": ADVICE[d], "status": a["status"], "since": a.get("since"), "why": a.get("why"),
        "confidence_score": a.get("confidence_score"),
        "sources": [{"publisher": s.get("publisher"), "published": s.get("published"), "url": s.get("url"),
                     "says": s.get("says")} for s in a.get("signals", []) if s.get("used") and s.get("url")][:5],
        "checked_at": a.get("checked_at"), "verdict_from": a.get("_from"),
    }
    if d == "block" and alumni:
        out["instead"] = [{"company": x["company"], "domain": x.get("domain"), "ex_employees_there": x["count"]}
                          for x in alumni[:3]]
    return out
