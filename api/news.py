"""
news.py — Multi-source story store with clustering, civic scoring, and uplift.

Imported by main.py. Auto-refreshes every REFRESH_INTERVAL seconds in background.
Call refresh() manually to force a fetch.
"""

from __future__ import annotations
import hashlib
import re
import ssl
import time
import threading
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

REFRESH_INTERVAL = 1800  # 30 min

# ── Source registry ───────────────────────────────────────────────────────────
# type: national | government | local | nonprofit
SOURCES: list[dict] = [
    # National media
    {"name": "NPR",             "url": "https://feeds.npr.org/1001/rss.xml",                                "type": "national"},
    {"name": "BBC World",       "url": "https://feeds.bbci.co.uk/news/world/rss.xml",                       "type": "national"},
    {"name": "CBS News",        "url": "https://www.cbsnews.com/latest/rss/main",                           "type": "national"},
    {"name": "PBS NewsHour",    "url": "https://www.pbs.org/newshour/feeds/rss/headlines",                  "type": "national"},
    {"name": "The Guardian",    "url": "https://www.theguardian.com/us/rss",                                "type": "national"},
    {"name": "NY Times",        "url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",         "type": "national"},
    {"name": "AP News",         "url": "https://feeds.apnews.com/rss/apf-topnews",                         "type": "national"},
    # Government / civic bodies
    {"name": "FEMA",            "url": "https://www.fema.gov/feeds/fema_news.xml",                          "type": "government"},
    {"name": "CDC",             "url": "https://tools.cdc.gov/api/v2/resources/media/316422.rss",            "type": "government"},
    {"name": "White House",     "url": "https://www.whitehouse.gov/feed/",                                  "type": "government"},
    {"name": "Iowa Governor",   "url": "https://governor.iowa.gov/newsroom/feed/",                          "type": "government"},
    # Local / Iowa
    {"name": "Iowa Public Radio","url": "https://www.iowapublicradio.org/rss.xml",                         "type": "local"},
    {"name": "Radio Iowa",      "url": "https://www.radioiowa.com/feed/",                                   "type": "local"},
    {"name": "KCCI",            "url": "https://www.kcci.com/rss",                                         "type": "local"},
    {"name": "WHO-TV",          "url": "https://who13.com/feed/",                                          "type": "local"},
]

CIVIC_KEYWORDS = {
    "community", "council", "city", "county", "municipal", "mayor", "vote",
    "election", "public", "emergency", "disaster", "resilience", "infrastructure",
    "housing", "water", "climate", "flood", "storm", "evacuation", "shelter",
    "food", "health", "safety", "police", "fire", "school", "education",
    "iowa", "local", "neighborhood", "volunteer", "nonprofit", "grant",
    "budget", "policy", "law", "senate", "congress", "legislation", "alert",
    "relief", "response", "preparedness", "recovery", "resource", "aid",
    "utility", "transit", "zoning", "permit", "ordinance", "referendum",
}

_STOP = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "by","from","as","is","was","are","were","be","been","being","have","has",
    "had","do","does","did","will","would","could","should","may","might",
    "this","that","these","those","it","its","their","they","he","she","we",
    "you","after","over","over","new","says","said","amid","what","how",
}

SOURCE_TYPE_ORDER = ["local", "government", "national", "nonprofit"]


# ── Story dataclass ───────────────────────────────────────────────────────────
@dataclass
class Story:
    id: str
    title: str
    summary: str = ""
    sources: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    civic_score: int = 0
    coverage_count: int = 1
    uplifted: bool = False
    uplift_note: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        unique_types = list(dict.fromkeys(self.source_types))
        type_counts: dict[str, int] = {}
        for t in self.source_types:
            type_counts[t] = type_counts.get(t, 0) + 1
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "sources": self.sources,
            "source_types": unique_types,
            "type_counts": type_counts,
            "urls": self.urls,
            "civic_score": self.civic_score,
            "coverage_count": self.coverage_count,
            "uplifted": self.uplifted,
            "uplift_note": self.uplift_note,
            "age_minutes": round((time.time() - self.timestamp) / 60),
            "is_blindspot": len(unique_types) == 1,
        }


# ── Global store ─────────────────────────────────────────────────────────────
_stories: list[Story] = []
_last_refresh: float = 0.0
_lock = threading.Lock()
_uplifted: dict[str, str] = {}  # story_id → note


# ── Helpers ───────────────────────────────────────────────────────────────────
def _norm(title: str) -> set[str]:
    words = re.findall(r"[a-z]+", title.lower())
    return {w for w in words if w not in _STOP and len(w) > 2}


def _story_id(title: str) -> str:
    return hashlib.md5(title.lower().strip().encode()).hexdigest()[:12]


def _civic(title: str) -> int:
    lower = title.lower()
    return sum(1 for kw in CIVIC_KEYWORDS if kw in lower)


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(
        r"\s*[|\-–]\s*(Reuters|AP|NPR|BBC|CBS|PBS|Guardian|NYT|CNN|Fox|MSNBC)\b.*$",
        "", title, flags=re.IGNORECASE,
    )
    return title[:140]


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    return re.sub(r"\s+", " ", text).strip()


def _trim_summary(text: str, max_chars: int = 220) -> str:
    text = _strip_html(text)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rfind(" ")
    return text[: cut if cut > 0 else max_chars] + "…"


def _fetch_feed(src: dict) -> list[tuple[str, str, str, str, str]]:
    """Returns list of (title, source_name, source_type, url, summary)."""
    try:
        req = urllib.request.Request(
            src["url"], headers={"User-Agent": "CivicResilience-NewsBot/2.0"}
        )
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            content = resp.read()
        root = ET.fromstring(content)
        results = []
        for item in root.findall(".//item")[:15]:
            t = _clean_title((item.findtext("title") or "").strip())
            u = (item.findtext("link") or "").strip()
            desc = _trim_summary(item.findtext("description") or "")
            if t:
                results.append((t, src["name"], src["type"], u, desc))
        atom_ns = "http://www.w3.org/2005/Atom"
        for entry in root.findall(f".//{{{atom_ns}}}entry")[:15]:
            t_el = entry.find(f"{{{atom_ns}}}title")
            l_el = entry.find(f"{{{atom_ns}}}link")
            s_el = entry.find(f"{{{atom_ns}}}summary") or entry.find(f"{{{atom_ns}}}content")
            if t_el is not None and t_el.text:
                t = _clean_title(t_el.text.strip())
                u = (l_el.get("href", "") if l_el is not None else "")
                desc = _trim_summary(s_el.text or "" if s_el is not None else "")
                results.append((t, src["name"], src["type"], u, desc))
        return results
    except Exception:
        return []


def _cluster(raw: list[tuple[str, str, str, str, str]]) -> list[Story]:
    used = [False] * len(raw)
    clusters: list[Story] = []
    for i, (title_i, src_i, type_i, url_i, desc_i) in enumerate(raw):
        if used[i]:
            continue
        words_i = _norm(title_i)
        s = Story(
            id=_story_id(title_i),
            title=title_i,
            summary=desc_i,
            sources=[src_i],
            source_types=[type_i],
            urls=[url_i] if url_i else [],
            civic_score=_civic(title_i),
        )
        used[i] = True
        for j, (title_j, src_j, type_j, url_j, desc_j) in enumerate(raw):
            if used[j] or j == i:
                continue
            words_j = _norm(title_j)
            if not words_i or not words_j:
                continue
            overlap = len(words_i & words_j) / min(len(words_i), len(words_j))
            if overlap >= 0.55:
                if src_j not in s.sources:
                    s.sources.append(src_j)
                    s.source_types.append(type_j)
                if url_j and url_j not in s.urls:
                    s.urls.append(url_j)
                # Use longest summary available
                if not s.summary and desc_j:
                    s.summary = desc_j
                used[j] = True
        s.coverage_count = len(s.sources)
        clusters.append(s)
    return clusters


def _sort_key(s: Story) -> tuple:
    return (s.uplifted, s.civic_score * (1 + s.coverage_count), s.coverage_count)


# ── Public API ────────────────────────────────────────────────────────────────
def refresh() -> int:
    raw: list[tuple[str, str, str, str, str]] = []
    for src in SOURCES:
        raw.extend(_fetch_feed(src))

    clusters = _cluster(raw)

    with _lock:
        for s in clusters:
            if s.id in _uplifted:
                s.uplifted = True
                s.uplift_note = _uplifted[s.id]
        clusters.sort(key=_sort_key, reverse=True)
        global _stories, _last_refresh
        _stories = clusters
        _last_refresh = time.time()

    return len(clusters)


def get_feed(limit: int = 60) -> list[dict]:
    with _lock:
        return [s.to_dict() for s in _stories[:limit]]


def get_blindspot(limit: int = 25) -> list[dict]:
    with _lock:
        return [s.to_dict() for s in _stories if len(set(s.source_types)) == 1][:limit]


def get_uplifted() -> list[dict]:
    with _lock:
        return [s.to_dict() for s in _stories if s.uplifted]


def uplift(story_id: str, note: str = "") -> bool:
    with _lock:
        _uplifted[story_id] = note
        for s in _stories:
            if s.id == story_id:
                s.uplifted = True
                s.uplift_note = note
        _stories.sort(key=_sort_key, reverse=True)
        return story_id in {s.id for s in _stories}


def remove_uplift(story_id: str) -> None:
    with _lock:
        _uplifted.pop(story_id, None)
        for s in _stories:
            if s.id == story_id:
                s.uplifted = False
                s.uplift_note = ""
        _stories.sort(key=_sort_key, reverse=True)


def ticker_items(limit: int = 15) -> list[str]:
    with _lock:
        items = []
        for s in _stories[:limit]:
            if s.uplifted:
                tag = "UPLIFTED"
            elif s.is_blindspot if hasattr(s, "is_blindspot") else len(set(s.source_types)) == 1:
                tag = "BLINDSPOT"
            elif s.coverage_count >= 3:
                tag = f"{s.coverage_count} SOURCES"
            else:
                tag = s.source_types[0].upper() if s.source_types else "NEWS"
            src_label = s.sources[0] if len(s.sources) == 1 else f"{len(s.sources)} outlets"
            items.append(f"[{tag}] {s.title}  — {src_label}")
        return items or ["Civic Resilience Network — Live"]


def last_refresh_iso() -> Optional[str]:
    if not _last_refresh:
        return None
    return datetime.fromtimestamp(_last_refresh, tz=timezone.utc).isoformat()


def stats() -> dict:
    with _lock:
        total = len(_stories)
        uplifted_count = sum(1 for s in _stories if s.uplifted)
        blindspot_count = sum(1 for s in _stories if len(set(s.source_types)) == 1)
        multi_count = sum(1 for s in _stories if s.coverage_count > 1)
        type_counts: dict[str, int] = {}
        for s in _stories:
            for t in set(s.source_types):
                type_counts[t] = type_counts.get(t, 0) + 1
    return {
        "total": total,
        "uplifted": uplifted_count,
        "blindspot": blindspot_count,
        "multi_source": multi_count,
        "by_type": type_counts,
        "last_refresh": last_refresh_iso(),
        "sources_configured": len(SOURCES),
    }


# ── Background auto-refresh ───────────────────────────────────────────────────
def _auto():
    while True:
        time.sleep(REFRESH_INTERVAL)
        try:
            refresh()
        except Exception:
            pass

threading.Thread(target=_auto, daemon=True).start()
