"""
hearings.py — Congressional hearing schedule and stream state.

Fetches hearing schedules from Congress.gov API.
Free DEMO_KEY allows 30 req/hour. Set CONGRESS_API_KEY env var for higher limits.
"""

from __future__ import annotations
import os
import ssl
import time
import urllib.request
import urllib.parse
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

API_KEY = os.environ.get("CONGRESS_API_KEY", "DEMO_KEY")
CONGRESS_BASE = "https://api.congress.gov/v3"
CURRENT_CONGRESS = 119  # 119th Congress: Jan 2025 – Jan 2027

# ── In-memory stream state ────────────────────────────────────────────────────
_stream: dict = {
    "url": "",
    "title": "",
    "committee": "",
    "embed_url": "",
    "version": 0,
    "set_at": None,
}

# ── Committee YouTube channels ────────────────────────────────────────────────
COMMITTEE_STREAMS: list[dict] = [
    # C-SPAN live feeds
    {"name": "C-SPAN (House Floor / Hearings)",   "type": "cspan",   "youtube_channel": "UCb-0HNqDzXc7_tKXzRXHSRw", "url": "https://www.youtube.com/@cspan/streams"},
    {"name": "C-SPAN 2 (Senate Floor / Hearings)","type": "cspan",   "youtube_channel": "UCb-0HNqDzXc7_tKXzRXHSRw", "url": "https://www.youtube.com/@cspan2/streams"},
    {"name": "C-SPAN 3 (History / More Hearings)","type": "cspan",   "youtube_channel": "UCb-0HNqDzXc7_tKXzRXHSRw", "url": "https://www.youtube.com/@cspan3/streams"},
    # House committees
    {"name": "House Judiciary Committee",          "type": "house",   "url": "https://www.youtube.com/@HouseJudiciary/streams"},
    {"name": "House Oversight Committee",          "type": "house",   "url": "https://www.youtube.com/@houseoversight/streams"},
    {"name": "House Intelligence Committee",       "type": "house",   "url": "https://www.youtube.com/@HouseIntelligence/streams"},
    {"name": "House Armed Services Committee",     "type": "house",   "url": "https://www.youtube.com/@HouseArmedServices/streams"},
    {"name": "House Energy & Commerce Committee",  "type": "house",   "url": "https://www.youtube.com/@HouseEandC/streams"},
    {"name": "House Ways & Means Committee",       "type": "house",   "url": "https://www.youtube.com/@WaysandMeansGOP/streams"},
    {"name": "House Foreign Affairs Committee",    "type": "house",   "url": "https://www.youtube.com/@HouseForeignAffairs/streams"},
    {"name": "House Financial Services Committee", "type": "house",   "url": "https://www.youtube.com/@HouseFinancialSvcs/streams"},
    {"name": "House Agriculture Committee",        "type": "house",   "url": "https://www.youtube.com/@HouseAgCommittee/streams"},
    {"name": "House Budget Committee",             "type": "house",   "url": "https://www.youtube.com/@HouseBudgetGOP/streams"},
    {"name": "House Science Committee",            "type": "house",   "url": "https://www.youtube.com/@ScienceSpaceandTech/streams"},
    # Senate committees
    {"name": "Senate Judiciary Committee",         "type": "senate",  "url": "https://www.youtube.com/@SenateJudiciaryDems/streams"},
    {"name": "Senate Armed Services Committee",    "type": "senate",  "url": "https://www.youtube.com/@ArmedServicesCommittee/streams"},
    {"name": "Senate Foreign Relations Committee", "type": "senate",  "url": "https://www.youtube.com/@SenateForeignRelations/streams"},
    {"name": "Senate Finance Committee",           "type": "senate",  "url": "https://www.youtube.com/@SenateFinance/streams"},
    {"name": "Senate Health (HELP) Committee",     "type": "senate",  "url": "https://www.youtube.com/@SenateHELP/streams"},
    {"name": "Senate Commerce Committee",          "type": "senate",  "url": "https://www.youtube.com/@SenateCommerce/streams"},
    {"name": "Senate Banking Committee",           "type": "senate",  "url": "https://www.youtube.com/@SenateBanking/streams"},
    {"name": "Senate Intelligence Committee",      "type": "senate",  "url": "https://www.intelligence.senate.gov/hearings"},
    {"name": "Senate Homeland Security Committee", "type": "senate",  "url": "https://www.youtube.com/@HsgGovAffairs/streams"},
    {"name": "Senate Agriculture Committee",       "type": "senate",  "url": "https://www.agriculture.senate.gov/hearings"},
    {"name": "Senate Environment & Public Works",  "type": "senate",  "url": "https://www.youtube.com/@EPWCommittee/streams"},
    {"name": "Senate Veterans Affairs Committee",  "type": "senate",  "url": "https://www.youtube.com/@SVACHearings/streams"},
]


def _fetch(path: str, params: dict | None = None) -> dict | None:
    p = {"api_key": API_KEY, "format": "json"}
    if params:
        p.update(params)
    url = f"{CONGRESS_BASE}{path}?{urllib.parse.urlencode(p)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CivicResilience/1.0"})
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except Exception:
        return None


def get_schedule(days_ahead: int = 7) -> list[dict]:
    """Fetch upcoming hearings from Congress.gov for the next N days."""
    now = datetime.now(timezone.utc)
    from_dt = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_dt = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = _fetch(f"/hearing/{CURRENT_CONGRESS}", {
        "limit": 50,
        "fromDateTime": from_dt,
        "toDateTime": to_dt,
        "sort": "date asc",
    })

    if not data or "hearings" not in data:
        return []

    results = []
    for h in data.get("hearings", []):
        results.append({
            "title": h.get("title", "Untitled Hearing"),
            "committee": h.get("committees", [{}])[0].get("name", "") if h.get("committees") else "",
            "chamber": h.get("chamber", ""),
            "date": h.get("date", ""),
            "url": h.get("url", ""),
            "jacket_number": h.get("jacketNumber", ""),
        })
    return results


def get_stream() -> dict:
    return dict(_stream)


def set_stream(url: str, title: str = "", committee: str = "") -> dict:
    embed = _make_embed_url(url)
    _stream.update(
        url=url,
        title=title,
        committee=committee,
        embed_url=embed,
        version=_stream["version"] + 1,
        set_at=datetime.now(timezone.utc).isoformat(),
    )
    return dict(_stream)


def clear_stream() -> dict:
    _stream.update(url="", title="", committee="", embed_url="", version=_stream["version"] + 1, set_at=None)
    return dict(_stream)


def _make_embed_url(url: str) -> str:
    """Convert a YouTube watch/live URL to an embeddable URL."""
    if not url:
        return ""
    # youtube.com/watch?v=ID
    if "youtube.com/watch" in url or "youtu.be/" in url:
        vid = ""
        if "v=" in url:
            vid = url.split("v=")[1].split("&")[0]
        elif "youtu.be/" in url:
            vid = url.split("youtu.be/")[1].split("?")[0]
        if vid:
            return f"https://www.youtube.com/embed/{vid}?autoplay=1&rel=0"
    # youtube.com/live/ID
    if "youtube.com/live/" in url:
        vid = url.split("youtube.com/live/")[1].split("?")[0]
        return f"https://www.youtube.com/embed/{vid}?autoplay=1&rel=0"
    # C-SPAN — use their direct stream pages
    if "c-span.org" in url:
        return url
    # Already an embed URL or other source — use as-is
    return url


def committee_list() -> list[dict]:
    return COMMITTEE_STREAMS
