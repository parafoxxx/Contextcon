"""Run every generated resume through Pramaan and score it against ground truth.

  python -m redteam.score            # full run: Claude reads each PDF (real end-to-end)
  python -m redteam.score --no-llm   # skip PDF reading; feed ground-truth claims (fast, free, tests the checks only)

Caught      = the planted lie's claim got DISCREPANCY or REVIEW from an expected check.
False flag  = a DISCREPANCY on a claim that was not planted.
Writes data/scoreboard.json (shown on the Scoreboard tab).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pramaan import config, pipeline
from pramaan.matching import same_company
from pramaan.models import ResumeClaims, Status as S

OUT = Path(__file__).parent / "out"


def job_company(report, claim_id: str) -> str | None:
    m = re.match(r"job(\d+)", claim_id)
    if not m or not report.claims:
        return None
    i = int(m.group(1)) - 1
    return report.claims.jobs[i].company if i < len(report.claims.jobs) else None


def on_target(report, v, entry) -> bool:
    if entry.get("target_edu"):
        return v.claim_id.startswith("edu")
    tc = entry.get("target_company")
    if not tc:
        return False
    if "+" in v.claim_id:  # overlap verdicts touch two jobs
        return any(same_company(job_company(report, part) or "", tc) for part in v.claim_id.split("+"))
    return same_company(job_company(report, v.claim_id) or "", tc)


def score_one(entry, use_llm: bool):
    pdf = OUT / entry["file"]
    claims = None if use_llm else ResumeClaims.model_validate(entry["claims"])
    r = pipeline.verify(pdf.read_bytes(), pdf.name, consent=True, claims=claims, run_id=f"rt_{entry['name']}",
                        do_outreach=False)
    caught_by = None
    if entry["lie"]:
        for v in r.verdicts:
            if v.status in (S.DISCREPANCY, S.REVIEW) and on_target(r, v, entry) and v.check in entry["expected_checks"]:
                caught_by = f"{v.check} ({v.status.value})"
                break
    false_flags = [f"{v.claim_id}: {v.reason[:90]}" for v in r.verdicts
                   if v.status == S.DISCREPANCY and not (entry["lie"] and on_target(r, v, entry))]
    to_review = [v.claim_id for v in r.verdicts if v.status == S.REVIEW and not (entry["lie"] and on_target(r, v, entry))]
    return {"name": entry["name"], "lie": entry["lie"], "layout": entry["layout"], "caught": bool(caught_by),
            "caught_by": caught_by, "false_flags": false_flags, "honest_to_review": to_review,
            "duration_s": r.duration_s, "overall": r.overall}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    with ThreadPoolExecutor(args.workers) as ex:
        rows = list(ex.map(lambda e: score_one(e, not args.no_llm), manifest))
    planted = [r for r in rows if r["lie"]]
    totals = {"planted": len(planted), "caught": sum(r["caught"] for r in planted),
              "false_flags": sum(len(r["false_flags"]) for r in rows),
              "honest_to_review": sum(len(r["honest_to_review"]) for r in rows),
              "median_s": round(statistics.median([r["duration_s"] for r in rows]), 1) if rows else 0}
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "mode": config.MODE + ("" if not args.no_llm else " (claims given, PDF reading skipped)"),
           "totals": totals, "runs": rows}
    (config.DATA / "scoreboard.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    for r in rows:
        mark = ("CAUGHT " + r["caught_by"]) if r["caught"] else ("MISSED" if r["lie"] else "honest")
        print(f"{r['name']:42} {mark:38} false_flags={len(r['false_flags'])} {r['duration_s']}s")
        for f in r["false_flags"]:
            print("    FALSE FLAG:", f)
    print(f"\nCaught {totals['caught']}/{totals['planted']} planted lies · {totals['false_flags']} honest claims flagged · "
          f"{totals['honest_to_review']} sent to human · median {totals['median_s']}s")


if __name__ == "__main__":
    main()
