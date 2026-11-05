# Pramaan: background verification an agent finishes in minutes

Background checks take firms 7–14 days per hire. Pramaan takes a resume and returns a finished report in minutes, with a cited verdict for every claim. Only the cases the evidence can't settle go to a human.

**The idea:** a resume and the candidate's own online profile are both written by the candidate. Agreement between them proves consistency, not truth. So Pramaan marks a claim **Verified** only when evidence the candidate *can't edit* agrees:

| Evidence | Controlled by | Catches |
|---|---|---|
| Company founded year, headcount, growth | the company | "Led 25 people" at a company that was too small; jobs at companies that didn't exist yet |
| Funding rounds with dates | investors / press | "Joined at seed" after the Series A |
| Other people's profiles overlapping the same team and time | third parties | roles on teams nobody else was on |
| Web search | third parties | fake employers with no trace anywhere |
| Employer / reference email replies | third parties | anything still unsettled |
| The candidate's own profile | the candidate | stretched dates, inflated titles (marked *Matches profile*, not *Verified*) |

## Flow

1. **Consent gate.** No check starts without recorded consent.
2. **Read** (Claude, `claude-opus-5-5`): PDF, scan or image becomes structured claims. Claude only reads; it never decides a verdict.
3. **Resolve identity.** Uses the profile URL if given, otherwise a name search scored by shared employers and dates. Common names that tie go to a human.
4. **Check** (deterministic Python, `pramaan/checks.py`): employer existence and founding date, dates and title against the profile, team size against headcount, claimed funding stage against the dates of real rounds, "employee #N", corroboration by colleagues, overlapping jobs, education, references.
5. **Outreach.** One email per former employer to an HR contact found through Crustdata people search, plus one email asking the candidate to explain discrepancies *before* HR sees them. Every email passes a **guard**: consent on file, never the current employer, no data about other employers, demo-inbox redirect.
6. **Close the loop.** Claude reads untidy replies and uploaded offer letters, quotes the exact text, and updates the verdict.

Every verdict carries evidence with `source`, `record id`, `field`, `value`, `controlled_by`, `retrieved_at`.

## Run

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY, CRUSTDATA_API_KEY, PRAMAAN_MODE=live
.venv/Scripts/python -m uvicorn app:app --port 8000     # open http://localhost:8000
```

Modes: `live` calls Crustdata and caches every response; `replay` reads the cache only (for the stage if WiFi fails); `fixture` uses synthetic offline data (`fixtures/dataset.json`, all fictional).

## Red team scoreboard

```bash
python -m redteam.generate --subjects redteam/subjects.json   # real careers of people who consented
python -m redteam.score                                        # Claude reads every PDF; writes data/scoreboard.json
python -m redteam.score --no-llm                               # checks only, free and fast
```

The generator plants one lie per resume: title inflation, date stretch, fake employer, team size, funding stage or graduation year. It also writes messy *honest* variants: legal company names, `03/2019` and "till date" dates, `SDE-2`-style titles, and two-column layouts. The score counts lies caught and honest claims wrongly flagged.

## Honest limits

- Crustdata's `2025-11-01` API returns current headcount plus trailing growth (up to 2 years), not a full monthly history. Older headcount is an estimate and is labelled with its method and confidence.
- Real employer email addresses need contact enrichment. In the demo every email goes to `DEMO_INBOX`.
