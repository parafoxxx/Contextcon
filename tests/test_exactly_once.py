"""Crash/retry safety: re-running the same run never sends an email twice, and a crashed send
is never retried blindly.  python -m tests.test_exactly_once"""
import os
import sys

os.environ["PRAMAAN_MODE"] = "fixture"
sys.stdout.reconfigure(encoding="utf-8")

from pramaan import config, outreach, pipeline  # noqa: E402
from tests.smoke_fixture import liar  # noqa: E402

sent = []
config.SMTP_HOST, config.DEMO_INBOX = "smtp.fake", "team@example.com"
outreach.config.SMTP_HOST, outreach.config.DEMO_INBOX = config.SMTP_HOST, config.DEMO_INBOX


def fake_send(to, subject, body, ref):
    sent.append(ref)
    return "sent"


outreach._send = fake_send
outreach.LEDGER.unlink(missing_ok=True)

r1 = pipeline.verify(b"", "x.pdf", consent=True, claims=liar, run_id="exactly_once")
first = len(sent)
r2 = pipeline.verify(b"", "x.pdf", consent=True, run_id="exactly_once")  # retry: no claims passed -> resumes
print("run 1 sent:", first, "| run 2 sent:", len(sent) - first)
print("run 2 deliveries:", [o["delivery"] for o in r2.outreach])
assert first >= 1 and len(sent) == first, "an email was sent twice!"
assert r2.claims and r2.claims.name == liar.name, "resume did not reuse saved claims"

# Crash mid-send: ledger says 'sending' -> must not resend
key = r1.outreach[0]["id"]
outreach._ledger_set(key, {"status": "sending"})
print("after simulated crash:", outreach.deliver_once(key, "team@example.com", "s", "b"))
assert len(sent) == first
print("OK: exactly-once holds across retries and crashes")
