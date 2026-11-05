"""Map raw Crustdata responses into small, predictable records.

Written defensively: field names are from the 2025-11-01 docs, with fallbacks for
anything missing, so an unexpected response shape degrades a check instead of crashing it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .dates import to_index

STAGE_ORDER = ["bootstrapped", "pre_seed", "seed", "series_a", "series_b", "series_c", "series_d_plus", "public"]


def stage_rank(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    t = s.lower().replace("-", "_").replace(" ", "_")
    if "pre_seed" in t or "angel" in t:
        return 1
    if "seed" in t:
        return 2
    m = re.search(r"series_([a-z])", t)
    if m:
        letter = m.group(1)
        return {"a": 3, "b": 4, "c": 5}.get(letter, 6)
    if "ipo" in t or "public" in t or "post_ipo" in t:
        return 7
    if "bootstrap" in t:
        return 0
    return None  # debt, grant, undisclosed... -> not a stage


def stage_name(rank: Optional[int]) -> str:
    return STAGE_ORDER[rank] if rank is not None and 0 <= rank < len(STAGE_ORDER) else "unknown"


def dig(d: Any, path: str, default=None):
    cur = d
    for p in path.split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur


@dataclass
class Role:
    company: str
    title: str
    start_raw: Optional[str]
    end_raw: Optional[str]
    start: Optional[int]
    end: Optional[int]               # None = current
    company_id: Optional[int] = None
    domain: Optional[str] = None
    employment_type: Optional[str] = None
    headcount_latest: Optional[int] = None
    is_current: bool = False


@dataclass
class Person:
    person_id: Optional[str]
    name: str
    profile_url: Optional[str]
    headline: Optional[str]
    location: Optional[str]
    roles: list[Role] = field(default_factory=list)
    schools: list[dict] = field(default_factory=list)
    match_confidence: Optional[float] = None


@dataclass
class FundingRound:
    date: Optional[int]
    date_raw: Optional[str]
    round_type: Optional[str]
    rank: Optional[int]
    amount_usd: Optional[float]


@dataclass
class Company:
    company_id: Optional[int]
    name: str
    domain: Optional[str]
    year_founded: Optional[int]
    status: Optional[str]
    headcount_total: Optional[int]
    growth_percent: dict
    by_role_absolute: dict
    timeseries: list[tuple[int, int]]   # (month index, headcount) if the API ever returns it
    rounds: list[FundingRound]
    company_type: Optional[str]
    industries: list
    last_round_type: Optional[str]
    last_fundraise_date: Optional[str]
    match_confidence: Optional[float] = None


def _role(r: dict, current: bool) -> Role:
    end_raw = r.get("end_date")
    return Role(
        company=r.get("name") or r.get("company_name") or "",
        title=r.get("title") or "",
        start_raw=r.get("start_date"),
        end_raw=end_raw,
        start=to_index(r.get("start_date")),
        end=None if (current or not end_raw) else to_index(end_raw, is_end=True),
        company_id=r.get("crustdata_company_id"),
        domain=r.get("company_website_domain"),
        employment_type=r.get("employment_type"),
        headcount_latest=r.get("company_headcount_latest"),
        is_current=current,
    )


def person_from(pd: dict, confidence: Optional[float] = None) -> Person:
    emp = dig(pd, "experience.employment_details", {}) or {}
    roles = [_role(r, True) for r in (emp.get("current") or [])] + \
            [_role(r, False) for r in (emp.get("past") or [])]
    loc = dig(pd, "basic_profile.location")
    if isinstance(loc, dict):
        loc = loc.get("raw") or loc.get("full_location")
    return Person(
        person_id=str(pd.get("crustdata_person_id") or "") or None,
        name=dig(pd, "basic_profile.name") or pd.get("name") or "",
        profile_url=dig(pd, "social_handles.professional_network_identifier.profile_url"),
        headline=dig(pd, "basic_profile.headline"),
        location=loc,
        roles=roles,
        schools=dig(pd, "education.schools", []) or [],
        match_confidence=confidence,
    )


def people_from_enrich(data: Any) -> list[Person]:
    out = []
    for entry in data or []:
        for m in entry.get("matches") or []:
            if m.get("person_data"):
                out.append(person_from(m["person_data"], m.get("confidence_score")))
    return out


def people_from_search(data: Any) -> list[Person]:
    if not isinstance(data, dict):
        return []
    return [person_from(p) for p in data.get("profiles") or []]


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    for k, v in d.items():  # fuzzy fallback on key names
        for want in keys:
            if want.split("_")[0] in k.lower() and v not in (None, ""):
                return v
    return None


def _rounds(funding: dict) -> list[FundingRound]:
    out = []
    raw_rounds = funding.get("funding_rounds") or funding.get("milestones") or []
    if isinstance(raw_rounds, dict):
        raw_rounds = list(raw_rounds.values())
    for r in raw_rounds:
        if not isinstance(r, dict):
            continue
        rt = _first(r, "round_type", "funding_type", "type", "round_name", "stage", "series")
        dt = _first(r, "date", "announced_date", "announced_on", "funding_date", "round_date")
        amt = _first(r, "amount_usd", "money_raised_usd", "amount", "raised_amount_usd")
        try:
            amt = float(amt) if amt is not None else None
        except (TypeError, ValueError):
            amt = None
        out.append(FundingRound(to_index(str(dt)) if dt else None, str(dt) if dt else None,
                                str(rt) if rt else None, stage_rank(str(rt)) if rt else None, amt))
    out.sort(key=lambda x: (x.date is None, x.date or 0))
    return out


def _timeseries(headcount: dict) -> list[tuple[int, int]]:
    """Find any list of {date, count} entries anywhere under headcount."""
    for v in headcount.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            pts = []
            for e in v:
                d = _first(e, "date", "month", "period")
                n = _first(e, "employee_count", "headcount", "count", "total", "value")
                if d is not None and isinstance(n, (int, float)):
                    idx = to_index(str(d))
                    if idx is not None:
                        pts.append((idx, int(n)))
            if pts:
                return sorted(pts)
    return []


def companies_from_enrich(data: Any) -> list[Company]:
    out = []
    for entry in data or []:
        for m in entry.get("matches") or []:
            cd = m.get("company_data") or {}
            bi = cd.get("basic_info") or {}
            hc = cd.get("headcount") or {}
            fu = cd.get("funding") or {}
            yf = bi.get("year_founded")
            try:
                yf = int(str(yf)[:4]) if yf else None
            except ValueError:
                yf = None
            out.append(Company(
                company_id=cd.get("crustdata_company_id"),
                name=bi.get("name") or "",
                domain=bi.get("primary_domain"),
                year_founded=yf,
                status=bi.get("status"),
                headcount_total=hc.get("total"),
                growth_percent=hc.get("growth_percent") or {},
                by_role_absolute=hc.get("by_role_absolute") or {},
                timeseries=_timeseries(hc) if isinstance(hc, dict) else [],
                rounds=_rounds(fu) if isinstance(fu, dict) else [],
                company_type=bi.get("company_type"),
                industries=[str(x) for x in (dig(cd, "taxonomy.professional_network_industries") or
                                             [dig(cd, "taxonomy.professional_network_industry")]) if x],
                last_round_type=fu.get("last_round_type"),
                last_fundraise_date=fu.get("last_fundraise_date"),
                match_confidence=m.get("confidence_score"),
            ))
    return out
