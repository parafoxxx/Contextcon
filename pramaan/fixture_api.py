"""A tiny offline stand-in for the Crustdata API, built from fixtures/dataset.json.
Returns the same response shapes as the real 2025-11-01 API so the rest of the code
cannot tell the difference. Synthetic data only."""
from __future__ import annotations

import json
from functools import lru_cache

from . import config
from .matching import same_company


@lru_cache(maxsize=1)
def _ds():
    return json.loads((config.FIXTURES_DIR / "dataset.json").read_text(encoding="utf-8"))


def _person_data(p: dict) -> dict:
    def role(r):
        return {"name": r[0], "title": r[1], "start_date": r[2] + "T00:00:00+00:00",
                "end_date": (r[3] + "T00:00:00+00:00") if r[3] else None, "crustdata_company_id": r[4]}
    return {
        "crustdata_person_id": p["id"],
        "basic_profile": {"name": p["name"], "headline": p["headline"], "location": {"raw": "Bengaluru, India"}},
        "social_handles": {"professional_network_identifier": {"profile_url": p["url"]}},
        "experience": {"employment_details": {"current": [role(r) for r in p["roles"] if not r[3]],
                                              "past": [role(r) for r in p["roles"] if r[3]]}},
        "education": {"schools": [{"school": s[0], "degree": s[1], "field_of_study": s[2], "start_year": s[3], "end_year": s[4]}
                                  for s in p["schools"]]},
    }


def _company_data(c: dict) -> dict:
    return {
        "crustdata_company_id": c["id"],
        "basic_info": {"name": c["name"], "primary_domain": c["domain"], "year_founded": c["founded"]},
        "headcount": {"total": c["total"], "growth_percent": c["growth"], "by_role_absolute": c["by_role"],
                      **({"timeseries": [{"date": d, "employee_count": n} for d, n in c["timeseries"]]} if c.get("timeseries") else {})},
        "funding": {"funding_rounds": [{"round_type": t, "date": d} for t, d in c["rounds"]],
                    "last_round_type": c["rounds"][-1][0] if c["rounds"] else None,
                    "last_fundraise_date": c["rounds"][-1][1] if c["rounds"] else None},
    }


def _match(p: dict, cond: dict) -> bool:
    if "op" in cond:
        return all(_match(p, c) for c in cond["conditions"])
    f, t, v = cond["field"], cond["type"], cond["value"]
    if f == "basic_profile.name":
        if t == "=":
            return p["name"].lower() == str(v).lower()
        return all(w in p["name"].lower() for w in str(v).lower().split())
    roles = p["roles"]
    if ".current." in f:
        roles = [r for r in roles if not r[3]]
    elif ".past." in f:
        roles = [r for r in roles if r[3]]
    if f.endswith("company_name"):
        vals = v if isinstance(v, list) else [v]
        return any(same_company(r[0], x) for r in roles for x in vals)
    if f.endswith("title"):
        return any(all(w in r[1].lower() for w in str(v).lower().split()) for r in roles)
    return False


def handle(path: str, body: dict):
    ds = _ds()
    if path == "/person/enrich":
        out = []
        for url in body.get("professional_network_profile_urls", []):
            m = [p for p in ds["people"] if p["url"].lower().rstrip("/") == url.lower().rstrip("/")]
            out.append({"matched_on": url, "match_type": "professional_network_profile_url",
                        "match_status": "matched" if m else "not_found",
                        "matches": [{"confidence_score": 1.0, "person_data": _person_data(m[0])}] if m else []})
        return out
    if path == "/person/search":
        hits = [p for p in ds["people"] if _match(p, body["filters"])][: body.get("limit", 25)]
        return {"profiles": [_person_data(p) for p in hits], "total_count": len(hits), "next_cursor": None}
    if path == "/company/identify":
        out = []
        for key, kind in (("names", "name"), ("domains", "domain")):
            for val in body.get(key, []):
                m = [c for c in ds["companies"] if (same_company(c["name"], val) if kind == "name" else c["domain"] == val)]
                out.append({"matched_on": val, "match_type": kind, "matches": [
                    {"confidence_score": 0.95, "company_data": {"crustdata_company_id": c["id"],
                                                                "basic_info": {"name": c["name"], "primary_domain": c["domain"]}}}
                    for c in m]})
        return out
    if path == "/company/enrich":
        out = []
        for key, kind in (("names", "name"), ("domains", "domain"), ("crustdata_company_ids", "crustdata_company_id")):
            for val in body.get(key, []):
                if kind == "name":
                    m = [c for c in ds["companies"] if same_company(c["name"], val)]
                elif kind == "domain":
                    m = [c for c in ds["companies"] if c["domain"] == val]
                else:
                    m = [c for c in ds["companies"] if c["id"] == val]
                out.append({"matched_on": val, "match_type": kind,
                            "matches": [{"confidence_score": 0.95, "company_data": _company_data(c)} for c in m]})
        return out
    if path == "/web/search/live":
        q = body["query"].lower()
        res = next((v for k, v in ds["web"].items() if k in q), [])
        return {"success": True, "query": body["query"], "results": res, "metadata": {"total_results": len(res)}}
    return None
