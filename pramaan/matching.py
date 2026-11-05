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


# Extra words that don't change which company is meant ("Flipkart India" == "Flipkart")
GENERIC = {"internet", "digital", "ventures", "enterprises", "industries", "international", "online",
           "pvt", "ltd", "hq", "official"}


def same_company(a: str, b: str) -> bool:
    """Strict on purpose: a wrong match produces a wrong 'Verified', which is worse than 'Unverified'.
    'Chill Hai' != 'CHILL', 'NoBroker HR' != 'NoBroker', 'SPO IIT Kanpur' != 'IIT'."""
    ca, cb = clean(a), clean(b)
    if not ca or not cb:
        return False
    if ca == cb or ca.replace(" ", "") == cb.replace(" ", ""):
        return True
    if RENAMES.get(ca) == cb or RENAMES.get(cb) == ca or (RENAMES.get(ca) and RENAMES.get(ca) == RENAMES.get(cb)):
        return True
    ta, tb = set(ca.split()), set(cb.split())
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if small < big and (big - small) <= GENERIC:
        return True
    # No fuzzy fallback: on real data it matched 'NoBroker' to 'NEOBROKER'. A miss goes to the employer instead.
    return False


def name_similarity(a: str, b: str) -> int:
    return int(fuzz.token_sort_ratio((a or "").lower(), (b or "").lower()))
