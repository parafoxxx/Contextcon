"""A batch that crashes mid-way resumes without re-checking anyone already done.  python -m tests.test_batch_resume"""
import os, sys
os.environ["PRAMAAN_MODE"] = "fixture"
sys.stdout.reconfigure(encoding="utf-8")
from pramaan import batch, pipeline  # noqa: E402
from tests.batch_fixture import P  # noqa: E402  (also runs the fixture batch once)

real_verify, calls = pipeline.verify, []


def crashing(*a, **k):
    calls.append(a[1])
    if len(calls) == 4:
        raise RuntimeError("simulated crash (power cut) on the 4th applicant")
    return real_verify(*a, **k)


batch.BATCH_DIR.joinpath("Bcrash.json").unlink(missing_ok=True)
pipeline.verify = crashing
try:
    batch.verify_batch([(b"", f) for f in P], consent=True, claims_override=P, batch_id="Bcrash", do_outreach=False, workers=1)
except RuntimeError as e:
    print("crashed:", e)
done_before = len(batch.load("Bcrash")["runs"])
calls.clear()
pipeline.verify = real_verify
pipeline.verify = lambda *a, **k: (calls.append(a[1]), real_verify(*a, **k))[1]
b = batch.verify_batch([(b"", f) for f in P], consent=True, claims_override=P, batch_id="Bcrash", do_outreach=False, workers=1)
print(f"checked before crash: {done_before} · re-checked after resume: {len(calls)} of {len(P)} · final status: {b['status']} {b['counts']}")
assert len(calls) == len(P) - done_before and b["status"] == "done"
print("OK: resume continues where it stopped")
