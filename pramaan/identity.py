"""Resolve the resume to one person record, even for common names.

Strategy: profile URL on the resume -> enrich it and confirm the name matches.
Otherwise search by name and score every candidate by how many of the resume's
employers (and overlapping dates) appear in their history. A tie goes to a human.
"""
from __future__ import annotations

from typing import Optional

from . import crustdata as cd
from .dates import to_index
from .matching import name_similarity, same_company
from .models import ResumeClaims
from .normalize import Person, people_from_enrich, people_from_search


def _score(person: Person, claims: ResumeClaims) -> tuple[int, list[str]]:
    score, hits = 0, []
    for j in claims.jobs:
        js, je = to_index(j.start), to_index(j.end, is_end=True)
        for r in person.roles:
            if same_company(j.company, r.company) or (j.via_staffing_firm and same_company(j.via_staffing_firm, r.company)):
                score += 2
                rs, re_ = r.start, r.end
                if js is not None and rs is not None and abs(js - rs) <= 6:
                    score += 1
                if je is not None and re_ is not None and abs(je - re_) <= 6:
                    score += 1
                hits.append(r.company)
                break
    for e in claims.education:
        for s in person.schools:
            if same_company(e.school, s.get("school") or ""):
                score += 1
                break
    return score, hits


def resolve(claims: ResumeClaims, log=None, emit=lambda *a, **k: None) -> tuple[Optional[Person], dict]:
    info: dict = {"method": None, "status": "unresolved", "candidates_considered": 0, "notes": []}

    if claims.linkedin_url:
        u = claims.linkedin_url.strip().rstrip("/")
        if not u.lower().startswith("http"):
            u = "https://" + (u if u.lower().startswith("www.") else "www." + u)
        claims.linkedin_url = u
        emit("identity", f"Profile URL on resume: {claims.linkedin_url}. Enriching.")
        data, meta = cd.enrich_person(claims.linkedin_url, log)
        people = people_from_enrich(data) if data else []
        if meta.get("error"):
            info["notes"].append(f"Profile enrich failed: {meta['error']}")
        if people:
            p = people[0]
            sim = name_similarity(p.name, claims.name)
            info.update(method="profile_url", candidates_considered=1, name_similarity=sim,
                        profile_url=p.profile_url or claims.linkedin_url, person_id=p.person_id)
            if sim >= 75:
                score, hits = _score(p, claims)
                info.update(status="resolved", confidence="high", employer_hits=hits)
                emit("identity", f"Matched {p.name} via profile URL (name similarity {sim}%, {len(hits)} employers match).")
                return p, info
            info.update(status="mismatch", confidence="high")
            info["notes"].append(f"The profile URL on the resume belongs to '{p.name}', not '{claims.name}'.")
            emit("identity", f"⚠ Profile URL belongs to '{p.name}', not '{claims.name}'.", level="warn")
            return None, info

    # Search by exact name, then fuzzy name + employer
    emit("identity", f"Searching people named '{claims.name}'.")
    data, meta = cd.search_people({"field": "basic_profile.name", "type": "=", "value": claims.name}, 25, log)
    people = people_from_search(data)
    companies = [j.company for j in claims.jobs if j.company][:5]
    if len(people) < 1 and companies:
        data, meta = cd.search_people({"op": "and", "conditions": [
            {"field": "basic_profile.name", "type": "(.)", "value": claims.name},
            {"field": "experience.employment_details.company_name", "type": "in", "value": companies},
        ]}, 25, log)
        people = people_from_search(data)
    info["candidates_considered"] = len(people)
    if meta.get("error") and not people:
        info["notes"].append(f"Person search failed: {meta['error']}")
        return None, info
    if not people:
        info.update(status="not_found")
        emit("identity", "No matching person record found.", level="warn")
        return None, info

    scored = sorted(((_score(p, claims), p) for p in people), key=lambda x: -x[0][0])
    (best_score, best_hits), best = scored[0]
    runner = scored[1][0][0] if len(scored) > 1 else -1
    emit("identity", f"{len(people)} people named '{claims.name}'. Best match shares {len(best_hits)} employer(s) "
                     f"(score {best_score} vs next {max(runner, 0)}).")
    info.update(method="name_search", best_score=best_score, runner_up_score=runner, employer_hits=best_hits)

    if best_score < 3:
        # A shared employer alone is not identity: Infosys has ~300,000 people, many named Rahul Verma.
        info.update(status="not_found")
        info["notes"].append("People with this name exist, but none match the resume's employers AND dates. "
                             "No profile attached, rather than risk judging this candidate by a stranger's record.")
        return None, info
    if best_score - runner < 2 and runner >= 2:
        info.update(status="ambiguous")
        info["notes"].append("Two or more people with this name share employers with the resume. A human must pick.")
        info["ambiguous_candidates"] = [p.profile_url for _, p in scored[:3]]
        return None, info

    # Re-enrich for full history if we have a URL (search results can be partial)
    if best.profile_url:
        d2, _ = cd.enrich_person(best.profile_url, log)
        full = people_from_enrich(d2) if d2 else []
        if full:
            best = full[0]
    info.update(status="resolved", confidence="high" if best_score - runner >= 3 else "medium",
                profile_url=best.profile_url, person_id=best.person_id)
    return best, info
