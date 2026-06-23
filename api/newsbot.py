"""
newsbot.py — Fetch civic/community/emergency news headlines from RSS feeds.
Returns a list of formatted ticker strings.

Usage:
  python3 ~/api/newsbot.py              # print headlines to stdout
  Called by /overlay/newsbot/run and /overlay/newsbot/preview API endpoints.
"""

import ssl
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import re

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

# ── RSS feeds (civic / community / emergency focus) ───────────────────────────
FEEDS = [
    ("NPR News",       "https://feeds.npr.org/1001/rss.xml"),
    ("BBC World",      "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("CBS News",       "https://www.cbsnews.com/latest/rss/main"),
    ("PBS NewsHour",   "https://www.pbs.org/newshour/feeds/rss/headlines"),
    ("The Guardian",   "https://www.theguardian.com/us/rss"),
    ("NY Times",       "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
]

# ── Civic-relevance keywords ──────────────────────────────────────────────────
CIVIC_KEYWORDS = {
    "community", "council", "city", "county", "municipal", "mayor", "vote",
    "election", "public", "emergency", "disaster", "resilience", "infrastructure",
    "housing", "water", "climate", "flood", "storm", "evacuation", "shelter",
    "food", "health", "safety", "police", "fire", "school", "education",
    "iowa", "local", "neighborhood", "volunteer", "nonprofit", "grant",
    "budget", "policy", "law", "senate", "congress", "legislation",
}

MAX_HEADLINES = 18
MAX_TITLE_LEN = 120


def _fetch_feed(name: str, url: str) -> list[str]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "CivicResilience-NewsBot/1.0"}
        )
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            content = resp.read()
        root = ET.fromstring(content)

        titles = []
        # RSS 2.0: items with <title>
        for item in root.findall(".//item"):
            t = item.findtext("title", "").strip()
            if t:
                titles.append(t)
        # Atom: entries with <title>
        atom_ns = "http://www.w3.org/2005/Atom"
        for entry in root.findall(f".//{{{atom_ns}}}entry"):
            t_el = entry.find(f"{{{atom_ns}}}title")
            if t_el is not None and t_el.text:
                titles.append(t_el.text.strip())

        return titles[:10]
    except Exception as e:
        print(f"  [{name}] fetch error: {e}")
        return []


def _is_civic(title: str) -> bool:
    lower = title.lower()
    return any(kw in lower for kw in CIVIC_KEYWORDS)


def _clean(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"\s*[|\-–]\s*(Reuters|AP|NPR|BBC|CBS|PBS|Guardian|NYT)\b.*$", "", title, flags=re.IGNORECASE)
    if len(title) > MAX_TITLE_LEN:
        title = title[:MAX_TITLE_LEN - 1] + "…"
    return title


def fetch_headlines() -> tuple[list[str], int]:
    """Return (ticker_item_list, sources_hit_count)."""
    all_titles: list[tuple[str, str]] = []
    sources_hit = 0

    for name, url in FEEDS:
        titles = _fetch_feed(name, url)
        if titles:
            sources_hit += 1
        for t in titles:
            all_titles.append((name, t))

    # Civic-relevant headlines first, then general
    civic   = [(n, t) for n, t in all_titles if _is_civic(t)]
    general = [(n, t) for n, t in all_titles if not _is_civic(t)]
    selected = (civic + general)[:MAX_HEADLINES]

    items = []
    seen: set[str] = set()
    for name, title in selected:
        clean = _clean(title)
        key = clean.lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        items.append(f"{clean}  [{name}]")

    ts = datetime.now(timezone.utc).strftime("%-I:%M %p UTC")
    items.append(f"Headlines updated at {ts} — civicresilience.net")

    return items, sources_hit


if __name__ == "__main__":
    items, sources = fetch_headlines()
    print(f"\n{sources} sources | {len(items)} headlines:\n")
    for i, h in enumerate(items, 1):
        print(f"  {i:2d}. {h}")
