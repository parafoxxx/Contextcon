"""Pulse: is this company still alive?  Finds zombie companies in a CRM or a data graph.

For each company: resolve one canonical Crustdata record, gather independent evidence
(court filings, press, an exodus of employees, headcount collapse, website status), decide
ALIVE / DISTRESSED / DEAD / ACQUIRED / CANT_TELL by rules, and cite every fact with its date.
The LLM only reads news snippets; rules decide.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from . import checks, config, crustdata as cd, llm
from .dates import fmt, to_index, today_index
from .matching import same_company
from .normalize import companies_from_enrich, people_from_search

NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
# Only official court / registry sites count as a legal record; everything else is press.
OFFICIAL = re.compile(r"(nclt\.gov\.in|nclat\.nic\.in|ibbi\.gov\.in|mca\.gov\.in|company-information\.service\.gov\.uk|"
                      r"thegazette\.co\.uk|courtlistener\.com|sci\.gov\.in|ecourts\.gov\.in)", re.I)


class Signal(BaseModel):
    kind: str            # legal | press | exodus | headcount_drop | website | hiring_freeze | stale_funding | alive
    strength: str        # strong | support
    says: str            # human sentence
    source: str
    url: Optional[str] = None
    record: Optional[str] = None
    value: Optional[str] = None
    date: Optional[str] = None
    retrieved_at: str = Field(default_factory=NOW)


class NewsReading(BaseModel):
    about_this_company: bool = Field(description="False if the results are about a different company with a similar name")
    company_role: Literal["subject", "creditor_or_petitioner", "mentioned", "unclear"] = Field(
        description="In any insolvency/court item: is THIS company the one being wound up (subject), the one suing or owed "
                    "money (creditor_or_petitioner), or just mentioned?")
    ceased_operations: bool = Field(description="Results state THIS company shut down, halted operations, or ceased trading")
    insolvency_or_bankruptcy: bool = Field(description="Results state THIS company entered insolvency, bankruptcy, administration or liquidation")
    acquired_by: Optional[str] = Field(None, description="Acquirer, if results state the company was acquired")
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
A company that ACQUIRES others was not acquired. A subsidiary's liquidation, or a SOLVENT voluntary liquidation, is a
restructuring, not the main company failing. Shutting down one product, app feature, office or business line is NOT
the company shutting down: only set ceased_operations if the whole company stopped operating.
Layoffs alone are not a shutdown. If the results are about a different company with a similar name, say so."""


# ---------------- evidence gathering ----------------

def _crustdata(name: str, domain: Optional[str], cache: checks.CompanyCache, log):
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


def exodus_signal(name: str, log) -> tuple[Optional[Signal], dict]:
    """Departures in the last 18 months vs the 18 before, from Crustdata people data; and who still lists it as current."""
    cur, _ = cd.search_people({"field": "experience.employment_details.current.company_name", "type": "in", "value": [name]}, 1, log)
    past, _ = cd.search_people({"field": "experience.employment_details.past.company_name", "type": "in", "value": [name]}, 50, log)
    ends = []
    for p in people_from_search(past):
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
             "sample_departures_recent_18m": recent, "sample_departures_prior_18m": before, "peak_departure_month": spike_month}
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


def news_signals(name: str, log) -> tuple[list[Signal], Optional[NewsReading], list[dict]]:
    q = f"{name} shut down OR insolvency OR bankruptcy OR liquidation OR acquired"
    data, meta = cd.web_search(q, log)
    results = [r for r in ((data or {}).get("results") or []) if isinstance(r, dict)][:8]
    if not results:
        return [], None, []
    out: list[Signal] = []
    listing = "\n".join(f"[{i}] {r.get('title', '')} | {r.get('snippet', '')[:300]} | {r.get('url', '')[:120]}"
                        for i, r in enumerate(results))
    try:
        reading = llm._parse(NewsReading, NEWS_SYSTEM, f"Company: {name}\n\nResults:\n{listing}")
    except llm.LLMError:
        return [], None, results
    if not reading.about_this_company:
        return [], reading, results
    idx = [i for i in reading.supporting_results if 0 <= i < len(results)] or [0]
    official = [r for r in results if OFFICIAL.search(r.get("url") or "")]

    def sig(kind, i, says, strength="strong"):
        r = results[i]
        return Signal(kind=kind, strength=strength, source="crustdata:/web/search/live", url=r.get("url"),
                      value=(r.get("title") or "")[:140], date=reading.event_date, says=says)

    title = lambda i: (results[i].get("title") or "")[:110]  # noqa: E731
    if reading.insolvency_or_bankruptcy and reading.subsidiary_or_solvent_only:
        i = idx[0]
        out.append(sig("restructuring", i, f"Subsidiary / solvent liquidation only, not a failure: “{title(i)}”", "support"))
    elif reading.insolvency_or_bankruptcy and reading.company_role in ("subject", "unclear"):
        if official:
            r = official[0]
            out.append(Signal(kind="legal", strength="strong", source="crustdata:/web/search/live", url=r.get("url"),
                              value=(r.get("title") or "")[:140], date=reading.event_date,
                              says=f"Official court / registry record: {(r.get('title') or '')[:110]}"))
        for i in idx[:3]:
            out.append(sig("insolvency", i, f"Insolvency / bankruptcy reported: “{title(i)}”"))
    elif official and reading.company_role == "creditor_or_petitioner":
        out.append(Signal(kind="court_mention", strength="support", source="crustdata:/web/search/live",
                          url=official[0].get("url"), value=(official[0].get("title") or "")[:140],
                          says="Named in a court case as creditor / petitioner, not as the company being wound up."))
    if reading.ceased_operations:
        for i in idx[:3]:
            out.append(sig("shutdown", i, f"Shutdown reported: “{title(i)}”"))
    if reading.acquired_by:
        for i in idx[:3]:  # one signal per supporting source, so a single article can't make it 'acquired'
            out.append(sig("acquired", i, f"Acquired by {reading.acquired_by}: “{title(i)}”",
                           "strong" if len(idx) >= 2 else "support"))
    if reading.renamed_to:
        out.append(sig("renamed", idx[0], f"Renamed to {reading.renamed_to}: “{title(idx[0])}”"))
    if reading.layoffs_only:
        out.append(sig("layoffs", idx[0], "Layoffs reported (not a shutdown).", "support"))
    return out, reading, results


# ---------------- decision ----------------

def decide(signals: list[Signal], reading: Optional[NewsReading], found: bool) -> tuple[str, str]:
    k = Counter(s.kind for s in signals)
    if not found and not signals:
        return "CANT_TELL", "No company record and no public trace."
    # Signs of life (real job openings AND a working website) veto DEAD: conflicting evidence goes to a person.
    openings = any(s.kind == "alive" and "open job postings" in s.says for s in signals)
    site_up = any(s.kind == "alive" and s.source == "http" for s in signals)
    alive_veto = openings and site_up
    site_down = k["website"] > 0
    corroboration = [n for n, ok in (("website down", site_down), ("hiring stopped", k["hiring_freeze"] > 0),
                                     ("headcount collapse", k["headcount_drop"] > 0), ("employee exodus", k["exodus"] > 0)) if ok]
    if k["renamed"]:
        return "RENAMED", next(s.says for s in signals if s.kind == "renamed")
    if k["acquired"] and not k["shutdown"]:
        redirect = any(s.kind == "website" and "redirects" in (s.value or "") for s in signals)
        if k["acquired"] >= 2 or redirect:
            return "ACQUIRED", next(s.says for s in signals if s.kind == "acquired")
        return "REVIEW", "One source reports an acquisition (" + next(s.says for s in signals if s.kind == "acquired") + \
            "); not confirmed by a second source or a website redirect. A person decides."
    if k["shutdown"] >= 2 or (k["shutdown"] and k["insolvency"]):
        if alive_veto:
            return "REVIEW", "Press reports a shutdown, but the company still has open jobs and a working website. A person decides."
        if corroboration:
            return "DEAD", f"Shutdown reported by {k['shutdown']} source(s), corroborated by " + ", ".join(corroboration) + "."
        return "DISTRESSED", "Shutdown reported, but nothing in the data corroborates it yet."
    if k["legal"] or k["insolvency"] >= 2:
        tail = " (official court record)" if k["legal"] else ""
        if alive_veto:
            return "INSOLVENT", f"Insolvency proceedings reported{tail}; still operating (open jobs, website up)."
        return "INSOLVENT", f"Insolvency proceedings reported{tail}" + \
            (", corroborated by " + ", ".join(corroboration) if corroboration else "") + "."
    if alive_veto and (k["shutdown"] + k["insolvency"]) <= 1 and not k["legal"]:
        return "ALIVE", "Open jobs and a working website; one unconfirmed press mention was noted but doesn't change that."
    if k["shutdown"] or k["insolvency"] or len(corroboration) >= 2:
        why = corroboration + (["press report"] if k["shutdown"] or k["insolvency"] else [])
        return "DISTRESSED", "Some evidence of failure (" + ", ".join(why) + "), not enough independent sources to call it."
    return "ALIVE", "No sign of failure" + (" (open jobs, website up)." if alive_veto else ".")


def check_company(name: str, domain: Optional[str], cache: checks.CompanyCache, log) -> dict:
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
    news, reading, results = news_signals(c.name if c else name, log)
    signals += news
    status, why = decide(signals, reading, bool(c))
    crust_status = (raw or {}).get("basic_info", {}).get("status") if raw else None
    zombie = status == "DEAD" and (crust_status in (None, "active", "operating")) and bool(c)
    since = next((s.date for s in signals if s.kind in ("legal", "insolvency", "shutdown", "acquired", "renamed") and s.date), None) or \
        next((s.date for s in signals if s.kind in ("exodus", "headcount_drop") and s.date), None)
    return {
        "input": {"name": name, "domain": domain},
        "canonical": {"crustdata_company_id": c.company_id if c else None, "name": c.name if c else None,
                      "domain": c.domain if c else None, "crustdata_status": crust_status,
                      "headcount_now": c.headcount_total if c else None},
        "status": status, "why": why, "since": since, "zombie": zombie,
        "people": people,
        "signals": [s.model_dump() for s in signals],
        "news_reading": reading.model_dump() if reading else None,
        "checked_at": NOW(),
    }
