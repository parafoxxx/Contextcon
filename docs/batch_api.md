# Batch API (for the UI)

- `POST /api/batch` multipart: `files` (many), `consent=true` -> `{batch_id, count}`
- `GET /api/stream/{batch_id}` SSE events `{t, kind, msg, level, data}`; kinds: `batch`, `applicant` (data: run_id, overall),
  `graph`, `cluster` (data: cluster object), `error`, `done` (data: batch_id, counts)
- `GET /api/batch/{batch_id}` -> 
  - `applicants`: `{run_id: {name, overall, summary, file}}`
  - `decisions`: `{run_id: {decision: CLEAR|VERIFY|HOLD, why, cluster}}`
  - `clusters`: `[{id, type: ring|watch|explained_cohort|cohort, members:[run_id], signals:[...], reason, phantom_employers?, employer?, evidence?, links:[{a,b,kind,detail}]}]`
  - `links`: every link `{a, b, kind, detail}`; kinds: phantom_employer, shared_reference, shared_contact, shared_profile_url,
    near_duplicate_text, pdf_metadata, shared_small_employer, shared_employer
  - `counts`, `duration_s`, `status`
- Per-applicant drill-down: `GET /api/report/{run_id}` (existing single-resume report)
- Offline test data: `python -m tests.batch_fixture` writes batch `Btest` (open `/api/batch/Btest`)
