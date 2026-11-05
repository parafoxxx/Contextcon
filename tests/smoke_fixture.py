"""Smoke test on synthetic fixtures without calling Claude: python -m tests.smoke_fixture"""
import os
import sys

os.environ["PRAMAAN_MODE"] = "fixture"
sys.stdout.reconfigure(encoding="utf-8")

from pramaan import pipeline  # noqa: E402
from pramaan.models import EducationClaim, JobClaim, ReferenceClaim, ResumeClaims  # noqa: E402

honest = ResumeClaims(name="Aarav Mehta", email="aarav@example.com", jobs=[
    JobClaim(company="Tanager AI", title="Founding Engineer", start="Aug 2023", end="Present", early_employee_claim=3),
    JobClaim(company="Nimbus Labs Pvt. Ltd.", title="Sr. Software Engineer", start="Apr 2021", end="Jun 2023"),
    JobClaim(company="Nimbus Labs", title="SDE-2", start="Jan 2020", end="Mar 2021"),
    JobClaim(company="Orbit Retail Private Limited", title="SDE", start="07/2017", end="12/2019"),
], education=[EducationClaim(school="IIT Kanpur", degree="B.Tech", end_year=2017)],
    references=[ReferenceClaim(name="Priya Nair", company="Nimbus Labs", relationship="manager")])

liar = ResumeClaims(name="Aarav Mehta", email="aarav@example.com", jobs=[
    JobClaim(company="Tanager AI", title="Founding Engineer", start="Aug 2023", end="Present"),
    JobClaim(company="Nimbus Labs", title="Engineering Manager", start="Jan 2020", end="Jun 2023",
             team_size_claim=25, joined_stage_claim="seed", stage_claim_text="Joined pre-Series A"),
    JobClaim(company="Orbit Retail", title="SDE", start="Jan 2016", end="Dec 2019"),
    JobClaim(company="Quantix Dynamics", title="Software Engineer", start="2015", end="2016"),
], education=[EducationClaim(school="IIT Kanpur", degree="B.Tech", end_year=2015)])

for label, c in (("HONEST", honest), ("LIAR", liar)):
    r = pipeline.verify(b"", "x.pdf", consent=True, claims=c, run_id=label.lower())
    print(f"\n===== {label}: {r.overall} | {r.summary} | identity={r.identity.get('status')}")
    for v in r.verdicts:
        print(f"  [{v.status.value:11}] {v.claim_id:22} {v.reason}")
    for o in r.outreach:
        print(f"  OUTBOX {o['kind']} {o.get('company')} -> {o['to']} guard={o['guard']} {o['delivery']}")
