---
description: Verify Pulse end to end (offline tests, then the sample CRM against hand labels)
---
1. Run `.venv/Scripts/python -m tests.test_pulse`. It must end with `ALL PASS`; if not, report the failing lines.
2. Run the sample CRM and score it against `sample/crm_truth.json`:
   ```
   PYTHONIOENCODING=utf-8 .venv/Scripts/python -c "import json; from pramaan import pulse_batch; t=json.load(open('sample/crm_truth.json',encoding='utf-8')); r=pulse_batch.run(open('sample/crm_sample.csv',encoding='utf-8').read(), job_id='Pcheck'); ok=[a['status'] in t[a['input']['name']] for a in r['accounts'] if a['input']['name'] in t and not a.get('merged_into_row')]; print(sum(ok),'/',len(ok)); print(r['summary'])"
   ```
   Expected: about 15/16 with the mismatch going to REVIEW, never a wrong DEAD.
3. Report the score, any account whose status changed, and the sources that decided it.
