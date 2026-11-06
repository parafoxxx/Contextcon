"""Pulse: is this company still alive?  Finds zombie companies in a CRM or a data graph.

For each company: resolve one canonical Crustdata record, gather independent evidence
(court filings, press, an exodus of employees, headcount collapse, website status), decide
ALIVE / DISTRESSED / DEAD / ACQUIRED / CANT_TELL by rules, and cite every fact with its date.
The LLM only reads news snippets; rules decide.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from . import companies, config, crustdata as cd, llm, sources
from .dates import fmt, to_index, today_index
from .matching import same_company
from .normalize import companies_from_enrich, people_from_search

NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
FAILURE_WORDS = re.compile(r"shut|insolv|bankrupt|liquidat|collapse|wind(s|ing)? down|ceas|halt|NCLT|administration", re.I)
CREDIBLE = ("official", "news", "trade")


class Signal(BaseModel):
    kind: str            # legal | press | exodus | headcount_drop | website | hiring_freeze | stale_funding | alive
    strength: str        # strong | support
    says: str            # human sentence
    source: str
    url: Optional[str] = None
    record: Optional[str] = None
    value: Optional[str] = None
    date: Optional[str] = None
    publisher: Optional[str] = None   # e.g. economictimes.com, or "Crustdata" for data signals
    tier: Optional[str] = None        # official | news | weak | data
    published: Optional[str] = None   # article date when the result states one
    quote: Optional[str] = None       # the source's own words
    used: bool = False                # True if the rule that set the status relied on it
    retrieved_at: str = Field(default_factory=NOW)


class NewsReading(BaseModel):
    about_this_company: bool = Field(description="False if the results are about a different company with a similar name")
    company_role: Literal["subject", "creditor_or_petitioner", "mentioned", "unclear"] = Field(
        description="In any insolvency/court item: is THIS company the one being wound up (subject), the one suing or owed "
                    "money (creditor_or_petitioner), or just mentioned?")
    ceased_operations: bool = Field(description="Results state THIS company shut down, halted operations, or ceased trading")
    insolvency_or_bankruptcy: bool = Field(description="Results state THIS company entered insolvency, bankruptcy, administration or liquidation")
    acquired_by: Optional[str] = Field(None, description="Acquirer, if results state the company was acquired")
    acquisition_still_current: bool = Field(False, description="True only if the company is STILL owned by that acquirer today: "
                                            "False if results report a later separation, spin-off, demerger or buy-back")
    renamed_to: Optional[str] = Field(None, description="New name, if results state the company was renamed")
    layoffs_only: bool = Field(description="Results mention layoffs but no shutdown or insolvency")
    subsidiary_or_solvent_only: bool = Field(
        description="True if the only insolvency/liquidation item concerns a subsidiary or separate legal entity, or is a "
                    "SOLVENT voluntary liquidation (a restructuring), not the main company failing")
    event_date: Optional[str] = Field(None, description="Month and year of the main event if stated, e.g. 'May 2025'")
    supporting_results: list[int] = Field(default_factory=list, description="Indexes of the results that state the main event")
    quote: str = Field(description="Exact words from a result stating the main event, or ''")


NEWS_SYSTEM = """You read web search results about one company. Report only what the results state, and quote them.
Be careful: a company that FILES an insolvency case against someone, or is OWED money, is a creditor, not insolvent.
A company that ACQUIRES others was not acquired. Report the CURRENT state: if a company was acquired and later separated,
spun off or demerged, it is independent today. A subsidiary's liquidation, or a SOLVENT voluntary liquidation, is a
restructuring, not the main company failing. Shutting down one product, app feature, office or business line is NOT
the company shutting down: only set ceased_operations if the whole company stopped operating.
Layoffs alone are not a shutdown. Different companies often share a name: a result about a company with a different
website, country or business from the one described is NOT this company. Use only results about this company, and set
about_this_company=false if none are."""


# ---------------- evidence gathering ----------------

def _crustdata(name: str, domain: Optional[str], cache: companies.CompanyCache, log):
    cid = cache.canonical_id(name, domain)
    if not cid:
        return None, None
    data, meta = cd._post("/company/enrich", {"crustdata_company_ids": [cid],
                                              "fields": ["basic_info", "headcount", "funding", "hiring"]}, log)
    if not data:
        return None, meta.get("error")
    comps = companies_from_enrich(data)
    raw = (data[0].get("matches") or [{}])[0].get("company_data") or {}
    return (comps[0] if comps else None), raw


def headcount_signals(c, raw) -> list[Signal]:
    out = []
    rec = f"crustdata_company_id={c.company_id}"
    ts = c.timeseries
    if ts:
        last_m, last_n = ts[-1]
        window = [(m, n) for m, n in ts if m >= last_m - 24]
        pm, pn = max(window, key=lambda x: x[1])
        drop = (pn - last_n) / pn if pn else 0
        if drop >= 0.2:
            out.append(Signal(kind="headcount_drop", strength="strong", source="crustdata:/company/enrich", record=rec,
                              value=f"{pn:,} → {last_n:,}", date=fmt(pm),
                              says=f"Headcount fell {drop:.0%} from its peak of {pn:,} ({fmt(pm)}) to {last_n:,} ({fmt(last_m)})."))
        else:
            yr = [(m, n) for m, n in ts if m >= last_m - 12]
            g = (last_n - yr[0][1]) / yr[0][1] if yr and yr[0][1] else 0
            out.append(Signal(kind="alive", strength="support", source="crustdata:/company/enrich", record=rec,
                              value=f"{yr[0][1]:,} → {last_n:,}", says=f"Headcount steady over 12 months ({g:+.0%}).",
                              date=fmt(last_m)))
    hi = raw.get("hiring") or {}
    if hi.get("openings_count") in (0, None) and c.headcount_total and c.headcount_total > 100:
        out.append(Signal(kind="hiring_freeze", strength="support", source="crustdata:/company/enrich", record=rec,
                          value=str(hi.get("openings_count")), says="No open job postings on record."))
    elif hi.get("openings_count"):
        out.append(Signal(kind="alive", strength="support", source="crustdata:/company/enrich", record=rec,
                          value=str(hi["openings_count"]), says=f"{hi['openings_count']} open job postings."))
    if c.last_fundraise_date:
        d = to_index(c.last_fundraise_date)
        if d is not None and today_index() - d >= 30:
            out.append(Signal(kind="stale_funding", strength="support", source="crustdata:/company/enrich", record=rec,
                              value=f"{c.last_round_type} {c.last_fundraise_date}", date=c.last_fundraise_date,
                              says=f"Last funding round ({c.last_round_type}) was {(today_index() - d) // 12} years ago."))
    return out


def alumni_destinations(people, name: str, top: int = 5, per: int = 3) -> dict:
    """Where ex-employees work now, from profiles already fetched (no extra API call).
    A dead account's contacts are lost; the companies its people moved to are new leads."""
    by: dict[str, dict] = {}
    moved = 0
    for p in people:
        if not any(same_company(r.company, name) and not r.is_current for r in p.roles):
            continue
        now = [r for r in p.roles if r.is_current and r.company and not same_company(r.company, name)]
        if not now:
            continue
        moved += 1
        r = now[0]
        key = (r.company_id and f"id:{r.company_id}") or r.company.lower().strip()
        d = by.setdefault(key, {"company": r.company, "domain": r.domain, "count": 0, "people": []})
        d["count"] += 1
        if len(d["people"]) < per:
            d["people"].append({"name": p.name, "title": r.title, "profile_url": p.profile_url})
    ranked = sorted(by.values(), key=lambda d: -d["count"])
    return {"sampled": len(people), "moved": moved, "companies": len(ranked), "top_destinations": ranked[:top]}


def exodus_signal(name: str, log) -> tuple[Optional[Signal], dict]:
    """Departures in the last 18 months vs the 18 before, from Crustdata people data; and who still lists it as current."""
    cur, _ = cd.search_people({"field": "experience.employment_details.current.company_name", "type": "in", "value": [name]}, 1, log)
    past, _ = cd.search_people({"field": "experience.employment_details.past.company_name", "type": "in", "value": [name]}, 50, log)
    ends = []
    past_people = people_from_search(past)
    for p in past_people:
        for r in p.roles:
            if same_company(r.company, name) and r.end:
                ends.append(r.end)
    now = today_index()
    recent = sum(1 for e in ends if now - 18 <= e <= now)
    before = sum(1 for e in ends if now - 36 <= e < now - 18)
    spike_month = None
    if ends:
        months = Counter(e for e in ends if now - 18 <= e <= now)
        if months:
            spike_month = fmt(max(months, key=months.get))
    stats = {"still_current": (cur or {}).get("total_count"), "listed_as_past": (past or {}).get("total_count"),
             "sample_departures_recent_18m": recent, "sample_departures_prior_18m": before, "peak_departure_month": spike_month,
             "alumni": alumni_destinations(past_people, name)}
    if recent >= 10 and recent >= 2 * max(before, 1):
        return Signal(kind="exodus", strength="strong", source="crustdata:/person/search", value=f"{recent} vs {before}",
                      date=spike_month,
                      says=f"Exodus: {recent} departures in the last 18 months vs {before} in the 18 before (sample of "
                           f"{len(ends)} ex-employees), peaking {spike_month}."), stats
    return None, stats


def website_signal(domain: Optional[str]) -> Optional[Signal]:
    if not domain:
        return None
    url = "https://" + domain.strip().removeprefix("https://").removeprefix("http://").strip("/")
    try:
        r = httpx.get(url, timeout=8, follow_redirects=True, headers={"user-agent": "Mozilla/5.0 (Pulse data-freshness check)"})
        final = urlparse(str(r.url)).netloc.lower().removeprefix("www.")
        orig = urlparse(url).netloc.lower().removeprefix("www.")
        if final and orig and final.split(".")[-2:] != orig.split(".")[-2:]:
            return Signal(kind="website", strength="strong", source="http", url=str(r.url), value=f"redirects to {final}",
                          says=f"{orig} now redirects to {final}.")
        if r.status_code >= 500 or r.status_code == 404:
            return Signal(kind="website", strength="support", source="http", url=url, value=f"HTTP {r.status_code}",
                          says=f"Website returns HTTP {r.status_code}.")
        text = r.text[:20000].lower()
        if re.search(r"domain (is )?for sale|this domain may be for sale|parked|buy this domain", text):
            return Signal(kind="website", strength="strong", source="http", url=url, value="parked", says="Website is a parked / for-sale domain.")
        return Signal(kind="alive", strength="support", source="http", url=str(r.url), value=f"HTTP {r.status_code}",
                      says="Website is up.")
    except httpx.HTTPError as e:
        return Signal(kind="website", strength="strong", source="http", url=url, value=type(e).__name__,
                      says=f"Website unreachable ({type(e).__name__}).")


def news_signals(name: str, log, who: str = "") -> tuple[list[Signal], Optional[NewsReading], list[dict]]:
    data, _ = cd.web_search(f"{name} shut down OR insolvency OR bankruptcy OR liquidation OR acquired", log)
    results = [r for r in ((data or {}).get("results") or []) if isinstance(r, dict)][:8]
    # Any hint of failure -> also search the insolvency court's own site, so an official record can be cited
    if any(FAILURE_WORDS.search(f"{r.get('title', '')} {r.get('snippet', '')}") for r in results):
        court, _ = cd.web_search(f"{name} insolvency", log, site="nclt.gov.in")
        key = name.lower().split()[0]
        results += [r for r in ((court or {}).get("results") or [])[:3]
                    if isinstance(r, dict) and key in f"{r.get('title', '')} {r.get('snippet', '')}".lower()]
    if not results:
        return [], None, []
    cited = [sources.cite(r) for r in results]
    listing = "\n".join(f"[{i}] ({c['publisher']}, {c['tier']}{', ' + c['published'] if c['published'] else ''}) "
                        f"{c['title']} | {c['snippet'][:300]}" for i, c in enumerate(cited))
    reading = None
    for attempt in range(2):  # the model is often busy under parallel load: wait and try once more
        try:
            reading = llm._parse(NewsReading, NEWS_SYSTEM, f"Company: {name}{who}\n\nResults:\n{listing}")
            break
        except llm.LLMError as e:
            err = str(e)
            if not attempt:
                time.sleep(8)
    if reading is None:
        return [Signal(kind="unread", strength="support", source="llm", value=err[:200], publisher="model", tier="data",
                       says=f"{len(cited)} news results found but the model could not read them ({err[:80]}).")], None, cited
    if not reading.about_this_company:
        return [], reading, cited
    idx = [i for i in reading.supporting_results if 0 <= i < len(cited)] or [0]

    def sig(kind, i, says, strength="strong"):
        c = cited[i]
        return Signal(kind=kind, strength=strength, source="crustdata:/web/search/live", url=c["url"], value=c["title"][:140],
                      date=reading.event_date, publisher=c["publisher"], tier=c["tier"], published=c["published"],
                      quote=c["snippet"][:280], says=says)

    title = lambda i: cited[i]["title"][:110]  # noqa: E731
    # A court's cause list is its daily hearing schedule naming hundreds of companies: not a record about this one
    cause_list = lambda c: bool(re.search(r"cause[\s_%20-]*list", f"{c['url']} {c['title']}", re.I))  # noqa: E731
    official = [i for i, c in enumerate(cited) if c["tier"] == "official" and not cause_list(c)]
    out: list[Signal] = []
    if reading.insolvency_or_bankruptcy and reading.subsidiary_or_solvent_only:
        out.append(sig("restructuring", idx[0], f"Subsidiary / solvent liquidation only, not a failure: “{title(idx[0])}”", "support"))
    elif reading.insolvency_or_bankruptcy and reading.company_role in ("subject", "unclear"):
        for i in official[:1]:
            out.append(sig("legal", i, f"Official court / registry record: “{title(i)}”"))
        for i in idx[:4]:
            if i not in official and not cause_list(cited[i]):
                out.append(sig("insolvency", i, f"Insolvency / bankruptcy reported: “{title(i)}”"))
    elif official and reading.company_role == "creditor_or_petitioner":
        out.append(sig("court_mention", official[0], "Named in a court case as creditor / petitioner, not as the company being wound up.",
                       "support"))
    if reading.ceased_operations:
        for i in idx[:4]:
            out.append(sig("shutdown", i, f"Shutdown reported: “{title(i)}”"))
    if reading.acquired_by and not reading.acquisition_still_current:
        out.append(sig("separated", idx[0], f"Was owned by {reading.acquired_by} but has since separated: “{title(idx[0])}”", "support"))
    if reading.acquired_by and reading.acquisition_still_current:
        for i in idx[:4]:
            out.append(sig("acquired", i, f"Acquired by {reading.acquired_by}: “{title(i)}”"))
    if reading.renamed_to and not same_company(reading.renamed_to, name):  # 'Zepto' -> 'Zepto Private Limited' is no rename
        for i in idx[:2]:
            out.append(sig("renamed", i, f"Renamed to {reading.renamed_to}: “{title(i)}”"))
    if reading.layoffs_only:
        out.append(sig("layoffs", idx[0], "Layoffs reported (not a shutdown).", "support"))
    return out, reading, cited


# ---------------- decision ----------------

def _pubs(signals, kind) -> list[Signal]:
    """One signal per distinct credible publisher (official or news) for this kind of event."""
    seen, out = set(), []
    for x in signals:
        if x.kind == kind and x.tier in CREDIBLE and x.publisher and x.publisher not in seen:
            seen.add(x.publisher)
            out.append(x)
    return out


def enough(pubs: list[Signal]) -> bool:
    """Independent confirmation: 2+ distinct publishers incl. an official or major outlet, or 3+ other publications."""
    return (len(pubs) >= 2 and any(x.tier in ("official", "news") for x in pubs)) or len(pubs) >= 3


def _union(*groups: list[Signal]) -> list[Signal]:
    seen, out = set(), []
    for g in groups:
        for x in g:
            if x.publisher not in seen:
                seen.add(x.publisher)
                out.append(x)
    return out


def _named(sigs: list[Signal]) -> str:
    return " and ".join(f"{x.publisher}{' (' + x.published + ')' if x.published else ''}" for x in sigs[:3])


def decide(signals: list[Signal], reading: Optional[NewsReading], found: bool) -> tuple[str, str, str, set]:
    """-> (status, a reason that names its sources, confidence, the kinds of evidence the rule relied on)"""
    k = Counter(s.kind for s in signals)
    if not found and not signals:
        return "CANT_TELL", "No company record and no public trace.", "low", set()
    openings = any(s.kind == "alive" and "open job postings" in s.says for s in signals)
    site_up = any(s.kind == "alive" and s.source == "http" for s in signals)
    alive_veto = openings and site_up  # signs of life: open jobs AND a working website
    corr = [(n, kind) for n, kind, ok in (("website down", "website", k["website"] > 0),
                                          ("hiring stopped", "hiring_freeze", k["hiring_freeze"] > 0),
                                          ("headcount collapse", "headcount_drop", k["headcount_drop"] > 0),
                                          ("employee exodus", "exodus", k["exodus"] > 0)) if ok]
    corr_txt, corr_kinds = ", ".join(n for n, _ in corr), {kind for _, kind in corr}
    weak = lambda kind: sum(1 for x in signals if x.kind == kind and x.tier == "weak")  # noqa: E731
    legal, shut, insol, acq, ren = (_pubs(signals, x) for x in ("legal", "shutdown", "insolvency", "acquired", "renamed"))

    if ren:
        return "RENAMED", f"{ren[0].says.split(':')[0]}, reported by {_named(ren)}.", "high" if len(ren) >= 2 else "medium", {"renamed"}
    if acq and not shut:
        redirect = any(s.kind == "website" and "redirects" in (s.value or "") for s in signals)
        if enough(acq) or redirect:
            return "ACQUIRED", f"{acq[0].says.split(':')[0]}, reported by {_named(acq)}" + \
                ("; the website now redirects." if redirect else "."), "high" if len(acq) >= 3 or redirect else "medium", \
                {"acquired", "website"}
        return "REVIEW", f"Only one credible source ({_named(acq)}) reports an acquisition. A person decides.", "low", {"acquired"}
    failure = _union(shut, insol)
    if shut and (legal or enough(failure)):
        shut = failure
        if alive_veto:
            return "REVIEW", f"{_named(shut)} report a shutdown, but the company still has open jobs and a working website. " \
                             "A person decides.", "low", {"shutdown", "alive"}
        if corr:
            conf = "high" if (legal or len(shut) >= 3) else "medium"
            return "DEAD", f"Shutdown reported by {_named(shut)}" + (", plus an official court record" if legal else "") + \
                f"; confirmed by {corr_txt}.", conf, {"shutdown", "insolvency", "legal"} | corr_kinds
        return "DISTRESSED", f"Shutdown reported by {_named(shut)}, but nothing in the data confirms it yet.", "low",             {"shutdown", "insolvency"}
    if legal and not insol and not shut and (openings or site_up or k["alive"] >= 2):
        # NCLT also hears mergers, name changes and petitions that are never admitted: a filing alone isn't insolvency
        return "REVIEW", f"An official court filing ({legal[0].publisher}) names the company, but no news reports insolvency and it " \
                         "shows signs of life. A person reads the filing.", "low", {"legal", "alive"}
    if legal or enough(insol):
        src =(f"official record ({legal[0].publisher})" if legal else "") + (" and " if legal and insol else "") + \
              (_named(insol) if insol else "")
        tail = "; still operating (open jobs, website up)." if alive_veto else (f"; {corr_txt}." if corr else ".")
        return "INSOLVENT", f"Insolvency proceedings: {src}{tail}", "high" if legal else "medium", {"legal", "insolvency"} | corr_kinds
    if k["unread"]:
        return "REVIEW", "News results were found but could not be read this time. A person checks, or run it again.", \
            "low", {"unread"}
    if alive_veto:
        noted = len(shut) + len(insol) + weak("shutdown") + weak("insolvency")
        return "ALIVE", "Open jobs and a working website" + \
            (f"; {noted} unconfirmed failure mention(s) noted, not enough to change that." if noted else "."), "high", {"alive"}
    if shut or insol or len(corr) >= 2:
        parts = ([f"reported by {_named(_union(shut, insol))}"] if shut or insol else []) + ([corr_txt] if corr else [])
        return "DISTRESSED", "Some evidence of failure (" + "; ".join(parts) + "), not enough independent credible sources.", "low", \
            {"shutdown", "insolvency"} | corr_kinds
    if weak("shutdown") or weak("insolvency"):
        return "REVIEW", "Failure reported only by social media, blogs or directories, which don't count as evidence. A person checks.", \
            "low", {"shutdown", "insolvency"}
    return "ALIVE", "No sign of failure.", "medium", {"alive"}


def _monthly(ts) -> list[list]:
    """Crustdata's weekly headcount as one point per month: [["2025-05", 1304], ...]"""
    by = {}
    for m, n in ts or []:
        by[m] = n
    return [[f"{m // 12}-{m % 12 + 1:02d}", by[m]] for m in sorted(by)]


# ---------------- confidence score ----------------

TIER_POINTS = {"official": 35, "news": 25, "trade": 12, "weak": 0}
CORROBORATION_POINTS = {"website": ("website down", 10), "hiring_freeze": ("hiring stopped", 10),
                        "headcount_drop": ("headcount collapse", 10), "exodus": ("employee exodus", 10)}
FAILURE = ("DEAD", "INSOLVENT", "ACQUIRED", "RENAMED")


def score(status: str, signals: list[Signal]) -> tuple[int, list[dict]]:
    """How sure Pulse is of the status it gave, 0-100, with every point accounted for (shown to the user)."""
    parts: list[dict] = []
    life = [x for x in signals if x.kind == "alive"]
    open_jobs = any("open job postings" in x.says for x in life)
    site_up = any(x.source == "http" for x in life)
    if status in FAILURE:
        rule = {"DEAD": "passed the Dead rule (2+ publishers and a data check)", "INSOLVENT": "passed the Insolvent rule",
                "ACQUIRED": "passed the Acquired rule", "RENAMED": "passed the Renamed rule"}[status]
        parts.append({"what": rule, "points": 20})
        seen = set()
        for x in signals:  # each distinct publisher that decided it, by trust tier
            if x.used and x.tier in TIER_POINTS and x.publisher and x.publisher not in seen:
                seen.add(x.publisher)
                parts.append({"what": f"{x.publisher} ({x.tier})", "points": TIER_POINTS[x.tier]})
        raw = sum(p["points"] for p in parts[1:])
        if raw > 70:
            parts.append({"what": "sources capped at 70", "points": 70 - raw})
        for kind, (label, pts) in CORROBORATION_POINTS.items():
            if any(x.kind == kind for x in signals):
                parts.append({"what": label, "points": pts})
        if status == "DEAD" and open_jobs:  # an operating insolvent / acquired / renamed company is expected to hire
            parts.append({"what": "still has open jobs", "points": -15})
        if status == "DEAD" and site_up:
            parts.append({"what": "website still up", "points": -10})
    elif status == "ALIVE":
        parts.append({"what": "no credible failure report", "points": 50})
        if open_jobs:
            parts.append({"what": "open job postings", "points": 20})
        if site_up:
            parts.append({"what": "website up", "points": 15})
        if any(x.kind == "alive" and "Headcount steady" in x.says for x in signals):
            parts.append({"what": "headcount steady", "points": 10})
        noted = {x.publisher for x in signals if x.kind in ("shutdown", "insolvency") and x.tier in CREDIBLE}
        if noted:
            parts.append({"what": f"{len(noted)} unconfirmed failure mention(s)", "points": -8 * len(noted)})
    else:  # REVIEW / DISTRESSED / CANT_TELL: low by design; a person decides
        parts.append({"what": "evidence conflicts or is thin; a person decides", "points": 30})
        pubs = {x.publisher for x in signals if x.kind in ("shutdown", "insolvency", "acquired", "legal") and x.tier in CREDIBLE}
        if pubs:
            parts.append({"what": f"{len(pubs)} credible report(s) to read", "points": min(5 * len(pubs), 15)})
    total = max(5, min(99, sum(p["points"] for p in parts)))
    return total, parts


def label(n: int) -> str:
    return "high" if n >= 80 else "medium" if n >= 55 else "low"


def check_company(name: str, domain: Optional[str], cache: companies.CompanyCache, log) -> dict:
    c, raw = _crustdata(name, domain, cache, log)
    signals: list[Signal] = []
    if c:
        signals += headcount_signals(c, raw or {})
    ex, people = exodus_signal(c.name if c else name, log)
    if ex:
        signals.append(ex)
    w = website_signal(domain or (c.domain if c else None))
    if w:
        signals.append(w)
    # Tell the reader which company this is, so a namesake elsewhere (Cred Inc., a US crypto lender) is rejected
    bi, hc = (raw or {}).get("basic_info") or {}, (raw or {}).get("headcount") or {}
    who = "".join(x for x in (
        f"\nWebsite: {(c.domain if c else None) or domain}" if (c and c.domain) or domain else "",
        f"\nMost employees in: {hc.get('largest_headcount_country')}" if hc.get("largest_headcount_country") else "",
        f"\nWhat it does: {str(bi.get('description'))[:240]}" if bi.get("description") else ""))
    news, reading, results = news_signals(c.name if c else name, log, who)
    signals += news
    for x in signals:  # data signals are cited as Crustdata or as the website itself
        if x.publisher is None:
            x.publisher, x.tier = ("Crustdata" if x.source.startswith("crustdata") else "website check"), "data"
    status, why, confidence, used_kinds = decide(signals, reading, bool(c))
    for x in signals:
        x.used = x.kind in used_kinds and x.tier != "weak"
    conf_score, conf_parts = score(status, signals)
    confidence = label(conf_score)
    crust_status = (raw or {}).get("basic_info", {}).get("status") if raw else None
    zombie = status == "DEAD" and (crust_status in (None, "active", "operating")) and bool(c)
    since = next((s.date for s in signals if s.kind in ("legal", "insolvency", "shutdown", "acquired", "renamed") and s.date), None) or \
        next((s.date for s in signals if s.kind in ("exodus", "headcount_drop") and s.date), None)
    return {
        "input": {"name": name, "domain": domain},
        "canonical": {"crustdata_company_id": c.company_id if c else None, "name": c.name if c else None,
                      "domain": c.domain if c else None, "crustdata_status": crust_status,
                      "headcount_now": c.headcount_total if c else None,
                      "headcount_series": _monthly(c.timeseries) if c else []},
        "status": status, "why": why, "confidence": confidence, "confidence_score": conf_score,
        "confidence_breakdown": conf_parts, "since": since, "zombie": zombie,
        "people": people,
        "signals": [s.model_dump() for s in signals],
        "news_reading": reading.model_dump() if reading else None,
        "checked_at": NOW(),
    }
