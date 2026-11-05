"""Resume in -> finished, cited report out."""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable, Optional

from . import checks, config, identity, llm, outreach
from .crustdata import CallLog
from .models import ControlledBy as C, Evidence, Report, ResumeClaims, Status as S, Verdict


Emit = Callable[..., None]


def _overall(r: Report) -> str:
    st = [v.status for v in r.verdicts]
    if any(v.status == S.DISCREPANCY and v.confidence == "high" for v in r.verdicts):
        return "discrepancies_found"
    if S.DISCREPANCY in st or S.REVIEW in st:
        return "needs_review"
    if S.PENDING in st or S.UNVERIFIED in st:
        return "awaiting_responses"
    return "clear"


def summarize(r: Report) -> str:
    counts = {s: sum(1 for v in r.verdicts if v.status == s) for s in S}
    parts = [f"{counts[S.VERIFIED]} verified independently", f"{counts[S.CONSISTENT]} consistent with profile",
             f"{counts[S.DISCREPANCY]} discrepancies", f"{counts[S.REVIEW]} for human review",
             f"{counts[S.PENDING]} awaiting replies", f"{counts[S.UNVERIFIED]} unverified"]
    return ", ".join(p for p in parts if not p.startswith("0 "))


def save(r: Report):
    (config.RUNS_DIR / f"{r.run_id}.json").write_text(r.model_dump_json(indent=1), encoding="utf-8")


def load(run_id: str) -> Optional[Report]:
    f = config.RUNS_DIR / f"{run_id}.json"
    return Report.model_validate_json(f.read_text(encoding="utf-8")) if f.exists() else None


def verify(data: bytes, filename: str, *, consent: bool, emit: Emit = lambda *a, **k: None,
           claims: Optional[ResumeClaims] = None, run_id: Optional[str] = None, do_outreach: bool = True) -> Report:
    t0 = time.time()
    log = CallLog()
    run_id = run_id or uuid.uuid4().hex[:12]
    report = Report(run_id=run_id, candidate="(reading resume)", consent_recorded=consent)

    # 1. Read the resume
    if claims is None:
        emit("extract", f"Reading {filename} with Claude…")
        try:
            claims = llm.extract_claims(data, filename)
        except llm.LLMError as e:
            report.overall, report.summary = "failed", f"Could not read the resume: {e}"
            emit("error", report.summary, level="error")
            save(report)
            return report
    report.claims, report.candidate = claims, claims.name
    emit("extract", f"Found {len(claims.jobs)} roles, {len(claims.education)} degrees, {len(claims.references)} references "
                    f"for {claims.name}.", data={"jobs": [j.model_dump() for j in claims.jobs]})
    for n in claims.extraction_notes:
        emit("extract", f"Note on the document: {n}", level="warn")

    # 2. Who is this?
    person, info = identity.resolve(claims, log, emit)
    report.identity = info
    if info.get("status") == "mismatch":
        report.verdicts.append(Verdict(claim_id="identity", claim_text=f"Profile URL belongs to {claims.name}",
                                       check="identity", status=S.DISCREPANCY, confidence="high",
                                       reason=info["notes"][-1],
                                       evidence=[Evidence(source="crustdata:/person/enrich", url=claims.linkedin_url,
                                                          controlled_by=C.THIRD_PARTY, value=info["notes"][-1])],
                                       next_action="ask_candidate"))
    elif info.get("status") == "ambiguous":
        report.verdicts.append(Verdict(claim_id="identity", claim_text=f"Identity of {claims.name}", check="identity",
                                       status=S.REVIEW, confidence="medium", reason=info["notes"][-1],
                                       evidence=[Evidence(source="crustdata:/person/search", controlled_by=C.THIRD_PARTY,
                                                          url=u, value="possible match") for u in info.get("ambiguous_candidates", [])]))

    # 3. Check every claim
    cache = checks.CompanyCache(log)
    for i, job in enumerate(claims.jobs, 1):
        emit("check", f"Checking role {i}: {checks.job_label(i, job)}")
        roles = checks.match_role(job, person, None) if person else []
        cid = next((r.company_id for r in roles if r.company_id), None)
        dom = next((r.domain for r in roles if r.domain), None)
        company, err = cache.get(job.company, cid, dom)
        if company is None and job.via_staffing_firm:
            company, err = cache.get(job.via_staffing_firm)
        vs: list[Verdict] = []
        for fn in (lambda: checks.check_employer(i, job, company, err, log),
                   lambda: checks.check_against_profile(i, job, person, company),
                   lambda: checks.check_team_size(i, job, company, person),
                   lambda: checks.check_stage(i, job, company),
                   lambda: checks.check_colleagues(i, job, company, person, log)):
            try:
                vs += fn()
            except Exception as e:  # noqa: BLE001 - one broken check must never sink the report
                vs.append(Verdict(claim_id=f"job{i}.error", claim_text=checks.job_label(i, job), check="internal",
                                  status=S.REVIEW, confidence="low", reason=f"A check failed unexpectedly: {type(e).__name__}: {e}"))
        for v in vs:
            emit("verdict", f"{v.status.value.upper()}: {v.claim_text} — {v.reason}", data=v.model_dump(mode="json"),
                 level={"discrepancy": "bad", "review": "warn", "verified": "good"}.get(v.status.value, "info"))
        report.verdicts += vs

    for fn in (lambda: checks.check_overlaps(claims), lambda: checks.check_education(claims, person),
               lambda: checks.check_references(claims, log)):
        try:
            for v in fn():
                emit("verdict", f"{v.status.value.upper()}: {v.claim_text} — {v.reason}", data=v.model_dump(mode="json"),
                     level={"discrepancy": "bad", "review": "warn", "verified": "good"}.get(v.status.value, "info"))
                report.verdicts.append(v)
        except Exception as e:  # noqa: BLE001
            emit("error", f"Check failed: {e}", level="warn")

    # 4. Outreach for what the data couldn't settle
    if do_outreach:
        report.outreach = outreach.plan_and_send(report, log, emit)

    report.overall = _overall(report)
    report.summary = summarize(report)
    report.duration_s = round(time.time() - t0, 1)
    report.credits_used = log.credits
    report.identity["api_calls"] = len(log.calls)
    save(report)
    emit("done", f"Done in {report.duration_s}s: {report.summary}", data={"overall": report.overall})
    return report


# ---------- Closing the loop: replies and documents ----------

def apply_reply(run_id: str, outreach_id: str, reply_text: str) -> Report:
    r = load(run_id)
    rec = next(o for o in r.outreach if o["id"] == outreach_id)
    reading = llm.read_reply(rec["subject"], "\n".join(rec["questions"]), reply_text)
    ev = Evidence(source=f"email reply ({rec['kind']}: {rec.get('company') or r.candidate})", record=f"outreach:{outreach_id}",
                  value=reading.quote, controlled_by=C.THIRD_PARTY if rec["kind"] == "employer" else C.CANDIDATE,
                  note=reading.summary)
    for v in r.verdicts:
        if v.claim_id in rec["claim_ids"]:
            v.evidence.append(ev)
            if not reading.is_clear:
                v.status, v.reason = S.REVIEW, f"Reply was unclear: {reading.summary}"
            elif reading.contradicts_claim or reading.confirms_employment is False:
                v.status, v.confidence, v.reason = S.DISCREPANCY, "high", f"Employer reply contradicts the claim: {reading.summary}"
            elif reading.confirms_employment:
                v.status, v.confidence, v.reason = S.VERIFIED, "high", f"Confirmed by employer: {reading.summary}"
            v.next_action = None
    rec["reply"] = {"text": reply_text, "reading": reading.model_dump()}
    r.overall, r.summary = _overall(r), summarize(r)
    save(r)
    return r


def apply_document(run_id: str, claim_id: str, data: bytes, filename: str) -> Report:
    r = load(run_id)
    v = next(v for v in r.verdicts if v.claim_id == claim_id)
    reading = llm.read_document(v.claim_text, data, filename)
    v.evidence.append(Evidence(source=f"candidate upload: {filename}", value=reading.quote, controlled_by=C.CANDIDATE,
                               note=f"{reading.document_type} from {reading.issuer or '?'}; tamper signals: "
                                    f"{', '.join(reading.tamper_signals) or 'none seen'}"))
    if reading.supports_claim and not reading.tamper_signals:
        v.status, v.confidence = S.REVIEW, "low"
        v.reason = f"Candidate provided a {reading.document_type} that supports the claim ({reading.summary}). Human to sign off."
    elif reading.supports_claim is False:
        v.reason += f" Candidate's own document also contradicts it: {reading.summary}"
    else:
        v.status = S.REVIEW
        v.reason = f"Candidate provided a document; needs a human look: {reading.summary}"
    v.next_action = "human_signoff"
    r.overall, r.summary = _overall(r), summarize(r)
    save(r)
    return r
