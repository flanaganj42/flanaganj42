"""
substack.py — Auto-publish civic editorial posts to Substack.

Reads session cookie from /etc/substack-session.
Generates editorial content via Ollama, then publishes to Substack API.
"""

from __future__ import annotations
import json
import os
import ssl
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

PUBLICATION    = "civicresiliencenetwork"
SUBSTACK_BASE  = f"https://{PUBLICATION}.substack.com"
OLLAMA_URL     = "http://192.168.12.200:11434"
GENERATE_MODEL = "gemma3:4b"
SESSION_FILE   = "/etc/substack-session"
AUTHOR_ID      = 17321092   # James D Flanagan — civicresiliencenetwork.substack.com
PUBLISH_HOUR   = 7          # 7 AM local time daily auto-publish

# ── Publish history (in-memory) ───────────────────────────────────────────────
_history: list[dict] = []
_lock = threading.Lock()


# ── Auth ─────────────────────────────────────────────────────────────────────
def _session_cookie() -> str:
    for path in [SESSION_FILE, "/tmp/substack-session-tmp"]:
        try:
            if os.path.exists(path):
                val = open(path).read().strip()
                if val:
                    return val
        except Exception:
            pass
    raise RuntimeError("Substack session cookie not found. Save it to /etc/substack-session.")


def _substack_request(method: str, path: str, body: dict | None = None) -> dict:
    cookie = _session_cookie()
    url = f"{SUBSTACK_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "Cookie": f"substack.sid={cookie}",
        "Content-Type": "application/json",
        "User-Agent": "CivicResilience/1.0",
        "Origin": SUBSTACK_BASE,
        "Referer": f"{SUBSTACK_BASE}/publish",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
        return json.loads(r.read())


# ── Content generation ────────────────────────────────────────────────────────
def generate_editorial(story: dict) -> dict:
    """Use Ollama to write a civic editorial post from a story dict."""
    title   = story.get("title", "")
    summary = story.get("summary", "")
    sources = ", ".join(story.get("sources", [])[:4])
    civic   = story.get("civic_score", 0)
    coverage = story.get("coverage_count", 1)

    prompt = f"""You are a civic journalist writing for the Civic Resilience Network newsletter on Substack.

Write a newsletter post about this trending news story. The post should:
- Open with a strong civic framing (why this matters to communities and democracy)
- Summarize what's happening in 1-2 clear paragraphs
- Add editorial perspective: what civic institutions, local leaders, or community members should know or do
- End with a "What You Can Do" section with 2-3 specific action items
- Be factual, nonpartisan, and solution-oriented
- Be 350-500 words total

Story title: {title}
Summary: {summary}
Sources covering this: {sources} ({coverage} outlets)
Civic relevance score: {civic}/10

Format your response as JSON with these exact keys:
- "post_title": compelling newsletter headline (max 80 chars)
- "subtitle": one sentence teaser (max 120 chars)
- "body_html": full post as HTML paragraphs using <p>, <h2>, <ul>, <li> tags
- "tags": list of 3-5 topic tags (strings)

Respond with only the JSON object, no other text."""

    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=json.dumps({"model": GENERATE_MODEL, "prompt": prompt, "stream": False}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=90, context=_SSL_CTX) as r:
            raw = json.loads(r.read())["response"].strip()
        # Extract JSON from markdown fences or raw output
        cleaned = raw
        if "```" in cleaned:
            for part in cleaned.split("```"):
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    cleaned = part
                    break
        return json.loads(cleaned.strip())
    except Exception as e:
        # Fallback: plain structure if generation fails
        return {
            "post_title": title[:80],
            "subtitle": summary[:120] if summary else "Civic Resilience Network Update",
            "body_html": f"<p>{summary}</p><p>Sources: {sources}</p>",
            "tags": ["civic", "news", "community"],
        }


# ── Substack API operations ───────────────────────────────────────────────────
def _html_to_prosemirror(html: str) -> str:
    """Convert simple HTML to Substack's ProseMirror JSON doc format."""
    import re
    nodes = []
    # Split on block-level tags
    chunks = re.split(r'<(p|h2|h3|ul|ol|hr)[^>]*>', html)
    raw_text = re.sub(r'<[^>]+>', ' ', html)
    # Build a simple paragraph-per-sentence structure for reliability
    paragraphs = [p.strip() for p in re.sub(r'<[^>]+>', ' ', html).split('\n') if p.strip()]
    for para in paragraphs:
        if para.startswith('##') or para.startswith('**'):
            # Treat as heading
            text = para.lstrip('#* ').strip()
            nodes.append({"type": "heading", "attrs": {"level": 2},
                          "content": [{"type": "text", "text": text}]})
        elif para.startswith('- ') or para.startswith('• '):
            nodes.append({"type": "paragraph",
                          "content": [{"type": "text", "text": para.lstrip('-• ').strip()}]})
        elif para:
            nodes.append({"type": "paragraph",
                          "content": [{"type": "text", "text": para}]})
    # Footer
    nodes.append({"type": "paragraph", "content": [{"type": "text", "text": ""}]})
    nodes.append({"type": "horizontal_rule"})
    nodes.append({"type": "paragraph", "content": [
        {"type": "text", "text": "Published by Civic Resilience Network · civicresilience.net"}
    ]})
    return json.dumps({"type": "doc", "content": nodes})


def create_draft(post_title: str, subtitle: str, body_html: str) -> dict:
    """Create a Substack draft and return the draft object."""
    body_doc = _html_to_prosemirror(body_html)
    payload = {
        "draft_title": post_title,
        "draft_subtitle": subtitle,
        "draft_body": body_doc,
        "draft_bylines": [{"id": AUTHOR_ID, "is_guest": False}],
        "audience": "everyone",
        "type": "newsletter",
    }
    return _substack_request("POST", "/api/v1/drafts", payload)


def publish_draft(draft_id: int) -> dict:
    """Publish an existing draft."""
    return _substack_request("POST", f"/api/v1/drafts/{draft_id}/publish", {
        "send": True,
        "share_automatically": True,
        "is_v2": True,
    })


def publish_story(story: dict) -> dict:
    """Full pipeline: generate editorial → create draft → publish. Returns result dict."""
    started = datetime.now(timezone.utc).isoformat()
    try:
        editorial = generate_editorial(story)
        draft = create_draft(
            editorial["post_title"],
            editorial["subtitle"],
            editorial["body_html"],
        )
        draft_id = draft.get("id")
        if not draft_id:
            raise RuntimeError(f"No draft ID returned: {draft}")
        result = publish_draft(draft_id)
        post_url = f"{SUBSTACK_BASE}/p/{result.get('slug', draft_id)}"
        record = {
            "status": "published",
            "story_title": story.get("title"),
            "post_title": editorial["post_title"],
            "post_url": post_url,
            "draft_id": draft_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        record = {
            "status": "error",
            "story_title": story.get("title", ""),
            "error": str(e),
            "published_at": started,
        }
    with _lock:
        _history.insert(0, record)
        if len(_history) > 50:
            _history.pop()
    return record


def get_history() -> list[dict]:
    with _lock:
        return list(_history)


# ── Daily auto-publish scheduler ──────────────────────────────────────────────
def _scheduler():
    """Background thread: publish daily at PUBLISH_HOUR local time."""
    import news as _news
    last_date = None
    while True:
        now = datetime.now()
        today = now.date()
        if now.hour == PUBLISH_HOUR and today != last_date:
            last_date = today
            try:
                feed = _news.get_feed(limit=30)
                if feed:
                    # Pick top civic story (highest score, prefer multi-source)
                    best = max(feed, key=lambda s: (s.get("uplifted", 0), s.get("civic_score", 0), s.get("coverage_count", 1)))
                    publish_story(best)
            except Exception:
                pass
        time.sleep(60)  # check every minute

threading.Thread(target=_scheduler, daemon=True).start()
