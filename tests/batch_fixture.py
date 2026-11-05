"""Offline batch test: a ring, a 'watch' pair, an explained layoff cohort, honest applicants.
python -m tests.batch_fixture"""
import os, sys
os.environ["PRAMAAN_MODE"] = "fixture"
sys.stdout.reconfigure(encoding="utf-8")
from pramaan import batch, rings  # noqa: E402
from pramaan.models import JobClaim as J, ReferenceClaim as R, ResumeClaims as RC  # noqa: E402
from tests.smoke_fixture import honest  # noqa: E402

rings.KNOWN_FAKES.unlink(missing_ok=True)
ring_ref = R(name="Rakesh Iyer", company="Quantix Dynamics", relationship="manager", email="rakesh.iyer@quantix-dynamics.example")
bullet = ["Optimised warehouse dispatch workflows, reducing turnaround time by 32% across 4 hubs",
          "Coordinated vendor onboarding and maintained SLA dashboards for regional operations"]
P = {
  "ring1.pdf": RC(name="Vivek Rana", email="vivek.r@example.com", jobs=[J(company="Quantix Dynamics", title="Operations Executive", start="Jan 2022", end="Dec 2024", bullets=bullet)], references=[ring_ref]),
  "ring2.pdf": RC(name="Sonal Mehra", email="sonal.m@example.com", jobs=[J(company="Quantix Dynamics Pvt Ltd", title="Operations Associate", start="Mar 2022", end="Nov 2024", bullets=bullet)], references=[ring_ref]),
  "ring3.pdf": RC(name="Imran Qureshi", email="imran.q@example.com", jobs=[J(company="Quantix Dynamics", title="Senior Operations Executive", start="Jun 2021", end="Oct 2024", bullets=bullet[:1] + ["Coordinated vendor onboarding and maintained SLA dashboards for regional operations teams"])], references=[ring_ref]),
  "watch1.pdf": RC(name="Leela Thomas", jobs=[J(company="Corvane Systems", title="Analyst", start="2020", end="2022")]),
  "watch2.pdf": RC(name="Arvind Nair", jobs=[J(company="Corvane Systems", title="Analyst", start="2019", end="2021")]),
  "cohort1.pdf": RC(name="Pooja Shah", jobs=[J(company="Orbit Retail", title="Store Operations Lead", start="Jan 2021", end="Apr 2024")]),
  "cohort2.pdf": RC(name="Nikhil Bansal", jobs=[J(company="Orbit Retail", title="Category Analyst", start="Aug 2020", end="Mar 2024")]),
  "cohort3.pdf": RC(name="Ritika Sen", jobs=[J(company="Orbit Retail", title="Supply Planner", start="Feb 2022", end="May 2024")]),
  "honest_aarav.pdf": honest,
}
b = batch.verify_batch([(b"", f) for f in P], consent=True, claims_override=P, batch_id="Btest", do_outreach=False,
                       emit=lambda k, m, level="info", data=None: print(f"[{k}] {m}") if k in ("cluster", "done") else None)
names = {rid: a["name"] for rid, a in b["applicants"].items()}
print()
for c in b["clusters"]:
    print(f"{c['type']:17} {[names[m] for m in c['members']]}  signals={c['signals']}")
for rid, d in b["decisions"].items():
    print(f"  {d['decision']:6} {names[rid]:14} {d['why'][:110]}")
# Known-fake memory: a new applicant in a later batch
new = {"late.pdf": RC(name="Tarun Gill", jobs=[J(company="Quantix Dynamics", title="Ops Executive", start="2023", end="2024")])}
b2 = batch.verify_batch([(b"", "late.pdf")], consent=True, claims_override=new, batch_id="Btest2", do_outreach=False)
print("\nnext batch:", [(b2["applicants"][r]["name"], d["decision"], d["why"][:100]) for r, d in b2["decisions"].items()])
