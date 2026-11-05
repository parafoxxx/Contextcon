"""Cross-applicant analysis: link applicants by shared artifacts, find clusters, and tell a
fraud ring apart from an honest cohort (people who left the same real company together).

Rule: a cluster is a RING only with at least two *independent* signal types, or a phantom
employer plus one more. One shared employer alone never convicts anyone.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz

from . import config
from .dates import fmt, to_index
from .matching import clean, same_company
from .models import ControlledBy as C, Evidence, Report, Status as S

KNOWN_FAKES = config.DATA / "known_fake_employers.json"

# Signal types. "Strong" ones can form a cluster on their own; "support" only adds weight.
STRONG = {"phantom_employer", "shared_reference", "shared_contact", "shared_profile_url", "near_duplicate_text"}
SUPPORT = {"shared_small_employer", "pdf_metadata"}


@dataclass
class Applicant:
    run_id: str
    name: str
    report: Report
    pdf_meta: dict = field(default_factory=dict)


@dataclass
class Link:
    a: str
    b: str
    kind: str
    detail: str


# ---------------- known fakes memory ----------------

def load_known_fakes() -> dict:
    try:
        return json.loads(KNOWN_FAKES.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def remember_fake(employer: str, batch_id: str, members: list[str]):
    fakes = load_known_fakes()
    key = clean(employer)
    entry = fakes.get(key, {"name": employer, "seen_in": []})
    entry["seen_in"].append({"batch": batch_id, "applicants": members})
    fakes[key] = entry
    KNOWN_FAKES.write_text(json.dumps(fakes, indent=1), encoding="utf-8")


def known_fake(employer: str) -> Optional[dict]:
    fakes = load_known_fakes()
    hit = fakes.get(clean(employer))
    if hit:
        return hit
    return next((v for v in fakes.values() if same_company(v["name"], employer)), None)


# ---------------- per-applicant facts ----------------

def employer_status(r: Report) -> dict[str, dict]:
    """company -> {phantom: bool, size: int|None, job_index, end}"""
    out = {}
    if not r.claims:
        return out
    by_id = {v.claim_id: v for v in r.verdicts}
    for i, j in enumerate(r.claims.jobs, 1):
        v = by_id.get(f"job{i}.employer")
        phantom = bool(v and v.check == "employer_exists" and v.status in (S.REVIEW, S.DISCREPANCY, S.UNVERIFIED)
                       and "not in crustdata" in v.reason.lower() or (v and "no company record" in v.reason.lower()))
        size, cid = None, None
        for ev in (v.evidence if v else []):
            if ev.field == "headcount.total" and ev.value and ev.value.isdigit():
                size = int(ev.value)
            m = re.match(r"crustdata_company_id=(\d+)", ev.record or "")
            if m:
                cid = int(m.group(1))
        out[j.company] = {"phantom": phantom, "size": size, "company_id": cid, "job": i, "start": to_index(j.start),
                          "end": to_index(j.end, is_end=True), "verdict": v}
    return out


def _shingles(text: str, k: int = 5) -> set:
    w = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(w[i:i + k]) for i in range(max(0, len(w) - k + 1))}


def _bullets(r: Report) -> str:
    if not r.claims:
        return ""
    return " ".join(b for j in r.claims.jobs for b in (j.bullets or []))


# ---------------- linking ----------------

def build_links(apps: list[Applicant]) -> list[Link]:
    links: list[Link] = []
    emp = {a.run_id: employer_status(a.report) for a in apps}

    # shared employers
    for x in range(len(apps)):
        for y in range(x + 1, len(apps)):
            A, B = apps[x], apps[y]
            for ca, sa in emp[A.run_id].items():
                for cb, sb in emp[B.run_id].items():
                    same_id = sa["company_id"] and sa["company_id"] == sb["company_id"]
                    if not (same_id or same_company(ca, cb)):  # one company under two spellings = one record
                        continue
                    if sa["phantom"] or sb["phantom"]:
                        links.append(Link(A.run_id, B.run_id, "phantom_employer", f"both cite '{ca}', which has no company record"))
                    elif (sa["size"] or sb["size"] or 10**6) < 50:
                        links.append(Link(A.run_id, B.run_id, "shared_small_employer", f"both cite '{ca}' (~{sa['size'] or sb['size']} staff)"))
                    elif sa["end"] is not None and sb["end"] is not None and abs(sa["end"] - sb["end"]) <= 6:
                        # a cohort = people who LEFT the same real company at about the same time
                        links.append(Link(A.run_id, B.run_id, "shared_employer",
                                          f"both left {ca} around {fmt(min(sa['end'], sb['end']))}"))

    # references, contact details, profile URLs, text
    def refs(r: Report):
        out = set()
        for ref in (r.claims.references if r.claims else []):
            for v in (ref.email, re.sub(r"\D", "", ref.name or "") and None, ref.name and clean(ref.name)):
                if v:
                    out.add(v.lower())
        return out

    for x in range(len(apps)):
        for y in range(x + 1, len(apps)):
            A, B = apps[x], apps[y]
            ca, cb = A.report.claims, B.report.claims
            if not ca or not cb:
                continue
            same_person = fuzz.token_sort_ratio(ca.name.lower(), cb.name.lower()) >= 90
            shared_refs = refs(A.report) & refs(B.report)
            if shared_refs:
                links.append(Link(A.run_id, B.run_id, "shared_reference", f"same reference: {sorted(shared_refs)[0]}"))
            for attr in ("email", "phone"):
                va, vb = getattr(ca, attr), getattr(cb, attr)
                if va and vb and re.sub(r"\W", "", va.lower()) == re.sub(r"\W", "", vb.lower()) and not same_person:
                    links.append(Link(A.run_id, B.run_id, "shared_contact", f"same {attr} under two names"))
            if ca.linkedin_url and cb.linkedin_url and ca.linkedin_url.rstrip("/").lower() == cb.linkedin_url.rstrip("/").lower() \
                    and not same_person:
                links.append(Link(A.run_id, B.run_id, "shared_profile_url", "same profile URL under two names"))
            sa, sb = _shingles(_bullets(A.report)), _shingles(_bullets(B.report))
            if len(sa) >= 8 and len(sb) >= 8:
                jac = len(sa & sb) / len(sa | sb)
                if jac >= 0.5 and not same_person:
                    links.append(Link(A.run_id, B.run_id, "near_duplicate_text", f"{jac:.0%} of achievement text identical"))
            ma, mb = A.pdf_meta, B.pdf_meta
            if ma and mb and ma.get("creator") and ma.get("creator") == mb.get("creator") and \
                    ma.get("producer") == mb.get("producer") and ma.get("creator") not in ("", None):
                links.append(Link(A.run_id, B.run_id, "pdf_metadata", f"same PDF creator/producer ({ma.get('creator')})"))
    return links


# ---------------- clustering & classification ----------------

def _components(nodes: list[str], links: list[Link], kinds: set) -> list[set]:
    parent = {n: n for n in nodes}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    for l in links:
        if l.kind in kinds:
            parent[find(l.a)] = find(l.b)
    groups = defaultdict(set)
    for n in nodes:
        groups[find(n)].add(n)
    return [g for g in groups.values() if len(g) >= 2]


def cohort_explanation(company, members: list[Applicant], emp: dict) -> Optional[dict]:
    """Did the real company shrink when these people say they left? Uses the headcount timeseries."""
    if company is None or not company.timeseries:
        return None
    ends = [st["end"] for m in members for k, st in emp[m.run_id].items()
            if (same_company(k, company.name) or st["company_id"] == company.company_id) and st["end"] is not None]
    if not ends:
        return None
    ends.sort()
    mid = ends[len(ends) // 2]
    ts = dict(company.timeseries)
    months = sorted(ts)
    before = [ts[m] for m in months if mid - 4 <= m <= mid - 1]
    after = [ts[m] for m in months if mid + 1 <= m <= mid + 3]
    if not before or not after:
        return {"explained": False, "why": f"headcount history doesn't cover {fmt(mid)}"}
    b, a = max(before), min(after)
    drop = (b - a) / b if b else 0
    spread = ends[-1] - ends[0]
    return {"explained": drop >= 0.03 and spread <= 6, "drop_pct": round(drop * 100, 1), "from": b, "to": a,
            "month": fmt(mid), "spread_months": spread,
            "why": (f"{company.name} headcount fell {drop:.0%} ({b:,} → {a:,}) around {fmt(mid)}, when these applicants left"
                    if drop >= 0.03 else f"no headcount drop at {company.name} around {fmt(mid)} ({b:,} → {a:,})")}


SIGNAL_TEXT = {
    "phantom_employer": "all cite {x}, which has no company record",
    "shared_reference": "the same reference ({x})",
    "shared_contact": "the same contact details under different names",
    "shared_profile_url": "the same profile URL under different names",
    "near_duplicate_text": "near-identical achievement text",
    "pdf_metadata": "PDFs made with the same tool ({x})",
    "shared_small_employer": "the same very small employer ({x})",
}


def ring_summary(glinks: list) -> str:
    parts = []
    for kind in ["phantom_employer", "shared_reference", "shared_contact", "shared_profile_url", "near_duplicate_text",
                 "shared_small_employer", "pdf_metadata"]:
        ls = [l for l in glinks if l.kind == kind]
        if not ls:
            continue
        x = ""
        if kind in ("phantom_employer", "shared_small_employer"):
            x = "'" + ls[0].detail.split("'")[1] + "'"
        elif kind == "shared_reference":
            x = ls[0].detail.split(": ", 1)[-1].title()
        elif kind == "pdf_metadata":
            x = ls[0].detail.split("(")[-1].rstrip(")")
        parts.append(SIGNAL_TEXT[kind].format(x=x))
    return "; ".join(parts)


def analyse(batch_id: str, apps: list[Applicant], company_lookup) -> dict:
    """company_lookup(name, company_id) -> normalize.Company | None (cached)."""
    links = build_links(apps)
    by_id = {a.run_id: a for a in apps}
    emp = {a.run_id: employer_status(a.report) for a in apps}

    # Strong signals form rings; shared real employers form cohorts (checked separately)
    ring_groups = _components([a.run_id for a in apps], links, STRONG)
    cohort_groups = _components([a.run_id for a in apps], links, {"shared_employer", "shared_small_employer"})

    clusters = []
    in_ring = set()
    for g in ring_groups:
        glinks = [l for l in links if l.a in g and l.b in g]
        kinds = {l.kind for l in glinks}
        strong = kinds & STRONG
        phantom = [l.detail.split("'")[1] for l in glinks if l.kind == "phantom_employer"]
        is_ring = len(strong) >= 2 or (phantom and len(kinds) >= 2)
        clusters.append({
            "id": f"C{len(clusters) + 1}", "type": "ring" if is_ring else "watch", "members": sorted(g),
            "signals": sorted(kinds), "phantom_employers": sorted(set(phantom)),
            "reason": (f"{len(g)} applicants linked by {len(strong)} independent signals: " + ring_summary(glinks) if is_ring else
                       f"{len(g)} applicants share one signal ({', '.join(sorted(kinds))}); not enough to call a ring"),
            "links": [l.__dict__ for l in glinks],
        })
        if is_ring:
            in_ring |= g

    for g in cohort_groups:
        if g <= in_ring:
            continue
        members = [by_id[m] for m in g]
        common, common_id = None, None
        for comp, st in emp[members[0].run_id].items():
            if all(any(same_company(comp, k) or (st["company_id"] and st["company_id"] == s2["company_id"])
                       for k, s2 in emp[m.run_id].items()) for m in members):
                common, common_id = comp, st["company_id"]
                break
        company = company_lookup(common, common_id) if common else None
        exp = cohort_explanation(company, members, emp)
        clusters.append({
            "id": f"C{len(clusters) + 1}", "type": "explained_cohort" if exp and exp["explained"] else "cohort",
            "members": sorted(g), "signals": ["shared_employer"], "employer": common,
            "reason": (exp or {}).get("why") or f"{len(g)} applicants worked at {common}; no headcount history to explain it",
            "evidence": ({"source": "crustdata:/company/enrich", "field": "headcount.timeseries",
                          "record": f"crustdata_company_id={company.company_id}", **exp} if exp and company else None),
            "links": [l.__dict__ for l in links if l.a in g and l.b in g and l.kind.startswith("shared")],
        })

    # Known fakes from earlier batches
    for a in apps:
        for comp in emp[a.run_id]:
            hit = known_fake(comp)
            if hit and not any(s.get("batch") == batch_id for s in hit["seen_in"]):
                a.report.verdicts.append(_known_fake_verdict(comp, hit))

    decisions = {}
    for a in apps:
        decisions[a.run_id] = decide(a, in_ring, clusters)
    for c in clusters:
        if c["type"] == "ring":
            for e in c["phantom_employers"]:
                remember_fake(e, batch_id, [by_id[m].name for m in c["members"]])
    return {"batch_id": batch_id, "links": [l.__dict__ for l in links], "clusters": clusters, "decisions": decisions}


def _known_fake_verdict(comp, hit):
    from .models import Verdict
    seen = hit["seen_in"][0]
    return Verdict(claim_id="known_fake", claim_text=f"Worked at {comp}", check="known_fake_employer", status=S.DISCREPANCY,
                   confidence="high",
                   reason=f"'{comp}' was identified as a phantom employer in batch {seen['batch']} "
                          f"(cited by {len(seen['applicants'])} applicants in a ring).",
                   evidence=[Evidence(source="pramaan:known_fake_employers", record=seen["batch"], value=hit["name"],
                                      controlled_by=C.THIRD_PARTY)])


def decide(a: Applicant, in_ring: set, clusters: list) -> dict:
    """CLEAR / VERIFY / HOLD. Never an automatic reject: a person makes the final call on HOLD."""
    vs = a.report.verdicts
    cluster = next((c for c in clusters if a.run_id in c["members"]), None)
    if a.run_id in in_ring:
        return {"decision": "HOLD", "why": f"member of {cluster['id']}: {cluster['reason']}", "cluster": cluster["id"]}
    if any(v.check == "known_fake_employer" for v in vs):
        return {"decision": "HOLD", "why": next(v.reason for v in vs if v.check == "known_fake_employer"), "cluster": None}
    bad = [v for v in vs if v.status == S.DISCREPANCY and v.confidence == "high"]
    if bad:
        return {"decision": "VERIFY", "why": f"{len(bad)} contradicted claim(s): {bad[0].reason}",
                "cluster": cluster["id"] if cluster else None}
    open_ = [v for v in vs if v.status in (S.REVIEW, S.DISCREPANCY, S.UNVERIFIED, S.PENDING) and v.check in
             ("employer_exists", "profile_match", "dates", "title", "identity")]
    if open_:
        note = f" ({cluster['reason']})" if cluster and cluster["type"] == "explained_cohort" else ""
        return {"decision": "VERIFY", "why": f"{len(open_)} claim(s) need employer confirmation{note}",
                "cluster": cluster["id"] if cluster else None}
    return {"decision": "CLEAR", "why": "every checked claim holds up", "cluster": cluster["id"] if cluster else None}
