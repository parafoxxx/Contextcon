"""Who said it, when, and how much to trust it.

Every web result is turned into a citable source: the real article URL (search redirects resolved),
the publisher, the publication date when the result states one, and a trust tier:

  official  court or company-registry sites (NCLT, IBBI, MCA, Companies House, ...)
  news      major news outlets
  trade     other publications: trade, regional and niche press (anything not known to be weak)
  weak      social media, forums, wikis, blogs and company directories

Weak sources are shown but never counted. A verdict needs 2+ distinct publishers including an official or major
outlet, or 3+ other publications (see pulse.enough).
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from . import config

OFFICIAL = re.compile(r"(^|\.)(nclt\.gov\.in|nclat\.nic\.in|ibbi\.gov\.in|mca\.gov\.in|sci\.gov\.in|ecourts\.gov\.in|"
                      r"company-information\.service\.gov\.uk|find-and-update\.company-information\.service\.gov\.uk|"
                      r"thegazette\.co\.uk|courtlistener\.com|sec\.gov)$", re.I)
NEWS = {
    # India
    "economictimes.indiatimes.com", "economictimes.com", "livemint.com", "moneycontrol.com", "business-standard.com",
    "thehindubusinessline.com", "thehindu.com", "hindustantimes.com", "indianexpress.com", "timesofindia.indiatimes.com",
    "ndtv.com", "ndtvprofit.com", "inc42.com", "entrackr.com", "yourstory.com", "the-ken.com", "medianama.com",
    "businesstoday.in", "financialexpress.com", "fortuneindia.com", "forbesindia.com", "cnbctv18.com", "theprint.in",
    "scroll.in", "outlookbusiness.com", "themorningcontext.com", "deccanherald.com", "news18.com", "indiatoday.in",
    "thehindubusinessline.com", "bqprime.com", "vccircle.com", "dealstreetasia.com",
    # International
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com", "techcrunch.com", "theverge.com", "forbes.com",
    "businessinsider.com", "bbc.com", "bbc.co.uk", "theguardian.com", "nytimes.com", "restofworld.org", "tech.eu",
    "sifted.eu", "axios.com", "theinformation.com", "fortune.com", "apnews.com", "aljazeera.com", "straitstimes.com",
    # Asia
    "storyboard18.com", "mothership.sg", "channelnewsasia.com", "techinasia.com", "e27.co", "businesstimes.com.sg",
    "scmp.com", "asia.nikkei.com", "nikkei.com", "theedgesingapore.com", "kr-asia.com",
}
WEAK_HINT = re.compile(r"(linkedin|reddit|medium|quora|twitter|x\.com|facebook|instagram|youtube|substack|blogspot|"
                       r"wordpress|tracxn|crunchbase|zaubacorp|tofler|glassdoor|ambitionbox|wikipedia|wikiwand|fandom|pitchbook|cbinsights|"
                       r"owler|zoominfo|google\.com|bing\.com)", re.I)
DATE_HEAD = re.compile(r"^\s*((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, \d{4})\s*[—–-]")

_cache_file = config.CACHE_DIR / "resolved_urls.json"
_lock = threading.Lock()
try:
    _resolved: dict[str, str] = json.loads(_cache_file.read_text(encoding="utf-8"))
except (FileNotFoundError, ValueError):
    _resolved = {}


def resolve(url: str) -> str:
    """Search results often link through a redirect (google.com/goto?...). Follow it once to the real article."""
    if not url or "google.com/goto" not in url:
        return url
    with _lock:
        if url in _resolved:
            return _resolved[url]
    real = url
    for attempt in range(4):  # parallel runs get throttled by the redirector: back off and retry
        if attempt:
            time.sleep(1.5 * attempt)
        try:
            r = httpx.get(url, follow_redirects=False, timeout=8, headers={"user-agent": "Mozilla/5.0"})
            loc = r.headers.get("location")
            if loc and loc.startswith("http"):
                real = loc
                break
            m = re.search(r"https?://(?!www\.google\.)[^\"'<> ]+", r.text[:5000])  # some redirects are a page with the link
            if m:
                real = m.group(0)
                break
        except httpx.HTTPError:
            continue
    with _lock:
        if real == url:
            return url  # unresolved: not cached, so the next run tries again
        _resolved[url] = real
        _cache_file.write_text(json.dumps(_resolved, indent=0), encoding="utf-8")
    return real


def publisher(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    for p in ("www.", "m.", "amp."):
        host = host.removeprefix(p)
    return host


def tier(pub: str) -> str:
    if not pub:
        return "weak"
    if OFFICIAL.search(pub):
        return "official"
    if WEAK_HINT.search(pub):
        return "weak"
    if pub in NEWS or any(pub.endswith("." + n) for n in NEWS):
        return "news"
    return "trade"


def published(snippet: str) -> Optional[str]:
    m = DATE_HEAD.match(snippet or "")
    return m.group(1) if m else None


def cite(result: dict) -> dict:
    """A search result as a citable source."""
    url = resolve(result.get("url") or "")
    pub = publisher(url)
    return {"url": url, "publisher": pub, "tier": tier(pub), "published": published(result.get("snippet") or ""),
            "title": (result.get("title") or "").strip(), "snippet": (result.get("snippet") or "").strip()}
