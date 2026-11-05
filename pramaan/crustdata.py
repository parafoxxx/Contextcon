"""Crustdata API client (x-api-version 2025-11-01).

Modes (PRAMAAN_MODE):
  live    -> real API; every response is cached to data/cache and reused on repeat
  replay  -> cache only (for the stage, if WiFi dies)
  fixture -> a local fake that serves fixtures/*.json in the same response shapes

Every call returns (data, meta). Failures never raise: data is None and meta["error"]
explains why, so the checks can degrade to UNVERIFIED instead of crashing.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

import httpx

from . import config

BASE = "https://api.crustdata.com"
HEADERS = {"x-api-version": "2025-11-01", "content-type": "application/json"}


class CallLog:
    """Per-run ledger of every external call: shown in the UI and used for citations."""

    def __init__(self):
        self.calls: list[dict] = []
        self.credits = 0

    def add(self, entry: dict):
        self.calls.append(entry)
        self.credits += int(entry.get("credits") or 0)


def _key(path: str, body: dict) -> str:
    blob = json.dumps({"p": path, "b": body}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _post(path: str, body: dict, log: Optional[CallLog] = None) -> tuple[Any, dict]:
    key = _key(path, body)
    cache_file = config.CACHE_DIR / f"{key}.json"
    meta = {"endpoint": path, "cache_key": key, "body": body}

    if config.MODE == "fixture":
        from .fixture_api import handle
        data = handle(path, body)
        meta.update(source="fixture", credits=0)
        if log:
            log.add(meta)
        return data, meta

    if cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        meta.update(source="cache", credits=0)
        if log:
            log.add(meta)
        return data, meta

    if config.MODE == "replay":
        meta.update(source="cache-miss", error="not cached (replay mode)")
        if log:
            log.add(meta)
        return None, meta

    if not config.CRUSTDATA_API_KEY:
        meta.update(error="CRUSTDATA_API_KEY not set")
        if log:
            log.add(meta)
        return None, meta

    headers = {**HEADERS, "authorization": f"Bearer {config.CRUSTDATA_API_KEY}"}
    last_err = None
    for attempt in range(3):
        try:
            r = httpx.post(BASE + path, json=body, headers=headers, timeout=45)
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                meta.update(error=f"HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
                if log:
                    log.add(meta)
                return None, meta
            data = r.json()
            cache_file.write_text(json.dumps(data, indent=1), encoding="utf-8")
            meta.update(source="live", credits=int(r.headers.get("x-credits-used", 0) or 0))
            if log:
                log.add(meta)
            return data, meta
        except (httpx.HTTPError, ValueError) as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
    meta.update(error=last_err or "unknown error")
    if log:
        log.add(meta)
    return None, meta


# ---------- Endpoints ----------

def enrich_person(profile_url: str, log=None):
    return _post("/person/enrich", {
        "professional_network_profile_urls": [profile_url],
        "fields": ["basic_profile", "experience", "education", "social_handles"],
    }, log)


def search_people(filters: dict, limit: int = 10, log=None):
    return _post("/person/search", {"filters": filters, "limit": limit}, log)


def enrich_company(*, name: str = None, domain: str = None, company_id: int = None, log=None):
    body: dict = {"fields": ["basic_info", "headcount", "funding"]}
    if company_id:
        body["crustdata_company_ids"] = [int(company_id)]
    elif domain:
        body["domains"] = [domain]
    else:
        body["names"] = [name]
    return _post("/company/enrich", body, log)


def web_search(query: str, log=None):
    return _post("/web/search/live", {"query": query, "sources": ["web", "news"]}, log)
