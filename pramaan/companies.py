"""One company, one record: resolve a name or domain to its canonical Crustdata company."""
from __future__ import annotations

import re
from typing import Optional

from . import crustdata as cd
from .matching import same_company


def _size_floor(rng) -> int:
    m = re.match(r"\s*([\d,]+)", str(rng or ""))
    return int(m.group(1).replace(",", "")) if m else 0


class CompanyCache:
    def __init__(self, log):
        self.log = log

    def canonical_id(self, name: str, domain: Optional[str] = None) -> Optional[int]:
        """Free Company Identify. Big brands have duplicate stub records ('Byjus', 'Think & Learn'), so gather every
        record whose name matches (also the brand and legal parts of 'BYJU'S (Think & Learn Pvt Ltd)') and keep
        the canonical one: the largest, with a website."""
        queries = [name]
        m = re.match(r"^(.*?)\s*\((.+)\)\s*$", name or "")
        if m:
            queries += [m.group(1), m.group(2)]
        cands = []
        for q in queries if not domain else [None]:
            idata, _ = cd.identify_company(name=q, domain=domain, log=self.log)
            for entry in idata or []:
                for mt in entry.get("matches") or []:
                    cd_ = mt.get("company_data") or {}
                    bi = cd_.get("basic_info") or {}
                    if cd_.get("crustdata_company_id") and (domain or same_company(bi.get("name") or "", name)
                                                             or same_company(bi.get("name") or "", q)):
                        cands.append((mt.get("confidence_score") or 0, _size_floor(bi.get("employee_count_range")),
                                      bool(bi.get("primary_domain")), cd_["crustdata_company_id"]))
        if not cands:
            return None
        cands.sort(key=lambda c: (c[0] >= 0.9, c[1], c[2]), reverse=True)
        return cands[0][3]
