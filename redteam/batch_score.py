"""Score a batch run against the pile's ground truth.

  python -m redteam.batch_score            # Gemini/Claude reads every PDF (full end to end)
  python -m redteam.batch_score --no-llm   # feed ground-truth claims (checks + rings only)

Writes data/batch_scoreboard.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pramaan import batch, config, rings
from pramaan.models import ResumeClaims

OUT = Path(__file__).parent / "batch_out"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--keep-memory", action="store_true", help="keep known fake employers from earlier batches")
    args = ap.parse_args()
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    if not args.keep_memory:
        rings.KNOWN_FAKES.unlink(missing_ok=True)
    files = [((OUT / e["file"]).read_bytes(), e["file"]) for e in manifest]
    override = None if not args.no_llm else {e["file"]: ResumeClaims.model_validate(e["claims"]) for e in manifest}
    bid = "Bscore" + ("_nollm" if args.no_llm else "")
    for f in config.RUNS_DIR.glob(f"{bid}_*.json"):
        f.unlink()
    (batch.BATCH_DIR / f"{bid}.json").unlink(missing_ok=True)
    b = batch.verify_batch(files, consent=True, claims_override=override, batch_id=bid, do_outreach=False,
                           emit=lambda k, m, level="info", data=None: print(f"[{k}] {m}") if k in ("cluster", "done", "error") else None)

    run_of = b["runs"]
    dec = {e["file"]: b["decisions"][run_of[e["file"]]] for e in manifest}
    clusters = {c["id"]: c for c in b["clusters"]}
    rows, t = [], {"ring_members": 0, "ring_held": 0, "honest": 0, "honest_held": 0, "cohorts": 0, "cohorts_explained": 0,
                   "liars": 0, "liars_flagged": 0, "dupes": 0, "dupes_consistent": 0}
    name_to_file = {e["name"]: e["file"] for e in manifest}
    for e in manifest:
        d = dec[e["file"]]
        ok = None
        if e.get("ring"):
            t["ring_members"] += 1
            ok = d["decision"] == "HOLD"
            t["ring_held"] += ok
        else:
            t["honest"] += 1 if not e.get("lie") else 0
            if not e.get("lie") and d["decision"] == "HOLD":
                t["honest_held"] += 1
        if e.get("cohort"):
            t["cohorts"] += 1
            c = clusters.get(d.get("cluster") or "")
            ok = bool(c and c["type"] == "explained_cohort") and d["decision"] != "HOLD"
            t["cohorts_explained"] += ok
        if e.get("lie"):
            t["liars"] += 1
            r = __import__("pramaan.pipeline", fromlist=["load"]).load(run_of[e["file"]])
            ok = d["decision"] in ("VERIFY", "HOLD") and any(v.status.value == "discrepancy" for v in r.verdicts)
            t["liars_flagged"] += ok
        if e["expect"] == "NOT_HOLD":
            ok = d["decision"] != "HOLD"
        if e["expect"].startswith("SAME_AS:"):
            t["dupes"] += 1
            other = dec[name_to_file[e["expect"].split(":", 1)[1]]]
            ok = other["decision"] == d["decision"]
            t["dupes_consistent"] += ok
        rows.append({"name": e["name"], "file": e["file"], "truth": e.get("ring") and f"ring {e['ring']}" or e.get("cohort")
                     or e.get("lie") or e["expect"].lower(), "decision": d["decision"], "why": d["why"], "ok": ok,
                     "run_id": run_of[e["file"]]})
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "batch_id": bid,
           "mode": config.MODE + (" (claims given)" if args.no_llm else f" ({config.LLM_PROVIDER} reads PDFs)"),
           "duration_s": b["duration_s"], "totals": t, "rows": rows}
    (config.DATA / "batch_scoreboard.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print()
    for r in rows:
        print(f"  {'✓' if r['ok'] in (True, None) else '✗'} {r['decision']:6} {r['name']:26} truth={r['truth']:18} {r['why'][:70]}")
    print(f"\nRing members held {t['ring_held']}/{t['ring_members']} · honest applicants held {t['honest_held']}/{t['honest']} · "
          f"layoff cohort explained {t['cohorts_explained']}/{t['cohorts']} · liars flagged {t['liars_flagged']}/{t['liars']} · "
          f"duplicate consistent {t['dupes_consistent']}/{t['dupes']} · {b['duration_s']}s")


if __name__ == "__main__":
    main()
