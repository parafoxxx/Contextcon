"""Offline tests of Pulse's rules, source grading, name matching and exactly-once ledger (no network, no keys).
Run:  .venv/Scripts/python -m tests.test_pulse"""
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from pramaan import pulse, pulse_batch, sources  # noqa: E402
from pramaan.matching import same_company  # noqa: E402
from pramaan.pulse import Signal  # noqa: E402


def S(kind, pub="economictimes.com", tier="news", says="x"):
    return Signal(kind=kind, strength="strong", says=says, source="crustdata:/web/search/live", publisher=pub, tier=tier)


def D(kind, says="x", value=None, source="crustdata:/company/enrich"):
    return Signal(kind=kind, strength="support", says=says, source=source, publisher="Crustdata", tier="data", value=value)


ALIVE = [D("alive", "16 open job postings."), D("alive", "Website is up.", source="http")]
cases = [
    ("2 news outlets + website down -> DEAD", [S("shutdown", "livemint.com"), S("shutdown", "inc42.com"), D("website")], "DEAD"),
    ("same outlet 3 times counts once -> DISTRESSED", [S("shutdown"), S("shutdown"), S("shutdown"), D("website")], "DISTRESSED"),
    ("LinkedIn + Reddit only -> REVIEW, never DEAD", [S("shutdown", "linkedin.com", "weak"), S("shutdown", "reddit.com", "weak"), D("website")], "REVIEW"),
    ("2 trade outlets only -> not DEAD", [S("shutdown", "apparelresources.com", "trade"), S("shutdown", "fibre2fashion.com", "trade"), D("website")], "DISTRESSED"),
    ("1 major + 1 trade + data -> DEAD", [S("shutdown", "mothership.sg"), S("shutdown", "apparelresources.com", "trade"), D("website")], "DEAD"),
    ("shutdown + insolvency from ONE outlet -> not DEAD", [S("shutdown"), S("insolvency"), D("website")], "DISTRESSED"),
    ("2 outlets, no data confirmation -> DISTRESSED", [S("shutdown", "livemint.com"), S("shutdown", "inc42.com")], "DISTRESSED"),
    ("2 outlets but open jobs + site up -> REVIEW", [S("shutdown", "livemint.com"), S("shutdown", "inc42.com")] + ALIVE, "REVIEW"),
    ("one insolvency mention + signs of life -> ALIVE", [S("insolvency")] + ALIVE, "ALIVE"),
    ("official court record -> INSOLVENT", [S("legal", "nclt.gov.in", "official")], "INSOLVENT"),
    ("court filing only, but operating -> REVIEW", [S("legal", "nclt.gov.in", "official")] + ALIVE, "REVIEW"),
    ("court filing, site up but no open jobs (Zomato) -> REVIEW", [S("legal", "nclt.gov.in", "official"), D("hiring_freeze"),
                                                                   D("alive", "Website is up.", source="http")], "REVIEW"),
    ("news found but unread -> REVIEW, never ALIVE", [D("unread")] + ALIVE, "REVIEW"),
    ("acquired, 1 outlet -> REVIEW", [S("acquired")], "REVIEW"),
    ("acquired, 2 outlets -> ACQUIRED", [S("acquired", "livemint.com"), S("acquired", "techcrunch.com")], "ACQUIRED"),
    ("acquired + website redirects -> ACQUIRED", [S("acquired"), D("website", value="redirects to x.com", source="http")], "ACQUIRED"),
    ("renamed -> RENAMED", [S("renamed", says="Renamed to Eternal: x")], "RENAMED"),
    ("no evidence, no record -> CANT_TELL", [], "CANT_TELL"),
    ("signs of life only -> ALIVE", ALIVE, "ALIVE"),
]
bad = 0
for name, sigs, want in cases:
    got, why, conf, _ = pulse.decide(sigs, None, found=bool(sigs))
    ok = got == want
    bad += not ok
    print(f"{'✓' if ok else '✗'} {name}: {got} ({conf}) — {why}")

for pub, want in [("nclt.gov.in", "official"), ("economictimes.indiatimes.com", "news"), ("tech.eu", "news"),
                  ("linkedin.com", "weak"), ("reddit.com", "weak"), ("apparelresources.com", "trade"), ("wikiwand.com", "weak")]:
    ok = sources.tier(pub) == want
    bad += not ok
    print(f"{'✓' if ok else '✗'} tier({pub}) = {sources.tier(pub)}")
ok = sources.published("May 20, 2025 — UK software startup...") == "May 20, 2025"
bad += not ok
print(f"{'✓' if ok else '✗'} article date read from the result: {sources.published('May 20, 2025 — UK software startup...')}")

for a, b, want in [("Byjus", "BYJU'S", True), ("BYJU'S (Think & Learn Pvt Ltd)", "BYJU'S", True),
                   ("Flipkart Internet Pvt. Ltd.", "Flipkart", True), ("NoBroker", "NEOBROKER", False),
                   ("Chill Hai", "CHILL", False), ("Zomato", "Eternal Ltd", True)]:
    ok = same_company(a, b) == want
    bad += not ok
    print(f"{'✓' if ok else '✗'} same_company({a!r}, {b!r}) = {same_company(a, b)}")

with tempfile.TemporaryDirectory() as d:
    pulse_batch.LEDGER = Path(d) / "ledger.json"
    first = pulse_batch.act_once("k1", {"kind": "pause_sequence"})
    second = pulse_batch.act_once("k1", {"kind": "pause_sequence"})
    ok = first == "done" and second.startswith("already done")
    bad += not ok
    print(f"{'✓' if ok else '✗'} exactly-once: first={first!r}, retry={second!r}")

# Where a dead company's people went (alumni -> new leads), from profiles already fetched
from pramaan import guard  # noqa: E402
from pramaan.normalize import Person, Role  # noqa: E402


def P(nm, past, now, cid=None):
    roles = [Role(company=past, title="Engineer", start_raw=None, end_raw="2025", start=None, end=1, is_current=False)]
    if now:
        roles.append(Role(company=now, title="SDE", start_raw=None, end_raw=None, start=None, end=None, company_id=cid, is_current=True))
    return Person(person_id=None, name=nm, profile_url=None, headline=None, location=None, roles=roles)


al = pulse.alumni_destinations([P("a", "Builder.ai", "Microsoft", 1), P("b", "Builder.ai", "Microsoft Corporation", 1),
                                P("c", "Builder.ai", "Swiggy"), P("d", "Builder.ai", None), P("e", "Builder.ai", "Builder.ai"),
                                P("f", "Koo", "Zomato")], "Builder.ai")
top = al["top_destinations"]
ok = al["moved"] == 3 and top[0]["count"] == 2 and top[1]["company"] == "Swiggy" and al["companies"] == 2
bad += not ok
print(f"{'✓' if ok else '✗'} alumni: {al['moved']} moved, top {[(d['company'], d['count']) for d in top]}")

# Pre-send guard for outreach agents
accts = [{"status": "DEAD", "input": {"name": "Builder.ai", "domain": "builder.ai"}, "canonical": {"name": "Builder.ai"},
          "people": {"alumni": al}, "signals": [{"used": True, "url": "https://x", "publisher": "reuters.com"}]},
         {"status": "ALIVE", "input": {"name": "Swiggy", "domain": "swiggy.com"}, "canonical": {}, "signals": []},
         {"status": "ACQUIRED", "input": {"name": "Ezetap", "domain": "ezetap.com"}, "canonical": {}, "signals": []}]
for to, want in [("priya@builder.ai", "block"), ("ops@in.swiggy.com", "allow"), ("Ezetap", "update"),
                 ("someone@gmail.com", "unknown"), ("x@neverseen.io", "unknown"), ("Builder", "unknown")]:
    n, d = guard.parse_target(to)
    g = guard.answer(to, None if d in guard.FREE_MAIL else guard.lookup(n, d, accts))
    ok = g["decision"] == want and (want != "block" or g["instead"][0]["company"] == "Microsoft")
    bad += not ok
    print(f"{'✓' if ok else '✗'} guard({to!r}) = {g['decision']}")

print("\nALL PASS" if not bad else f"\n{bad} FAILED")
sys.exit(1 if bad else 0)
