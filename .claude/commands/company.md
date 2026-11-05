---
description: Check one company with Pulse and explain the verdict with its sources
argument-hint: <company name> [website]
---
Check the company in $ARGUMENTS with Pulse:

```
PYTHONIOENCODING=utf-8 .venv/Scripts/python -c "import sys, json; from pramaan import pulse, companies; from pramaan.crustdata import CallLog; a=sys.argv[1:]; log=CallLog(); r=pulse.check_company(a[0], a[1] if len(a)>1 else None, companies.CompanyCache(log), log); print(json.dumps({k: r[k] for k in ('status','confidence','why','since','zombie')}, ensure_ascii=False)); [print(('USED ' if s['used'] else '     ')+s['kind'], s.get('tier'), s.get('publisher'), s.get('published') or '', s.get('url') or '') for s in r['signals']]" "<name>" "<website>"
```

Then tell the user the status, confidence and reason, and list the sources that decided it (publisher, date, link).
Point out anything that looks wrong (a namesake, a weak source, a creditor-not-debtor court case) rather than hiding it.
