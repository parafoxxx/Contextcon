"""Build an applicant pile with ground truth: rings, a real layoff cohort, individual liars, honest people.

  python -m redteam.batch_gen          -> redteam/batch_out/*.pdf + manifest.json

All applicant names are INVENTED. Real companies (BYJU'S, Unacademy, Infosys, ...) appear only as employers,
with company facts checked live against Crustdata. Phantom employers are invented names.

Ground truth per entry:  expect = HOLD | VERIFY | CLEAR-or-VERIFY ;  ring / cohort ids ;  lie (individual)
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from pramaan.models import EducationClaim as E, JobClaim as J, ReferenceClaim as R, ResumeClaims as RC
from redteam.generate import render

OUT = Path(__file__).parent / "batch_out"
random.seed(11)

OPS = ["Optimised dispatch workflows, reducing order turnaround time by 32% across 4 hubs",
       "Coordinated vendor onboarding and maintained SLA dashboards for regional operations",
       "Trained 15 new joiners on ticketing and escalation processes"]
SALES = ["Handled 120+ parent counselling calls a week and closed admissions for K-12 programmes",
         "Maintained CRM pipeline hygiene and weekly conversion reports for the regional sales head",
         "Exceeded quarterly admissions target by 18% for three consecutive quarters"]


def edu(school="Delhi University", deg="B.Com", y=2020):
    return [E(school=school, degree=deg, start_year=y - 3, end_year=y)]


def pile() -> list[dict]:
    out = []

    def add(name, claims, expect, **truth):
        out.append({"name": name, "claims": claims, "expect": expect, **truth})

    # ---- Ring A: 5 applicants, phantom employer + same reference + copied bullets ----
    refA = R(name="Deepak Malhotra", company="Zentrix Ops Solutions", relationship="Reporting manager",
             email="deepak.m@zentrixops.example")
    for nm, title, s, e in [("Rohit Sinha", "Operations Associate", "Feb 2022", "Jan 2025"),
                            ("Ananya Ghosh", "Operations Executive", "Apr 2022", "Dec 2024"),
                            ("Farhan Ali", "Senior Operations Associate", "Jul 2021", "Feb 2025"),
                            ("Sneha Kulkarni", "Operations Associate", "Sep 2022", "Mar 2025"),
                            ("Manish Yadav", "Operations Coordinator", "Jan 2022", "Nov 2024")]:
        b = random.sample(OPS, 2)
        add(nm, RC(name=nm, email=f"{nm.split()[0].lower()}.{nm.split()[1][0].lower()}@example.com",
                   jobs=[J(company=random.choice(["Zentrix Ops Solutions", "Zentrix Ops Solutions Pvt. Ltd."]), title=title,
                           start=s, end=e, bullets=b)], education=edu(), references=[refA]),
            "HOLD", ring="A")

    # ---- Ring B: 3 applicants, phantom employer + one phone number under three names + copied bullets ----
    for nm, s, e in [("Tarun Gill", "Mar 2021", "Aug 2024"), ("Pallavi Joshi", "Jun 2021", "Jul 2024"),
                     ("Varun Bhatt", "Jan 2022", "Sep 2024")]:
        add(nm, RC(name=nm, email=f"{nm.split()[0].lower()}@example.com", phone="+91 98765 43210",
                   jobs=[J(company="Brightloop Analytics", title="Data Analyst", start=s, end=e,
                           bullets=["Built Power BI dashboards tracking weekly sales KPIs for 30+ retail stores",
                                    "Automated Excel MIS reports with Python, saving 10 hours a week"])],
                   education=edu("Pune University", "B.Sc", 2020)),
            "HOLD", ring="B")

    # ---- Honest cohort: left BYJU'S together in mid-2023 (real layoff in Crustdata's headcount history) ----
    for nm, title, s, e, b in [("Kavya Raman", "Business Development Associate", "Aug 2021", "Jul 2023", SALES[0]),
                               ("Harsh Vardhan", "Academic Counsellor", "Jan 2022", "Aug 2023", SALES[2]),
                               ("Meghna Pillai", "Inside Sales Executive", "Mar 2022", "Sep 2023",
                                "Ran demo sessions for 40 families a month in the Bengaluru region"),
                               ("Aditya Rao", "Business Development Executive", "Nov 2021", "Aug 2023",
                                "Mentored a pod of 4 BDAs on objection handling")]:
        add(nm, RC(name=nm, email=f"{nm.split()[0].lower()}.{nm.split()[1].lower()}@example.com",
                   jobs=[J(company=random.choice(["BYJU'S", "Byjus", "BYJU'S (Think & Learn Pvt Ltd)"]), title=title,
                           start=s, end=e, bullets=[b])], education=edu()),
            "VERIFY", cohort="byjus_2023")

    # ---- Individual liars at real companies ----
    add("Ishaan Mehta", RC(name="Ishaan Mehta", email="ishaan.m@example.com",
                           jobs=[J(company="Unacademy", title="Growth Associate", start="Jan 2022", end="Jun 2024",
                                   joined_stage_claim="seed", stage_claim_text="Joined at seed stage, before the Series A",
                                   bullets=["Joined at seed stage, before the Series A, and scaled referral growth"])],
                           education=edu()), "VERIFY", lie="funding_stage")
    add("Neha Arora", RC(name="Neha Arora", email="neha.arora@example.com",
                         jobs=[J(company="Crustdata", title="Software Engineer", start="Jan 2021", end="Dec 2023")],
                         education=edu("IIT Delhi", "B.Tech", 2020)), "VERIFY", lie="founded_after_join")
    add("Sameer Khan", RC(name="Sameer Khan", email="sameer.k@example.com",
                          jobs=[J(company="Crustdata", title="Engineering Manager", start="Jan 2025", end="Present",
                                  team_size_claim=80, stage_claim_text="Managed a team of 80 engineers",
                                  bullets=["Managed a team of 80 engineers across data and platform"])],
                          education=edu("NIT Trichy", "B.Tech", 2018)), "VERIFY", lie="team_size")

    # ---- Honest, unrelated applicants at real companies (no profile exists, so they go to VERIFY, never HOLD) ----
    for nm, comp, title, s, e in [("Rahul Verma", "Infosys", "Systems Engineer", "Aug 2019", "Jul 2022"),
                                  ("Priya Menon", "Flipkart", "Category Manager", "Jun 2020", "Present"),
                                  ("Arjun Reddy", "Unacademy", "Educator Success Associate", "Feb 2023", "Mar 2025")]:
        add(nm, RC(name=nm, email=f"{nm.split()[0].lower()}.{nm.split()[1].lower()}@example.com",
                   jobs=[J(company=comp, title=title, start=s, end=e)], education=edu()), "NOT_HOLD")

    # ---- Two honest people from the same tiny local firm: one shared signal only -> must NOT be held ----
    for nm, s, e in [("Leela Thomas", "2019", "2021"), ("Joseph Mathew", "2020", "2022")]:
        add(nm, RC(name=nm, email=f"{nm.split()[0].lower()}@example.com",
                   jobs=[J(company="Kochi Spice Traders", title="Accounts Assistant", start=s, end=e)],
                   education=edu("Mahatma Gandhi University", "B.Com", 2019)), "NOT_HOLD")

    # ---- The same honest person submitted twice, reworded: must get the same decision ----
    dup = RC(name="Rahul Verma", email="rahul.verma@example.com",
             jobs=[J(company="Infosys Limited", title="Systems Engineer", start="08/2019", end="07/2022")], education=edu())
    add("Rahul Verma (resubmitted)", dup, "SAME_AS:Rahul Verma")
    return out


def next_morning() -> list[dict]:
    """One new applicant the next day, citing the employer a ring used yesterday."""
    return [{"name": "Kunal Desai", "claims": RC(name="Kunal Desai", email="kunal.desai@example.com",
             jobs=[J(company="Zentrix Ops Solutions", title="Operations Lead", start="Mar 2021", end="Aug 2025",
                     bullets=["Led a 6-person dispatch desk handling 2,000 orders a day"])], education=edu()),
             "expect": "HOLD", "lie": "known_fake"}]


def main():
    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    manifest = []
    for k, e in enumerate(pile(), 1):
        fn = f"{k:02d}_{e['name'].split(' (')[0].replace(' ', '_').lower()}{'_v2' if 'resubmitted' in e['name'] else ''}.pdf"
        render(e["claims"], OUT / fn, random.choice(["single", "two_column"]))
        manifest.append({**{k2: v for k2, v in e.items() if k2 != "claims"}, "file": fn,
                         "claims": json.loads(e["claims"].model_dump_json())})
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"{len(manifest)} applications -> {OUT}")
    nxt = OUT.parent / "batch_next_morning"
    nxt.mkdir(exist_ok=True)
    for e in next_morning():
        render(e["claims"], nxt / f"{e['name'].replace(' ', '_').lower()}.pdf")
    print(f"next-morning applicant -> {nxt}")


if __name__ == "__main__":
    main()
