"""Deterministic checks. Each returns Verdicts that cite their evidence and say who
controls that evidence (the candidate, the company, or a third party).

Principle: a claim only counts as VERIFIED when evidence the candidate cannot edit agrees.
Agreement with the candidate's own profile is only CONSISTENT.
"""
from __future__ import annotations

import re
from typing import Optional

from . import config
from . import crustdata as cd
from .dates import fmt, to_index, today_index, year_only
from .matching import same_company
from .models import ControlledBy as C, Evidence, JobClaim, ResumeClaims, Status as S, Verdict
from .normalize import Company, Person, Role, companies_from_enrich, people_from_search, stage_name, stage_rank
from .titles import is_exec_or_founder, normalize as norm_title, same_role, seniority

FUNCTION_KEYWORDS = {
    "engineering": r"engineer|developer|sde|swe|devops|sre|architect|programmer|technical",
    "product": r"product",
    "sales": r"sales|account executive|business development|bdr|sdr",
    "marketing": r"marketing|growth|brand|content",
    "operations": r"operations|ops\b",
    "design": r"design|ux|ui\b",
    "data": r"data|analytics|machine learning|ml\b|scientist",
    "hr": r"\bhr\b|people|talent|recruit",
    "finance": r"finance|accounting|accountant",
}


def pretty(s) -> str:
    s = re.sub(r"series_([a-z])", lambda m: "Series " + m.group(1).upper(), str(s or ""))
    return s.replace("pre_seed", "pre-seed").replace("_plus", "+").replace("_", " ")


def job_label(i: int, j: JobClaim) -> str:
    return f"{j.title} at {j.company} ({j.start or '?'} – {j.end or '?'})"


def _person_ev(p: Person, field: str, value: str, note: str = None) -> Evidence:
    return Evidence(source="crustdata:/person/enrich", record=f"person:{p.person_id or p.profile_url}",
                    field=field, value=value, controlled_by=C.CANDIDATE, url=p.profile_url, note=note)


def _company_ev(c: Company, field: str, value: str, note: str = None) -> Evidence:
    return Evidence(source="crustdata:/company/enrich", record=f"crustdata_company_id={c.company_id}",
                    field=field, value=value, controlled_by=C.COMPANY, note=note)


# ---------------- company lookup ----------------

class CompanyCache:
    def __init__(self, log):
        self.log, self.by_key = log, {}

    def get(self, name: str, company_id: Optional[int] = None, domain: Optional[str] = None) -> tuple[Optional[Company], Optional[str]]:
        key = company_id or domain or name.lower()
        if key in self.by_key:
            return self.by_key[key]
        data, meta = cd.enrich_company(name=name, domain=domain, company_id=company_id, log=self.log)
        result: tuple[Optional[Company], Optional[str]] = (None, meta.get("error"))
        if data:
            cands = companies_from_enrich(data)
            # name lookups can return look-alikes: require the name to actually match
            good = [c for c in cands if company_id or same_company(c.name, name) or (domain and c.domain == domain)]
            if good:
                good.sort(key=lambda c: -(c.match_confidence or 0))
                result = (good[0], None)
        self.by_key[key] = result
        return result


# ---------------- headcount estimation ----------------

def headcount_at(c: Company, month: int) -> tuple[Optional[int], str, str]:
    """(estimate, method, confidence). Uses a real timeseries if present; otherwise
    back-calculates from current headcount and trailing growth (up to 2 years)."""
    if c.timeseries:
        best = min(c.timeseries, key=lambda pt: abs(pt[0] - month))
        if abs(best[0] - month) <= 6:
            return best[1], f"headcount series value for {fmt(best[0])}", "high"
    if not c.headcount_total:
        return None, "no headcount data", "low"
    months_ago = today_index() - month
    g = c.growth_percent or {}
    options = [(0, None), (1, "mom"), (3, "qoq"), (6, "six_months"), (12, "yoy"), (24, "two_years")]
    best = None
    for span, key in options:
        if key is None:
            pct = 0.0
        else:
            pct = g.get(key)
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                continue
        if span <= months_ago + 3:
            best = (span, key, float(pct))
    if best is None:
        return c.headcount_total, "current headcount", "low"
    span, key, pct = best
    est = int(round(c.headcount_total / (1 + pct / 100.0))) if pct > -99 else c.headcount_total
    if months_ago - span > 6:
        # older than our growth data reaches. Only safe as an upper bound if the company has been growing.
        if pct >= 0:
            return est, f"≈{fmt(today_index() - span)} headcount (from {key} growth {pct:+.0f}%); company has been growing, so earlier headcount was likely lower", "medium"
        return None, "date is older than the growth data and the company has shrunk", "low"
    return est, f"current {c.headcount_total} adjusted by {key} growth {pct:+.0f}%", "medium" if key else "high"


def function_of(title: str) -> Optional[str]:
    t = norm_title(title)
    for fn, pat in FUNCTION_KEYWORDS.items():
        if re.search(pat, t):
            return fn
    return None


def function_share(c: Company, fn: Optional[str]) -> Optional[float]:
    if not fn or not c.by_role_absolute or not c.headcount_total:
        return None
    for k, v in c.by_role_absolute.items():
        if fn in str(k).lower() and isinstance(v, (int, float)) and v > 0:
            return min(1.0, v / c.headcount_total)
    return None


# ---------------- role matching ----------------

def match_role(job: JobClaim, person: Person, company: Optional[Company]) -> list[Role]:
    out = []
    for r in person.roles:
        if (company and company.company_id and r.company_id == company.company_id) or same_company(job.company, r.company) \
                or (job.via_staffing_firm and same_company(job.via_staffing_firm, r.company)):
            out.append(r)
    return out


def _overlap(a0, a1, b0, b1) -> int:
    a1 = a1 if a1 is not None else today_index()
    b1 = b1 if b1 is not None else today_index()
    return max(0, min(a1, b1) - max(a0, b0))


# ---------------- the checks ----------------

def check_employer(i: int, job: JobClaim, company: Optional[Company], err: Optional[str], log) -> list[Verdict]:
    cid = f"job{i}.employer"
    js = to_index(job.start)
    if company is None:
        # Not in the graph. Look for any public trace before calling it a ghost.
        data, meta = cd.web_search(f"\"{job.company}\" company", log)
        results = (data or {}).get("results") or [] if isinstance(data, dict) else []
        hits = [r for r in results if job.company.lower().split()[0] in (r.get("title", "") + r.get("snippet", "")).lower()]
        ev = [Evidence(source="crustdata:/company/enrich", field="names", value="no matching company record",
                       controlled_by=C.COMPANY, note=err)]
        if hits:
            ev += [Evidence(source="crustdata:/web/search/live", value=h.get("title"), url=h.get("url"),
                            controlled_by=C.THIRD_PARTY) for h in hits[:3]]
            return [Verdict(claim_id=cid, claim_text=f"Worked at {job.company}", check="employer_exists",
                            status=S.UNVERIFIED, confidence="medium",
                            reason=f"{job.company} is not in Crustdata's company graph but appears on the web. Needs confirmation from the employer.",
                            evidence=ev, next_action="email_employer")]
        if meta.get("error"):
            return [Verdict(claim_id=cid, claim_text=f"Worked at {job.company}", check="employer_exists",
                            status=S.UNVERIFIED, confidence="low",
                            reason=f"Could not check {job.company}: lookups failed ({meta['error']}).", evidence=ev,
                            next_action="retry_or_email")]
        return [Verdict(claim_id=cid, claim_text=f"Worked at {job.company}", check="employer_exists",
                        status=S.REVIEW, confidence="medium",
                        reason=f"No company record and no web presence found for '{job.company}'. Possible fake employer — "
                               f"or a very small or renamed firm. Candidate will be asked for an offer letter.",
                        evidence=ev, next_action="ask_candidate")]

    ev = [_company_ev(c := company, "basic_info.name", c.name)]
    if c.year_founded:
        ev.append(_company_ev(c, "basic_info.year_founded", str(c.year_founded)))
    if js is not None and c.year_founded:
        start_year = js // 12
        if start_year <= c.year_founded - 2:
            return [Verdict(claim_id=cid, claim_text=f"Worked at {job.company} from {job.start}", check="employer_exists",
                            status=S.DISCREPANCY, confidence="high",
                            reason=f"Claimed start {job.start}, but {c.name} was founded in {c.year_founded} — "
                                   f"{c.year_founded - start_year} years later.", evidence=ev, next_action="ask_candidate")]
        if start_year == c.year_founded - 1:
            return [Verdict(claim_id=cid, claim_text=f"Worked at {job.company} from {job.start}", check="employer_exists",
                            status=S.REVIEW, confidence="low",
                            reason=f"Claimed start {job.start} is the year before {c.name}'s recorded founding ({c.year_founded}). "
                                   f"Possible for a pre-incorporation founding team; a human should confirm.", evidence=ev)]
    return [Verdict(claim_id=cid, claim_text=f"{job.company} exists", check="employer_exists", status=S.VERIFIED,
                    confidence="high", reason=f"{c.name} is a real company" +
                    (f", founded {c.year_founded}, before the claimed start." if c.year_founded else "."), evidence=ev)]


def check_against_profile(i: int, job: JobClaim, person: Optional[Person], company: Optional[Company]) -> list[Verdict]:
    label = job_label(i, job)
    if person is None:
        return [Verdict(claim_id=f"job{i}.role", claim_text=label, check="profile_match", status=S.UNVERIFIED,
                        confidence="low", reason="Candidate's identity could not be resolved, so their history could not be compared.",
                        next_action="email_employer")]
    roles = match_role(job, person, company)
    js, je = to_index(job.start), to_index(job.end, is_end=True)
    if not roles:
        # what was the candidate doing during that time, by their own account?
        others = [r for r in person.roles if js is not None and r.start is not None and
                  _overlap(js, je, r.start, r.end) >= 6 and (r.employment_type or "").lower() not in ("part-time", "freelance")]
        ev = [_person_ev(person, "experience.employment_details", f"{len(person.roles)} roles; none at {job.company}")]
        if others:
            ev += [_person_ev(person, "experience.employment_details", f"{r.title} at {r.company} ({fmt(r.start)} – {fmt(r.end) if r.end else 'present'})")
                   for r in others[:3]]
            return [Verdict(claim_id=f"job{i}.role", claim_text=label, check="profile_match", status=S.REVIEW,
                            confidence="medium",
                            reason=f"{job.company} is missing from the candidate's own profile, which shows them at "
                                   f"{', '.join(sorted({r.company for r in others}))} during that period.",
                            evidence=ev, next_action="email_employer")]
        return [Verdict(claim_id=f"job{i}.role", claim_text=label, check="profile_match", status=S.UNVERIFIED,
                        confidence="low", reason=f"{job.company} is not on the candidate's profile; nothing contradicts it either.",
                        evidence=ev, next_action="email_employer")]

    # pick the profile role that best overlaps the claimed period (handles promotions)
    if js is not None:
        roles.sort(key=lambda r: -_overlap(js, je, r.start or js, r.end))
    span_start = min((r.start for r in roles if r.start is not None), default=None)
    span_end = None if any(r.end is None for r in roles) else max((r.end for r in roles if r.end is not None), default=None)
    out: list[Verdict] = []

    # ---- dates: compare against the whole tenure at the company ----
    tol = 11 if (year_only(job.start) or year_only(job.end)) else config.DATE_TOLERANCE_MONTHS
    ev = [_person_ev(person, "employment_details[].start_date/end_date",
                     f"{roles[0].company}: {fmt(span_start)} – {fmt(span_end) if span_end else 'present'}")]
    problems = []
    if js is not None and span_start is not None and span_start - js > tol:
        problems.append(f"start moved earlier by {span_start - js} months ({job.start} vs {fmt(span_start)})")
    job_is_current = je is not None and je >= today_index() - 1
    if je is not None and not job_is_current and span_end is not None and je - span_end > tol:
        problems.append(f"end moved later by {je - span_end} months ({job.end} vs {fmt(span_end)})")
    if job_is_current and span_end is not None and today_index() - span_end > max(tol, 3):
        problems.append(f"resume says current, but profile shows they left in {fmt(span_end)}")
    if problems:
        stretch = (span_start - js if js is not None and span_start is not None else 0) + \
                  ((je - span_end) if je is not None and span_end is not None and not job_is_current else 0)
        out.append(Verdict(claim_id=f"job{i}.dates", claim_text=f"{job.company}: {job.start} – {job.end}", check="dates",
                           status=S.DISCREPANCY, confidence="high" if stretch > 6 or job_is_current else "medium",
                           reason="Dates differ from the candidate's own profile: " + "; ".join(problems) + ".",
                           evidence=ev, next_action="ask_candidate"))
    else:
        out.append(Verdict(claim_id=f"job{i}.dates", claim_text=f"{job.company}: {job.start} – {job.end}", check="dates",
                           status=S.CONSISTENT, confidence="medium",
                           reason=f"Dates match the candidate's profile within {tol} months.", evidence=ev))

    # ---- title ----
    titles = [r.title for r in roles if r.title]
    best = next((t for t in titles if same_role(job.title, t)), None)
    tev = [_person_ev(person, "employment_details[].title", "; ".join(titles) or "(none)")]
    if best:
        out.append(Verdict(claim_id=f"job{i}.title", claim_text=f"Title: {job.title}", check="title", status=S.CONSISTENT,
                           confidence="medium", reason=f"Matches profile title '{best}'.", evidence=tev))
    elif titles:
        top = max(titles, key=seniority)
        rs, ps = seniority(job.title), seniority(top)
        if rs > ps:
            out.append(Verdict(claim_id=f"job{i}.title", claim_text=f"Title: {job.title}", check="title", status=S.DISCREPANCY,
                               confidence="high" if rs - ps >= 2 else "medium",
                               reason=f"Resume title '{job.title}' is more senior than anything on the profile at {job.company} "
                                      f"(highest: '{top}').", evidence=tev, next_action="ask_candidate"))
        else:
            out.append(Verdict(claim_id=f"job{i}.title", claim_text=f"Title: {job.title}", check="title", status=S.CONSISTENT,
                               confidence="low", reason=f"Worded differently from profile ('{top}') but not more senior.", evidence=tev))
    return out


def check_team_size(i: int, job: JobClaim, company: Optional[Company], person: Optional[Person] = None) -> list[Verdict]:
    if not job.team_size_claim:
        return []
    n = job.team_size_claim
    cid, text = f"job{i}.team_size", f"Led a team of {n} at {job.company}"
    # Does the candidate's own profile even show a people-management role there?
    if person and n >= 3:
        titles = [r.title for r in match_role(job, person, company) if r.title]
        if titles and max(seniority(t) for t in titles) < 5:
            return [Verdict(claim_id=cid, claim_text=text, check="team_size_feasibility", status=S.DISCREPANCY,
                            confidence="medium",
                            reason=f"Claims to have led {n} people, but every title on the candidate's own profile at "
                                   f"{job.company} is an individual-contributor role ({'; '.join(titles)}).",
                            evidence=[_person_ev(person, "employment_details[].title", "; ".join(titles))],
                            next_action="ask_candidate")]
    if company is None:
        return [Verdict(claim_id=cid, claim_text=text, check="team_size_feasibility", status=S.UNVERIFIED, confidence="low",
                        reason="No company record to test this against.", next_action="email_employer")]
    js, je = to_index(job.start), to_index(job.end, is_end=True)
    when = min(je if je is not None else today_index(), today_index())
    est, how, conf = headcount_at(company, when)
    if est is None:
        return [Verdict(claim_id=cid, claim_text=text, check="team_size_feasibility", status=S.UNVERIFIED, confidence="low",
                        reason=f"Not enough headcount history to test this ({how}).",
                        evidence=[_company_ev(company, "headcount.total", str(company.headcount_total))], next_action="email_employer")]
    ev = [_company_ev(company, "headcount.total", str(company.headcount_total)),
          _company_ev(company, "headcount.growth_percent", str(company.growth_percent), note=how)]
    fn = function_of(job.title)
    share = function_share(company, fn)
    if share:
        ev.append(_company_ev(company, f"headcount.by_role_absolute.{fn}", f"{share:.0%} of company"))
    if n >= est:
        return [Verdict(claim_id=cid, claim_text=text, check="team_size_feasibility", status=S.DISCREPANCY,
                        confidence="high" if conf != "low" else "medium",
                        reason=f"Claimed {n} reports, but {company.name} had about {est} employees in total around {fmt(when)}.",
                        evidence=ev, next_action="ask_candidate")]
    limit = est if is_exec_or_founder(job.title) else int(est * (share or 0.5))
    if n > max(limit, 3):
        return [Verdict(claim_id=cid, claim_text=text, check="team_size_feasibility", status=S.REVIEW, confidence="medium",
                        reason=f"Claimed {n} reports; {company.name} had about {est} employees" +
                               (f", ~{limit} in {fn}" if share else "") + f" around {fmt(when)}. Possible but unusual for a '{job.title}'.",
                        evidence=ev, next_action="email_employer")]
    return [Verdict(claim_id=cid, claim_text=text, check="team_size_feasibility", status=S.CONSISTENT, confidence="medium",
                    reason=f"Plausible: {company.name} had about {est} employees around {fmt(when)}.", evidence=ev)]


def _stage_at(company: Company, month: int) -> tuple[Optional[int], Optional[str], list]:
    """Highest funding stage reached at least 2 months before `month`."""
    best, best_raw, used = None, None, []
    for r in company.rounds:
        if r.rank is not None and r.date is not None and r.date <= month - 2:
            used.append(r)
            if best is None or r.rank > best:
                best, best_raw = r.rank, f"{r.round_type} ({r.date_raw})"
    if best is None and company.last_round_type and company.last_fundraise_date:
        d = to_index(company.last_fundraise_date)
        rk = stage_rank(company.last_round_type)
        if d is not None and rk is not None and d <= month - 2:
            best, best_raw = rk, f"{company.last_round_type} ({company.last_fundraise_date})"
    return best, best_raw, used


def check_stage(i: int, job: JobClaim, company: Optional[Company]) -> list[Verdict]:
    out = []
    js = to_index(job.start)
    if company is None or js is None:
        return out
    claimed = stage_rank(job.joined_stage_claim) if job.joined_stage_claim else None
    if claimed is not None:
        cid, text = f"job{i}.stage", f"Joined {job.company} at {stage_name(claimed)} stage" + (f" ('{job.stage_claim_text}')" if job.stage_claim_text else "")
        actual, raw, used = _stage_at(company, js)
        ev = [_company_ev(company, "funding.funding_rounds", f"{r.round_type} on {r.date_raw}") for r in company.rounds[:6]] or \
             [_company_ev(company, "funding.last_round_type", f"{company.last_round_type} ({company.last_fundraise_date})")]
        if not company.rounds and not company.last_round_type:
            out.append(Verdict(claim_id=cid, claim_text=text, check="funding_stage", status=S.UNVERIFIED, confidence="low",
                               reason="No funding history on record for this company.", evidence=ev))
        elif actual is not None and actual > claimed:
            out.append(Verdict(claim_id=cid, claim_text=text, check="funding_stage", status=S.DISCREPANCY, confidence="high",
                               reason=f"{company.name} had already raised {pretty(raw)} before the candidate joined in {job.start}, "
                                      f"so it was at {pretty(stage_name(actual))} stage, not {pretty(stage_name(claimed))}.",
                               evidence=ev, next_action="ask_candidate"))
        else:
            out.append(Verdict(claim_id=cid, claim_text=text, check="funding_stage", status=S.VERIFIED, confidence="medium",
                               reason=f"Funding history agrees: latest round before {job.start} was {pretty(raw) or 'none'}.", evidence=ev))

    if job.early_employee_claim:
        k = job.early_employee_claim
        cid, text = f"job{i}.early_employee", f"Employee #{k} at {job.company}"
        ev = []
        reasons = []
        actual, raw, _ = _stage_at(company, js)
        if actual is not None and actual >= 4:
            reasons.append(f"the company had already raised {raw} when they joined")
        if company.year_founded:
            ev.append(_company_ev(company, "basic_info.year_founded", str(company.year_founded)))
            yrs = js // 12 - company.year_founded
            if yrs >= 4 and k <= 15:
                reasons.append(f"they joined {yrs} years after founding ({company.year_founded})")
        est, how, conf = headcount_at(company, js)
        if est and conf != "low" and est > max(5 * k, k + 40):
            reasons.append(f"headcount was about {est} at that time ({how})")
            ev.append(_company_ev(company, "headcount", str(est), note=how))
        if reasons:
            out.append(Verdict(claim_id=cid, claim_text=text, check="early_employee", status=S.DISCREPANCY if len(reasons) >= 2 else S.REVIEW,
                               confidence="high" if len(reasons) >= 2 else "medium",
                               reason="Unlikely to be employee #" + str(k) + ": " + "; ".join(reasons) + ".", evidence=ev,
                               next_action="ask_candidate"))
        else:
            out.append(Verdict(claim_id=cid, claim_text=text, check="early_employee", status=S.CONSISTENT, confidence="low",
                               reason="Nothing in company history contradicts this.", evidence=ev))
    return out


def check_colleagues(i: int, job: JobClaim, company: Optional[Company], person: Optional[Person], log) -> list[Verdict]:
    """Third-party evidence: do other people's profiles show this team existing at that time?"""
    if company is None:
        return []
    js, je = to_index(job.start), to_index(job.end, is_end=True)
    if js is None:
        return []
    fn = function_of(job.title)
    conds = [{"field": "experience.employment_details.company_name", "type": "in", "value": [company.name]}]
    kw = {"engineering": "engineer", "product": "product", "sales": "sales", "marketing": "marketing", "data": "data",
          "design": "design", "operations": "operations", "hr": "talent", "finance": "finance"}.get(fn)
    if kw:
        conds.append({"field": "experience.employment_details.title", "type": "(.)", "value": kw})
    def is_self(p):
        return bool(person) and ((p.person_id and p.person_id == person.person_id) or
                                 (p.profile_url and p.profile_url == person.profile_url))

    def overlapping_in(people):
        found = []
        for p in people:
            if is_self(p):
                continue
            for r in p.roles:
                if same_company(r.company, company.name) and r.start is not None and _overlap(js, je, r.start, r.end) >= 3 \
                        and (fn is None or function_of(r.title) == fn):
                    found.append((p, r))
                    break
        return found

    data, meta = cd.search_people({"op": "and", "conditions": conds} if len(conds) > 1 else conds[0], 25, log)
    people = people_from_search(data)
    overlapping = overlapping_in(people)
    if not overlapping and len(conds) > 1:
        # titles vary ('SDE 2', 'MTS'): widen to everyone at the company and classify titles ourselves
        data2, meta2 = cd.search_people(conds[0], 25, log)
        more = people_from_search(data2)
        people += more
        overlapping = overlapping_in(more)
        meta = meta if not meta.get("error") else meta2
    cid, text = f"job{i}.colleagues", f"A {fn or 'similar'} team existed at {company.name} during {job.start} – {job.end}"
    if meta.get("error"):
        return [Verdict(claim_id=cid, claim_text=text, check="colleague_corroboration", status=S.UNVERIFIED, confidence="low",
                        reason=f"Colleague search failed: {meta['error']}")]
    ev = [Evidence(source="crustdata:/person/search", record=f"person:{p.person_id or 'n/a'}",
                   field="employment_details", value=f"{r.title} ({fmt(r.start)} – {fmt(r.end) if r.end else 'present'})",
                   controlled_by=C.THIRD_PARTY, url=p.profile_url, note="name withheld in report")
          for p, r in overlapping[:5]]
    if overlapping:
        return [Verdict(claim_id=cid, claim_text=text, check="colleague_corroboration", status=S.VERIFIED, confidence="medium",
                        reason=f"{len(overlapping)} other {'person' if len(overlapping) == 1 else 'people'}'s profile{'' if len(overlapping) == 1 else 's'} show {fn or 'similar'} roles at {company.name} overlapping this period.",
                        evidence=ev)]
    big = (company.headcount_total or 0) >= 200
    return [Verdict(claim_id=cid, claim_text=text, check="colleague_corroboration",
                    status=S.REVIEW if big and kw else S.UNVERIFIED, confidence="low",
                    reason=(f"No one else's profile shows {'an' if (fn or 'x')[0] in 'aeiou' else 'a'} {fn} role at {company.name} overlapping this period, "
                            f"despite ~{company.headcount_total} employees." if big and kw else
                            f"No overlapping colleagues found in a sample of {len(people)} profiles."),
                    evidence=ev, next_action="email_employer")]


def check_overlaps(claims: ResumeClaims) -> list[Verdict]:
    out = []
    skip = ("intern", "part", "freelance", "contract", "advisor", "consult", "volunteer")
    jobs = [(i, j, to_index(j.start), to_index(j.end, is_end=True)) for i, j in enumerate(claims.jobs, 1)]
    for a in range(len(jobs)):
        for b in range(a + 1, len(jobs)):
            ia, ja, sa, ea = jobs[a]
            ib, jb, sb, eb = jobs[b]
            if None in (sa, sb) or same_company(ja.company, jb.company):
                continue
            if any(s in (x.employment_type or "").lower() + " " + x.title.lower() for x in (ja, jb) for s in skip):
                continue
            ov = _overlap(sa, ea, sb, eb)
            if ov > config.OVERLAP_TOLERANCE_MONTHS:
                out.append(Verdict(claim_id=f"job{ia}+job{ib}.overlap",
                                   claim_text=f"{ja.company} and {jb.company} at the same time",
                                   check="overlap", status=S.REVIEW, confidence="medium",
                                   reason=f"Two full-time roles overlap by {ov} months. Could be a side project or a dating error.",
                                   evidence=[Evidence(source="resume", controlled_by=C.INTERNAL,
                                                      value=f"{ja.start}–{ja.end} vs {jb.start}–{jb.end}")],
                                   next_action="ask_candidate"))
    return out


def check_education(claims: ResumeClaims, person: Optional[Person]) -> list[Verdict]:
    out = []
    for k, e in enumerate(claims.education, 1):
        cid, text = f"edu{k}", f"{e.degree or 'Studied'} at {e.school}" + (f" ({e.end_year})" if e.end_year else "")
        if person is None:
            out.append(Verdict(claim_id=cid, claim_text=text, check="education", status=S.UNVERIFIED, confidence="low",
                               reason="Identity unresolved.", next_action="verify_with_institution"))
            continue
        match = next((s for s in person.schools if same_company(e.school, s.get("school") or "")), None)
        if not match:
            out.append(Verdict(claim_id=cid, claim_text=text, check="education", status=S.UNVERIFIED, confidence="low",
                               reason="Not listed on the candidate's profile.", next_action="verify_with_institution"))
            continue
        ev = [_person_ev(person, "education.schools[]", f"{match.get('school')} {match.get('degree') or ''} {match.get('end_year') or ''}")]
        if e.end_year and match.get("end_year") and abs(int(match["end_year"]) - e.end_year) > 1:
            out.append(Verdict(claim_id=cid, claim_text=text, check="education", status=S.DISCREPANCY, confidence="medium",
                               reason=f"Graduation year {e.end_year} vs {match['end_year']} on profile.", evidence=ev))
        else:
            out.append(Verdict(claim_id=cid, claim_text=text, check="education", status=S.CONSISTENT, confidence="medium",
                               reason="Matches the candidate's profile. Degree itself needs the institution to confirm.",
                               evidence=ev, next_action="verify_with_institution"))
    return out


def check_references(claims: ResumeClaims, log) -> list[Verdict]:
    out = []
    for k, ref in enumerate(claims.references, 1):
        if not ref.company:
            continue
        cid, text = f"ref{k}", f"Reference {ref.name} ({ref.relationship or 'colleague'} at {ref.company})"
        data, meta = cd.search_people({"field": "basic_profile.name", "type": "=", "value": ref.name}, 10, log)
        people = [p for p in people_from_search(data) if any(same_company(r.company, ref.company) for r in p.roles)]
        job = next((j for j in claims.jobs if same_company(j.company, ref.company)), None)
        if not people:
            out.append(Verdict(claim_id=cid, claim_text=text, check="reference", status=S.UNVERIFIED, confidence="low",
                               reason=f"No profile found for {ref.name} at {ref.company}.", next_action="email_reference"))
            continue
        p = people[0]
        r = next(r for r in p.roles if same_company(r.company, ref.company))
        ev = [Evidence(source="crustdata:/person/search", record=f"person:{p.person_id}", field="employment_details",
                       value=f"{r.title} at {r.company} ({fmt(r.start)} – {fmt(r.end) if r.end else 'present'})",
                       controlled_by=C.THIRD_PARTY, url=p.profile_url)]
        if job and r.start is not None and to_index(job.start) is not None and \
                _overlap(to_index(job.start), to_index(job.end, is_end=True), r.start, r.end) <= 0:
            out.append(Verdict(claim_id=cid, claim_text=text, check="reference", status=S.DISCREPANCY, confidence="medium",
                               reason=f"{ref.name} was at {ref.company} {fmt(r.start)} – {fmt(r.end) if r.end else 'present'}, "
                                      f"which never overlaps the candidate's time there.", evidence=ev))
        else:
            out.append(Verdict(claim_id=cid, claim_text=text, check="reference", status=S.VERIFIED, confidence="medium",
                               reason=f"{ref.name} really worked at {ref.company} during the candidate's time.", evidence=ev,
                               next_action="email_reference"))
    return out
