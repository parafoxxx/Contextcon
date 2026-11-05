"""Run one resume end to end from the command line: python -m tests.run_one path/to/resume.pdf"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pramaan import pipeline  # noqa: E402

path = sys.argv[1]


def emit(kind, msg, level="info", data=None):
    print(f"[{kind}] {msg}")


r = pipeline.verify(open(path, "rb").read(), path, consent=True, emit=emit, do_outreach="--outreach" in sys.argv)
print(f"\n== {r.overall} | {r.summary} | {r.duration_s}s | credits {r.credits_used} | identity {r.identity}")
for v in r.verdicts:
    print(f"  [{v.status.value:11}] {v.claim_id:20} {v.claim_text[:60]:60} | {v.reason}")
