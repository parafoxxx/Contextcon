"""Company-name matching that survives 'Flipkart Internet Pvt. Ltd.' vs 'Flipkart',
renames (Facebook -> Meta) and staffing-firm placements."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

SUFFIXES = r"\b(private|pvt|limited|ltd|inc|incorporated|llc|llp|corp|corporation|co|company|plc|gmbh|sa|ag|bv|" \
           r"technologies|technology|tech|solutions|services|software|systems|labs|india|global|group|the)\b"

# True renames / legal names only (not acquisitions: a claim at an acquired brand should match that brand)
RENAMES = {
    "facebook": "meta", "meta platforms": "meta", "twitter": "x",
    "paytm": "one97 communications", "one97": "one97 communications", "ola": "ani",
    "zomato": "eternal",
}


SCHOOL_ABBREV = {
    r"\biit\b": "indian institute of technology", r"\biiit\b": "international institute of information technology",
    r"\bnit\b": "national institute of technology", r"\biim\b": "indian institute of management",
    r"\bbits\b": "birla institute of technology and science", r"\biisc\b": "indian institute of science",
    r"\bdtu\b": "delhi technological university", r"\bdu\b": "delhi university",
    r"\buniversity of delhi\b": "delhi university",
}


def clean(name: str) -> str:
    t = (name or "").lower()
    for pat, rep in SCHOOL_ABBREV.items():
        t = re.sub(pat, rep, t)
    t = re.sub(r"[^\w\s&]", " ", t)
    t = re.sub(SUFFIXES, " ", t)
    return re.sub(r"\s+", " ", t).strip()


def same_company(a: str, b: str) -> bool:
    ca, cb = clean(a), clean(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    if RENAMES.get(ca) == cb or RENAMES.get(cb) == ca or (RENAMES.get(ca) and RENAMES.get(ca) == RENAMES.get(cb)):
        return True
    # short names need exact-ish match to avoid 'Ola' == 'Olacabs Holdings' false positives
    if min(len(ca), len(cb)) <= 4:
        return ca.split()[0] == cb.split()[0]
    return fuzz.token_set_ratio(ca, cb) >= 88 or fuzz.ratio(ca, cb) >= 90


def name_similarity(a: str, b: str) -> int:
    return int(fuzz.token_sort_ratio((a or "").lower(), (b or "").lower()))
