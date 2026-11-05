"""Live check that a real layoff (BYJU'S, mid-2023) explains a cohort, and a phantom employer forms a ring.
Applicants are invented; company data is live from Crustdata.  python -m tests.batch_live_cohort"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pramaan import batch  # noqa: E402
from pramaan.models import JobClaim as J, ReferenceClaim as R, ResumeClaims as RC  # noqa: E402

ref = R(name="Deepak Malhotra", company="Zentrix Ops Solutions", relationship="manager", email="deepak@zentrixops.example")
b1 = ["Handled 120+ parent counselling calls a week and closed admissions for K-12 programmes",
      "Maintained CRM pipeline hygiene and weekly conversion reports for the regional sales head"]
P = {
  "byjus1.pdf": RC(name="Kavya Raman", jobs=[J(company="BYJU'S", title="Business Development Associate", start="Aug 2021", end="Jul 2023")]),
  "byjus2.pdf": RC(name="Harsh Vardhan", jobs=[J(company="Byjus", title="Academic Counsellor", start="Jan 2022", end="Aug 2023")]),
  "byjus3.pdf": RC(name="Meghna Pillai", jobs=[J(company="BYJU'S (Think & Learn Pvt Ltd)", title="Inside Sales Executive", start="Mar 2022", end="Sep 2023")]),
  "zen1.pdf": RC(name="Rohit Sinha", jobs=[J(company="Zentrix Ops Solutions", title="Sales Associate", start="Feb 2022", end="Jan 2025", bullets=b1)], references=[ref]),
  "zen2.pdf": RC(name="Ananya Ghosh", jobs=[J(company="Zentrix Ops Solutions", title="Sales Executive", start="Apr 2022", end="Dec 2024", bullets=b1)], references=[ref]),
  "zen3.pdf": RC(name="Farhan Ali", jobs=[J(company="Zentrix Ops Solutions Pvt. Ltd.", title="Senior Sales Associate", start="Jul 2021", end="Feb 2025", bullets=b1)], references=[ref]),
}
b = batch.verify_batch([(b"", f) for f in P], consent=True, claims_override=P, batch_id="Blive1", do_outreach=False, workers=3,
                       emit=lambda k, m, level="info", data=None: print(f"[{k}] {m}") if k in ("cluster", "done", "error") else None)
names = {rid: a["name"] for rid, a in b["applicants"].items()}
for c in b["clusters"]:
    print(f"\n{c['type']:17} {[names[m] for m in c['members']]} signals={c['signals']}\n   {c['reason']}")
    if c.get("evidence"): print("   evidence:", {k: c["evidence"][k] for k in ("record", "field", "from", "to", "month", "drop_pct") if k in c["evidence"]})
for rid, d in b["decisions"].items():
    print(f"  {d['decision']:6} {names[rid]:14} {d['why'][:120]}")
