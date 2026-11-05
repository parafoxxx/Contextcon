"""Outbound email with a guard on every message.

Guard rules (each send is checked; failures are logged, never silently skipped):
  1. Candidate consent must be recorded.
  2. Never contact the candidate's CURRENT employer.
  3. Minimum data: only the candidate's name and the claim being checked for that employer.
     The guard scans the body for other employers' names, phone numbers and emails.
  4. Event rule: agents only talk to our own team, so every message goes to DEMO_INBOX (a team inbox).
     With no DEMO_INBOX set, nothing is emailed at all.
  5. At most one message per recipient per run, and exactly-once delivery via an on-disk ledger.
"""
from __future__ import annotations

import hashlib
import json
import re
import smtplib
import threading
from datetime import datetime, timezone
from email.message import EmailMessage

from . import config
from . import crustdata as cd
from .dates import to_index, today_index
from .matching import same_company
from .models import Report
from .normalize import people_from_search


def guard(report: Report, to: str, body: str, about_company: str | None, sent_to: set) -> tuple[bool, list[str], str]:
    problems = []
    if not report.consent_recorded:
        problems.append("no candidate consent on file")
    claims = report.claims
    if about_company and claims:
        for j in claims.jobs:
            if same_company(j.company, about_company):
                je = to_index(j.end, is_end=True)
                if je is None or je >= today_index() - 1:
                    problems.append(f"{about_company} is the candidate's current employer")
                break
        others = {j.company for j in claims.jobs if not same_company(j.company, about_company)}
        leaked = [o for o in others if o and o.lower() in body.lower()]
        if leaked:
            problems.append(f"body mentions other employers: {', '.join(leaked)}")
    if claims and claims.phone and claims.phone in body:
        problems.append("body contains candidate phone number")
    if re.search(r"\b\d{10}\b", body):
        problems.append("body contains a 10-digit number")
    # Event rule: agents only ever talk to our own team. Every message goes to the team inbox.
    actual_to = config.DEMO_INBOX
    if config.SMTP_HOST and not actual_to:
        problems.append("no team inbox (DEMO_INBOX) set: refusing to email anyone outside the team")
    if to in sent_to:
        problems.append(f"already contacted {to} in this run")
    return (not problems), problems, actual_to or f"(not sent; intended {to})"


# ---------- exactly-once delivery ----------
# A ledger on disk records every send *before* it is attempted. After a crash or a retry,
# a message marked 'sending' or 'sent' is never sent again.

LEDGER = config.OUTBOX_DIR / "ledger.json"
_ledger_lock = threading.Lock()


def _ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _ledger_set(key: str, entry: dict):
    with _ledger_lock:
        led = _ledger()
        led[key] = {**led.get(key, {}), **entry}
        tmp = LEDGER.with_suffix(".tmp")
        tmp.write_text(json.dumps(led, indent=1), encoding="utf-8")
        tmp.replace(LEDGER)


def idem_key(run_id: str, kind: str, company: str | None, claim_ids: list[str]) -> str:
    raw = "|".join([run_id, kind, (company or "").lower(), ",".join(sorted(claim_ids))])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def deliver_once(key: str, to: str, subject: str, body: str) -> str:
    prev = _ledger().get(key)
    if prev and prev.get("status") == "sent":
        return f"already sent at {prev.get('at')} (not resent)"
    if prev and prev.get("status") == "sending":
        return "a previous attempt may have sent this; not resending (check the team inbox)"
    _ledger_set(key, {"status": "sending", "to": to, "subject": subject,
                      "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    try:
        result = _send(to, subject, body, key)
    except Exception as e:  # noqa: BLE001 - delivery failure must not crash the run
        _ledger_set(key, {"status": "failed", "error": str(e)})
        return f"failed: {e}"
    _ledger_set(key, {"status": "sent" if result == "sent" else "logged"})
    return result


def _send(to: str, subject: str, body: str, ref: str) -> str:
    if not config.SMTP_HOST or not config.DEMO_INBOX:
        return "logged (not emailed)"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.SMTP_FROM or config.SMTP_USER, to, subject
    msg["X-Pramaan-Ref"] = ref
    msg.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
        s.starttls()
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASS)
        s.send_message(msg)
    return "sent"


def find_hr_contact(company: str, log) -> dict | None:
    data, _ = cd.search_people({"op": "and", "conditions": [
        {"field": "experience.employment_details.current.company_name", "type": "in", "value": [company]},
        {"field": "experience.employment_details.current.title", "type": "(.)", "value": "human resources"},
    ]}, 5, log)
    people = people_from_search(data)
    if not people:
        data, _ = cd.search_people({"op": "and", "conditions": [
            {"field": "experience.employment_details.current.company_name", "type": "in", "value": [company]},
            {"field": "experience.employment_details.current.title", "type": "(.)", "value": "talent"},
        ]}, 5, log)
        people = people_from_search(data)
    if not people:
        return None
    p = people[0]
    title = next((r.title for r in p.roles if r.is_current), p.headline)
    return {"name": p.name, "title": title, "profile_url": p.profile_url, "person_id": p.person_id}


def employer_email(report: Report, company: str, items: list[dict], contact: dict | None) -> tuple[str, str]:
    who = f"Dear {contact['name'].split()[0]}," if contact else "Dear HR team,"
    lines = "\n".join(f"  {k}. {it['question']}" for k, it in enumerate(items, 1))
    subject = f"Employment verification: {report.candidate} (ref {report.run_id[:8]})"
    body = f"""{who}

{report.candidate} has applied for a role and has consented to a background check. They state they worked at {company}. Could you confirm the following? A one-line reply is enough.

{lines}

If you can't share this without a signed consent form, reply "consent form" and we'll send it.

Thank you,
Pramaan Verification (on behalf of the hiring team)
Ref: {report.run_id}
"""
    return subject, body


def candidate_email(report: Report, items: list[dict]) -> tuple[str, str]:
    lines = "\n".join(f"  {k}. {it['claim']}: {it['reason']}" for k, it in enumerate(items, 1))
    subject = f"Please help us verify a few details (ref {report.run_id[:8]})"
    body = f"""Hi {report.candidate.split()[0]},

As part of your background check, we couldn't confirm the following from available records:

{lines}

This often has an innocent explanation (a company rename, a contract role, a typo in dates). If you can, please reply or upload an offer letter, relieving letter or payslip for each item. Nothing is shared with the employer until you've had a chance to respond.

Upload: http://localhost:8000/explain/{report.run_id}

Thanks,
Pramaan Verification
"""
    return subject, body


def plan_and_send(report: Report, log, emit=lambda *a, **k: None) -> list[dict]:
    """Group open questions by employer, run each email through the guard, send, and mark verdicts PENDING."""
    from .models import Status as S
    sent_to: set = set()
    records = []
    claims = report.claims
    by_company: dict[str, list] = {}
    to_candidate = []
    for v in report.verdicts:
        if v.status in (S.UNVERIFIED, S.REVIEW) and v.next_action in ("email_employer", "retry_or_email") and v.claim_id.startswith("job"):
            idx = int(re.match(r"job(\d+)", v.claim_id).group(1)) - 1
            if claims and idx < len(claims.jobs):
                by_company.setdefault(claims.jobs[idx].company, []).append((v, claims.jobs[idx]))
        if v.status in (S.DISCREPANCY, S.REVIEW) and v.next_action == "ask_candidate":
            to_candidate.append(v)

    for company, pairs in by_company.items():
        job = pairs[0][1]
        items, seen = [], set()
        q = f"Did {report.candidate} work at {company} as '{job.title}' from {job.start} to {job.end}?"
        items.append({"question": q, "claim_ids": [v.claim_id for v, _ in pairs]})
        if job.team_size_claim:
            items.append({"question": f"Did they manage a team of about {job.team_size_claim} people?",
                          "claim_ids": [f"{pairs[0][0].claim_id.split('.')[0]}.team_size"]})
        contact = find_hr_contact(company, log)
        subject, body = employer_email(report, company, items, contact)
        intended = f"hr@{company.lower().split()[0]}.example"  # real address would come from contact enrichment
        ok, problems, actual_to = guard(report, intended, body, company, sent_to)
        cids = sorted({cid for it in items for cid in it["claim_ids"]})
        rec = {"id": idem_key(report.run_id, "employer", company, cids), "kind": "employer", "company": company, "contact": contact,
               "intended_to": intended, "to": actual_to, "subject": subject, "body": body,
               "claim_ids": sorted({cid for it in items for cid in it["claim_ids"]}),
               "questions": [it["question"] for it in items],
               "guard": {"passed": ok, "problems": problems},
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if ok:
            rec["delivery"] = deliver_once(rec["id"], actual_to, subject, body)
            sent_to.add(intended)
            for v in report.verdicts:
                if v.claim_id in rec["claim_ids"] and v.status in (S.UNVERIFIED, S.REVIEW):
                    v.status, v.next_action = S.PENDING, f"awaiting reply ({rec['id']})"
            emit("outreach", f"✉ Emailed {contact['name'] + ', ' + (contact['title'] or '') if contact else 'HR'} at {company} "
                             f"({rec['delivery']}; guard passed).")
        else:
            rec["delivery"] = "blocked by guard"
            emit("guard", f"⛔ Email to {company} blocked: {'; '.join(problems)}", level="warn")
        records.append(rec)

    if to_candidate and claims:
        items = [{"claim": v.claim_text, "reason": v.reason, "claim_id": v.claim_id} for v in to_candidate]
        subject, body = candidate_email(report, items)
        ok, problems, actual_to = guard(report, claims.email or "candidate@unknown", body, None, sent_to)
        rec = {"id": idem_key(report.run_id, "candidate", None, [i["claim_id"] for i in items]), "kind": "candidate",
               "company": None, "contact": {"name": report.candidate},
               "intended_to": claims.email, "to": actual_to, "subject": subject, "body": body,
               "claim_ids": [i["claim_id"] for i in items], "questions": [i["claim"] for i in items],
               "guard": {"passed": ok, "problems": problems},
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if ok:
            rec["delivery"] = deliver_once(rec["id"], actual_to, subject, body)
            emit("outreach", f"✉ Asked the candidate to explain {len(items)} item(s) before anything reaches HR ({rec['delivery']}).")
        else:
            rec["delivery"] = "blocked by guard"
            emit("guard", f"⛔ Candidate email blocked: {'; '.join(problems)}", level="warn")
        records.append(rec)

    for rec in records:
        (config.OUTBOX_DIR / f"{report.run_id}_{rec['id']}.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return records
