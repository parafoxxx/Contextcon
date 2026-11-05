"""Turn messy resume / API dates into a month index (year*12 + month-1)."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
PRESENT = {"present", "current", "now", "till date", "to date", "ongoing", "today", "currently"}


def today_index() -> int:
    t = date.today()
    return t.year * 12 + t.month - 1


def to_index(s: Optional[str], *, is_end: bool = False) -> Optional[int]:
    """Parse 'Mar 2019', 'March, 2019', '03/2019', '2019-03', '2019-03-01T00:00:00+00:00',
    "Mar '19", '2019', 'Present'. Year-only start -> Jan, year-only end -> Dec."""
    if s is None:
        return None
    raw = str(s).strip().lower()
    if not raw:
        return None
    if any(p in raw for p in PRESENT):
        return today_index()
    # ISO yyyy-mm(-dd...)
    m = re.match(r"^(\d{4})-(\d{1,2})", raw)
    if m:
        return int(m.group(1)) * 12 + int(m.group(2)) - 1
    # mm/yyyy or mm-yyyy or mm.yyyy
    m = re.match(r"^(\d{1,2})[/\-.](\d{4})$", raw)
    if m:
        return int(m.group(2)) * 12 + int(m.group(1)) - 1
    # month name + year (4 or 2 digits)
    m = re.search(r"([a-z]{3})[a-z]*\.?,?\s*'?(\d{4}|\d{2})\b", raw)
    if m and m.group(1) in MONTHS:
        y = int(m.group(2))
        if y < 100:
            y += 2000 if y < 50 else 1900
        return y * 12 + MONTHS[m.group(1)] - 1
    m = re.search(r"\b(19\d{2}|20\d{2})\b", raw)
    if m:
        return int(m.group(1)) * 12 + (11 if is_end else 0)
    return None


def fmt(idx: Optional[int]) -> str:
    if idx is None:
        return "?"
    y, m = divmod(idx, 12)
    return f"{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][m]} {y}"


def year_only(s: Optional[str]) -> bool:
    return bool(s) and bool(re.fullmatch(r"\s*\d{4}\s*", str(s)))
