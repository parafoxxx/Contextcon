"""Title normalization so 'SDE-2' == 'Software Engineer II', and a rough seniority ladder."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

ABBREV = {
    r"\bsde\b": "software development engineer",
    r"\bswe\b": "software engineer",
    r"\bsr\.?\b": "senior",
    r"\bjr\.?\b": "junior",
    r"\bmgr\b": "manager",
    r"\beng\b": "engineer",
    r"\bvp\b": "vice president",
    r"\bavp\b": "assistant vice president",
    r"\bsvp\b": "senior vice president",
    r"\bevp\b": "executive vice president",
    r"\bpm\b": "product manager",
    r"\bem\b": "engineering manager",
    r"\bcto\b": "chief technology officer",
    r"\bceo\b": "chief executive officer",
    r"\bcoo\b": "chief operating officer",
    r"\bcfo\b": "chief financial officer",
    r"\bml\b": "machine learning",
    r"\bassoc\.?\b": "associate",
    r"\bdev\b": "developer",
    r"\bsoftware development engineer\b": "software engineer",
    r"\bmember of technical staff\b": "software engineer",
    r"\bmts\b": "software engineer",
}
ROMAN = {r"\b(i|1)\b": "1", r"\b(ii|2)\b": "2", r"\b(iii|3)\b": "3", r"\b(iv|4)\b": "4"}

# Higher = more senior. Order matters: first match wins.
LADDER = [
    (9, r"chief|founder|co-founder|president\b(?!.*vice)|managing director|\bpartner\b"),
    (8, r"senior vice president|executive vice president|\bsvp\b|\bevp\b"),
    (7, r"vice president|head of|\bdirector\b"),
    (6, r"senior manager|engineering manager|group manager|principal|staff engineer|architect"),
    (5, r"\bmanager\b|\blead\b|team lead"),
    (4, r"senior|\b3\b"),
    (3, r"\b2\b|associate (product )?manager|analyst 2|\bconsultant\b"),
    (2, r"engineer|developer|analyst|associate|executive|specialist|designer|scientist|consultant"),
    (1, r"intern|trainee|apprentice|fellow|student"),
]


def normalize(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"[\-_/,|()]", " ", t)
    for pat, rep in ABBREV.items():
        t = re.sub(pat, rep, t)
    for pat, rep in ROMAN.items():
        t = re.sub(pat, rep, t)
    return re.sub(r"\s+", " ", t).strip()


def seniority(title: str) -> int:
    t = normalize(title)
    for rank, pat in LADDER:
        if re.search(pat, t):
            return rank
    return 2


def same_role(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    return na == nb or fuzz.token_set_ratio(na, nb) >= 85


def is_exec_or_founder(title: str) -> bool:
    return seniority(title) >= 7
