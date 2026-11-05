# Pulse: project guide for Claude

Pulse finds dead companies that a CRM or data graph (Crustdata) still lists as active, cites the evidence for every
verdict, and cleans up the CRM. Built at ContextCon 04 (IIT Kanpur, Nov 5–6 2026). Repo: https://github.com/parafoxxx/Contextcon

## Commands

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # first time
cp .env.example .env                                                    # then fill in keys (never commit .env)
.venv/Scripts/python -m uvicorn app:app --port 8000 --reload           # app at http://localhost:8000
.venv/Scripts/python -m tests.test_pulse                                # offline tests, must end "ALL PASS"
```

Run the sample CRM from code (cached after the first run, ~7 s; fresh ~3 min):
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python -c "from pramaan import pulse_batch; r = pulse_batch.run(open('sample/crm_sample.csv', encoding='utf-8').read(), job_id='Psample'); print(r['summary'])"
```
Score against hand labels in `sample/crm_truth.json` (current: 15/16; Zepto goes to REVIEW, which is acceptable).

## Layout

| Path | What |
|---|---|
| `app.py` | FastAPI server: page `/`, `POST /api/pulse` (CSV or `sample=true`), `GET /api/stream/{job}` (SSE), `GET /api/pulse/{job}`, `/api/pulse/{job}/file/{cleaned.csv,corrections.jsonl,report.json}`, `POST /api/pulse/{job}/resume`, `POST /api/pulse_one` (name, domain) |
| `pramaan/pulse.py` | One company: evidence gathering, news reading, `decide()` rules, `check_company()` |
| `pramaan/pulse_batch.py` | Whole CRM: parse CSV, run accounts (3 parallel), merge duplicates, exactly-once actions (`act_once` ledger), write outputs |
| `pramaan/sources.py` | Citations: resolve google.com/goto redirects, publisher, article date, tier (official / news / trade / weak) |
| `pramaan/companies.py` | `CompanyCache.canonical_id`: free Company Identify, keeps the largest record with a website (stub duplicates exist, e.g. "Byjus" vs BYJU'S id 5705) |
| `pramaan/crustdata.py` | API client: disk cache in `data/cache`, retries on 429/5xx with `x-ratelimit-reset`, modes live / replay |
| `pramaan/llm.py` | Model calls with structured output; every reading cached on its exact input; Gemini fallback chain |
| `pramaan/normalize.py`, `matching.py`, `dates.py` | Response parsing, strict company-name matching, date parsing |
| `web/pulse.html` | The single page (vanilla JS, no build step) |
| `sample/` | `crm_sample.csv` (18 real Indian companies) and `crm_truth.json` (hand labels we're confident of) |
| `tests/test_pulse.py` | Offline tests of rules, tiers, matching, ledger |
| `data/` | Runtime only, gitignored: `cache/`, `pulse/<job>/`, `pulse/crm_ledger.json` |

## Principles (don't break these)

- **The model only reads; rules decide.** `decide()` in `pulse.py` sets the status. The model answers narrow questions about search results (debtor or creditor? whole company or one product? subsidiary / solvent liquidation? acquisition still current? same company or a namesake?) and must quote.
- **Every verdict cites what decided it.** Signals carry `publisher`, `tier`, `published`, `url`, `quote`, `used`. The UI shows "Decided by" vs "Also considered".
- **Weak sources never count** (LinkedIn, Reddit, wikis, directories). DEAD / INSOLVENT / ACQUIRED need `enough()`: 2+ distinct publishers incl. an official or major outlet, or 3+ other publications. The same outlet repeated counts once.
- **DEAD also needs data corroboration** (website down, hiring stopped, headcount collapse). **Signs of life veto DEAD**: open jobs + working website → REVIEW.
- **A court filing alone at an operating company → REVIEW**, not INSOLVENT (NCLT also hears mergers / name changes).
- **Zombie = DEAD while Crustdata says active.** Insolvent / acquired companies still operate; their staff are not zombies.
- **Strict company matching, no fuzzy matching.** Fuzzy matched NoBroker→NEOBROKER and Chill Hai→CHILL; a wrong match gives a confident wrong verdict.
- **Exactly once.** CRM actions are written to the ledger before they happen; re-runs must repeat nothing.
- **Hackathon rule: agents talk only to the team**, never real outsiders. Pulse sends no email; actions are recorded.

## Config (.env)

`LLM_PROVIDER=gemini|anthropic`, `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-3.5-flash`), `GEMINI_FALLBACKS`,
`ANTHROPIC_API_KEY` (model `claude-opus-5-5`), `CRUSTDATA_API_KEY`, `PRAMAAN_MODE=live|replay`. Values are trimmed of spaces/quotes.

## Gotchas learned the hard way

- **Crustdata** (`x-api-version: 2025-11-01`, `Authorization: Bearer`): history is `headcount.timeseries` (weekly, from Feb 2023), funding rounds are `funding.milestones`, `x-credits-used` is a **decimal** string. Rate limits: Enrich 15/min, Search 30/min, Web 10/min (the bottleneck). Company Identify is free. Profile headcount **lags reality** (Builder.ai's kept rising after bankruptcy), so it's never proof of life.
- **Gemini:** `gemini-2.5-flash` is retired for new keys; newer models return 503/504 under load, so keep the fallback chain and the 45 s cap. Keep **one** client alive behind a lock (a client that goes out of scope closes mid-request; parallel threads closed each other's).
- **Windows + Git Bash:** long `python - <<'EOF'` heredocs with quotes or backslashes often break. Write patch scripts to a file and run them. Use `PYTHONIOENCODING=utf-8` when printing curly quotes.
- **uvicorn `--reload`** may not notice deleted files; restart the server after big refactors.
- Web-search links are often `google.com/goto?url=…`; `sources.resolve()` follows the 302 to the real article (cached in `data/cache/resolved_urls.json`).

## Status and next steps

Done: CRM check, check-any-company, cited and graded sources, confidence, duplicate merge, exactly-once actions, cleaned CSV, correction feed, resume, offline tests, README.
Ideas: Crustdata watchers for re-checks; MCA company status as a second official source; Person Enrich to find where contacts at dead companies moved; a larger labelled set built by someone who didn't write the rules.
Open item: the README's first line is a placeholder timing note about the 10:37 PM first commit; update it with the organisers' answer.
