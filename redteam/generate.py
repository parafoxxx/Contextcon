"""Generate test resumes with known planted lies (ground truth) from real careers.

  python -m redteam.generate                      # fixture people (offline)
  python -m redteam.generate --subjects redteam/subjects.json   # live: real profile URLs, WITH CONSENT

subjects.json: [{"profile_url": "https://www.linkedin.com/in/..."}]   (people who agreed to be test subjects)

Writes redteam/out/*.pdf and redteam/out/manifest.json.
"""
from __future__ import annotations

import argparse
import copy
import json
import random
from pathlib import Path

from fpdf import FPDF

from pramaan import checks, crustdata as cd
from pramaan.dates import fmt
from pramaan.models import EducationClaim, JobClaim, ResumeClaims
from pramaan.normalize import people_from_enrich
from pramaan.titles import seniority

OUT = Path(__file__).parent / "out"
FAKE_EMPLOYERS = ["Quantix Dynamics", "Veridian Softworks", "Brightloop Analytics", "Corvane Systems"]
random.seed(7)


def base_claims(person) -> ResumeClaims:
    jobs = []
    for r in sorted(person.roles, key=lambda r: -(r.start or 0)):
        jobs.append(JobClaim(company=r.company, title=r.title, start=fmt(r.start) if r.start else None,
                             end="Present" if r.end is None else fmt(r.end)))
    edu = [EducationClaim(school=s.get("school"), degree=s.get("degree"), field=s.get("field_of_study"),
                          start_year=s.get("start_year"), end_year=s.get("end_year")) for s in person.schools if s.get("school")]
    return ResumeClaims(name=person.name, email=f"{person.name.split()[0].lower()}@example.com", jobs=jobs, education=edu)


def _past_ic(c: ResumeClaims):
    return [j for j in c.jobs if j.end != "Present" and seniority(j.title) <= 4]


def plant(c: ResumeClaims, lie: str, cache) -> tuple[ResumeClaims, dict] | None:
    c = copy.deepcopy(c)
    if lie == "title_inflation":
        cands = _past_ic(c)
        if not cands:
            return None
        j = cands[0]
        j.title = "Engineering Manager" if "engineer" in j.title.lower() or "sde" in j.title.lower() else "Senior Manager"
        return c, {"target_company": j.company, "expected_checks": ["title"]}
    if lie == "date_stretch":
        cands = [j for j in c.jobs if j.end != "Present" and j.start]
        if not cands:
            return None
        j = cands[-1]
        from pramaan.dates import to_index
        j.start = fmt(to_index(j.start) - 14)
        return c, {"target_company": j.company, "expected_checks": ["dates", "employer_exists"]}
    if lie == "fake_employer":
        oldest = c.jobs[-1] if c.jobs else None
        from pramaan.dates import to_index
        end = (to_index(oldest.start) - 1) if oldest and oldest.start else None
        name = random.choice(FAKE_EMPLOYERS)
        c.jobs.append(JobClaim(company=name, title="Software Engineer",
                               start=fmt(end - 12) if end else "2015", end=fmt(end) if end else "2016"))
        return c, {"target_company": name, "expected_checks": ["employer_exists", "profile_match"]}
    if lie == "team_size":
        cands = _past_ic(c)
        if not cands:
            return None
        j = cands[0]
        comp, _ = cache.get(j.company)
        n = max(12, int((comp.headcount_total or 20) * 1.5)) if comp and (comp.headcount_total or 0) < 60 else 15
        j.team_size_claim, j.stage_claim_text = n, f"Managed a team of {n} engineers"
        j.bullets = [f"Managed a team of {n} engineers across two product lines"]
        return c, {"target_company": j.company, "expected_checks": ["team_size_feasibility"]}
    if lie == "funding_stage":
        from pramaan.dates import to_index
        for j in c.jobs:
            comp, _ = cache.get(j.company)
            if not comp or not j.start:
                continue
            actual, _, _ = checks._stage_at(comp, to_index(j.start))
            if actual is not None and actual >= 3:
                j.joined_stage_claim, j.stage_claim_text = "seed", "Joined at seed stage, before the Series A"
                j.bullets = ["Joined at seed stage, before the Series A, and helped scale the platform"]
                return c, {"target_company": j.company, "expected_checks": ["funding_stage"]}
        return None
    if lie == "grad_year":
        if not c.education or not c.education[0].end_year:
            return None
        c.education[0].end_year -= 2
        if c.education[0].start_year:
            c.education[0].start_year -= 2
        return c, {"target_company": None, "target_edu": True, "expected_checks": ["education"]}
    raise ValueError(lie)


# ---------- messy-but-honest variants ----------

def messy(c: ResumeClaims, kind: str) -> ResumeClaims:
    c = copy.deepcopy(c)
    for j in c.jobs:
        if kind == "legal_names":
            j.company = j.company + " Pvt. Ltd."
        elif kind == "date_formats":
            from pramaan.dates import to_index
            for attr in ("start", "end"):
                v = getattr(j, attr)
                if v and v != "Present":
                    i = to_index(v)
                    setattr(j, attr, f"{i % 12 + 1:02d}/{i // 12}")
                elif v == "Present":
                    setattr(j, attr, "till date")
        elif kind == "abbrev_titles":
            j.title = (j.title.replace("Senior", "Sr.").replace("Software Development Engineer", "SDE")
                       .replace("Software Engineer II", "SDE-2").replace("Software Engineer", "SWE"))
    return c


# ---------- PDF rendering ----------

def _t(s) -> str:
    return str(s or "").replace("–", "-").replace("—", "-").replace("’", "'").encode("latin-1", "replace").decode("latin-1")


def render(c: ResumeClaims, path: Path, layout: str = "single"):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(True, 15)
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, _t(c.name), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, _t(f"{c.email}  |  Bengaluru, India"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    if layout == "two_column":
        left_x, right_x, top = 10, 75, pdf.get_y()
        pdf.set_xy(left_x, top)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(60, 7, "EDUCATION", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for e in c.education:
            pdf.set_x(left_x)
            pdf.multi_cell(60, 5, _t(f"{e.degree or ''} {e.field or ''}\n{e.school}\n{e.start_year or ''} - {e.end_year or ''}"))
        pdf.set_xy(left_x, pdf.get_y() + 4)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(60, 7, "SKILLS", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_x(left_x)
        pdf.multi_cell(60, 5, "Python, Go, Distributed systems, SQL, Kubernetes")
        pdf.set_xy(right_x, top)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, "EXPERIENCE", new_x="LMARGIN", new_y="NEXT")
        for j in c.jobs:
            pdf.set_x(right_x)
            pdf.set_font("Helvetica", "B", 10)
            pdf.multi_cell(125, 5, _t(f"{j.title} - {j.company}"))
            pdf.set_x(right_x)
            pdf.set_font("Helvetica", "I", 9)
            pdf.multi_cell(125, 5, _t(f"{j.start} - {j.end}"))
            pdf.set_font("Helvetica", "", 9)
            for b in j.bullets or ["Built and shipped production services used by thousands of customers."]:
                pdf.set_x(right_x)
                pdf.multi_cell(125, 5, _t("- " + b))
            pdf.ln(2)
    else:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Experience", new_x="LMARGIN", new_y="NEXT")
        for j in c.jobs:
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 6, _t(f"{j.title}, {j.company}"), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "I", 10)
            pdf.cell(0, 5, _t(f"{j.start} - {j.end}"), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10)
            for b in j.bullets or ["Built and shipped production services used by thousands of customers."]:
                pdf.multi_cell(0, 5, _t("- " + b), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Education", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        for e in c.education:
            pdf.cell(0, 5, _t(f"{e.degree or ''} {e.field or ''}, {e.school} ({e.start_year or ''} - {e.end_year or ''})"),
                     new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


def load_people(subjects: str | None):
    if subjects:
        urls = [s["profile_url"] for s in json.loads(Path(subjects).read_text(encoding="utf-8"))]
    else:
        urls = ["https://www.linkedin.com/in/aarav-mehta-example", "https://www.linkedin.com/in/rahul-sharma-kp-example"]
    people = []
    for u in urls:
        data, meta = cd.enrich_person(u)
        ps = people_from_enrich(data) if data else []
        if ps:
            people.append(ps[0])
        else:
            print("skip", u, meta.get("error"))
    return people


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subjects")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    cache = checks.CompanyCache(None)
    manifest = []
    for pi, person in enumerate(load_people(args.subjects), 1):
        base = base_claims(person)
        slug = f"p{pi}_{person.name.split()[0].lower()}"
        entries = [(f"{slug}_honest", base, None, {}, "single")]
        for kind, layout in (("legal_names", "two_column"), ("date_formats", "single"), ("abbrev_titles", "two_column")):
            entries.append((f"{slug}_honest_{kind}", messy(base, kind), None, {}, layout))
        for lie in ("title_inflation", "date_stretch", "fake_employer", "team_size", "funding_stage", "grad_year"):
            res = plant(base, lie, cache)
            if res:
                entries.append((f"{slug}_lie_{lie}", res[0], lie, res[1], random.choice(["single", "two_column"])))
        for name, claims, lie, truth, layout in entries:
            render(claims, OUT / f"{name}.pdf", layout)
            manifest.append({"name": name, "file": f"{name}.pdf", "lie": lie, "layout": layout, **truth,
                             "claims": json.loads(claims.model_dump_json())})
            print("wrote", name, layout)
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"{len(manifest)} resumes -> {OUT}")


if __name__ == "__main__":
    main()
