"""
substack.py — Auto-publish civic editorial posts to Substack.

Reads session cookie from /etc/substack-session.
Generates editorial content via Ollama, then publishes to Substack API.
Runs twice daily at PUBLISH_HOURS.
"""

from __future__ import annotations
import html as _html_module
import json
import os
import re
import ssl
import time
import threading
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

PUBLICATION    = "civicresiliencenetwork"
SUBSTACK_BASE  = f"https://{PUBLICATION}.substack.com"
OLLAMA_URL     = "http://192.168.12.200:11434"
GENERATE_MODEL = "mistral:latest"   # better long-form than gemma3:4b
FALLBACK_MODEL = "llama3.2:latest"
SESSION_FILE   = "/etc/substack-session"
AUTHOR_ID      = 17321092           # James D Flanagan
PUBLISH_HOURS  = [7, 19]            # 7 AM and 7 PM local time

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


# ── Ollama call ───────────────────────────────────────────────────────────────
def _ollama(prompt: str, model: str = GENERATE_MODEL, timeout: int = 180) -> str:
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return json.loads(r.read())["response"].strip()


def _extract_json(raw: str) -> dict:
    """Robustly extract a JSON object from model output."""
    cleaned = raw
    if "```" in cleaned:
        for part in cleaned.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                cleaned = part
                break
    # Find outermost { } in case model added preamble
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start >= 0 and end > start:
        cleaned = cleaned[start:end]
    return json.loads(cleaned)


# ── Content generation ────────────────────────────────────────────────────────
def generate_digest(stories: list[dict]) -> str:
    """Build an HTML digest of the top 6 headlines with one-line summaries."""
    top = stories[:6]
    items = ""
    for i, s in enumerate(top, 1):
        title = _html_module.escape(s.get("title", ""))
        summary = s.get("summary", "")
        # Trim summary to one clean sentence
        first_sentence = re.split(r'(?<=[.!?])\s+', summary)[0] if summary else ""
        first_sentence = _html_module.escape(first_sentence[:160])
        sources = " · ".join(s.get("sources", [])[:3])
        civic = s.get("civic_score", 0)
        civic_tag = f" <em>(Civic score: {civic})</em>" if civic >= 3 else ""
        items += f"<li><strong>{title}</strong>{civic_tag}<br>{first_sentence} <em>— {_html_module.escape(sources)}</em></li>\n"
    return f"<h2>Today's Top Headlines</h2>\n<ol>\n{items}</ol>\n"


def generate_editorial(story: dict, digest_html: str = "") -> dict:
    """Use Ollama to write a deep civic editorial post from a story dict."""
    title    = story.get("title", "")
    summary  = story.get("summary", "")
    sources  = ", ".join(story.get("sources", [])[:5])
    civic    = story.get("civic_score", 0)
    coverage = story.get("coverage_count", 1)
    chamber  = "multi-outlet" if coverage > 1 else "single outlet"

    prompt = f"""You are a senior civic journalist and editor for the Civic Resilience Network, a nonpartisan newsletter focused on democracy, community resilience, and civic participation.

Write a substantive, well-researched newsletter article about the following story. This is NOT a news brief — it is a full editorial piece with depth, context, and civic analysis.

STORY:
Title: {title}
Summary: {summary}
Coverage: {sources} ({coverage} outlets — {chamber})
Civic relevance score: {civic}/10

ARTICLE STRUCTURE (follow this exactly):
1. **Opening hook** (1 paragraph) — A compelling, specific opening that frames why this story matters to everyday citizens and communities. Do NOT start with "In a..." or restate the headline. Draw the reader in.

2. **What Is Happening** (2 paragraphs) — Explain the story clearly and factually. Who are the key actors? What decisions were made, what events occurred, what is at stake? Include specific details, figures, or quotes where relevant. Go beyond the headline.

3. **Background & Context** (1-2 paragraphs) — Why is this happening now? What led to this moment? Provide historical or policy context that helps readers understand the deeper forces at play. Connect it to broader trends in American civic life, democracy, or community resilience.

4. **Civic & Community Impact** (2 paragraphs) — What does this mean for local governments, civic institutions, community organizations, and everyday citizens? Focus especially on implications for Iowa and Midwestern communities where relevant. Be specific about who is affected and how.

5. **What You Can Do** (bulleted list of 3-4 items) — Concrete, actionable steps readers can take: contacting representatives, attending meetings, supporting organizations, registering to vote, volunteering, etc. Be specific, not generic.

6. **Editor's Note** (1 short paragraph) — A brief closing reflection on the broader civic significance. Nonpartisan but not toothless — civic journalism should call readers to engagement.

REQUIREMENTS:
- 750-950 words total
- Factual, nonpartisan, solution-oriented tone
- Write as if speaking to an engaged, informed citizen — not an expert
- Use specific names, places, numbers where possible
- Do NOT simply restate or paraphrase the headline — this must be original analysis

FORMAT: Respond with ONLY a JSON object (no markdown fences) with these keys:
- "post_title": original, compelling headline (max 85 chars) — NOT the source headline
- "subtitle": one punchy sentence that makes someone want to read (max 130 chars)
- "body_html": the full article as HTML using <p>, <h2>, <ul>, <li> tags
- "tags": list of 4-6 relevant topic strings

JSON only. No other text."""

    errors = []
    for model in [GENERATE_MODEL, FALLBACK_MODEL]:
        try:
            raw = _ollama(prompt, model=model, timeout=180)
            result = _extract_json(raw)
            # Validate required fields
            if all(k in result for k in ("post_title", "subtitle", "body_html")):
                # Inject digest section before the "What You Can Do" section if present
                if digest_html:
                    result["body_html"] = result["body_html"] + "\n<hr>\n" + digest_html
                return result
        except Exception as e:
            errors.append(f"{model}: {e}")
            continue

    # Fallback
    return {
        "post_title": title[:85],
        "subtitle": summary[:130] if summary else "Civic Resilience Network Update",
        "body_html": f"<p>{summary}</p>\n<hr>\n{digest_html}\n<p><em>Sources: {sources}</em></p>",
        "tags": ["civic", "news", "community", "democracy"],
        "_fallback": True,
        "_errors": errors,
    }


# ── HTML → ProseMirror converter ──────────────────────────────────────────────
class _HtmlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes: list[dict] = []
        self._cur_tag: str = ""
        self._cur_text: list[str] = []
        self._list_items: list[dict] = []
        self._in_list: str = ""

    def handle_starttag(self, tag, attrs):
        self._cur_tag = tag
        if tag in ("ul", "ol"):
            self._in_list = tag
            self._list_items = []
        elif tag == "li":
            self._cur_text = []
        elif tag == "hr":
            self.nodes.append({"type": "horizontal_rule"})
        elif tag in ("p", "h2", "h3"):
            self._cur_text = []

    def handle_endtag(self, tag):
        text = " ".join(self._cur_text).strip()
        if tag in ("p",) and text:
            self.nodes.append({"type": "paragraph", "content": [{"type": "text", "text": text}]})
        elif tag in ("h2", "h3") and text:
            level = 2 if tag == "h2" else 3
            self.nodes.append({"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": text}]})
        elif tag == "li" and text:
            self._list_items.append({"type": "list_item", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]})
        elif tag in ("ul", "ol") and self._list_items:
            list_type = "bullet_list" if tag == "ul" else "ordered_list"
            self.nodes.append({"type": list_type, "content": self._list_items})
            self._list_items = []
            self._in_list = ""
        self._cur_text = []
        self._cur_tag = ""

    def handle_data(self, data):
        stripped = data.strip()
        if stripped:
            self._cur_text.append(stripped)


def _html_to_prosemirror(html: str) -> str:
    parser = _HtmlParser()
    parser.feed(html)
    nodes = parser.nodes or [{"type": "paragraph", "content": [{"type": "text", "text": re.sub(r'<[^>]+>', ' ', html).strip()}]}]
    # Footer
    nodes.append({"type": "horizontal_rule"})
    nodes.append({"type": "paragraph", "content": [{"type": "text", "text": "Published by Civic Resilience Network · civicresilience.net"}]})
    return json.dumps({"type": "doc", "content": nodes})


# ── Substack API operations ───────────────────────────────────────────────────
def create_draft(post_title: str, subtitle: str, body_html: str) -> dict:
    payload = {
        "draft_title": post_title,
        "draft_subtitle": subtitle,
        "draft_body": _html_to_prosemirror(body_html),
        "draft_bylines": [{"id": AUTHOR_ID, "is_guest": False}],
        "audience": "everyone",
        "type": "newsletter",
    }
    return _substack_request("POST", "/api/v1/drafts", payload)


def publish_draft(draft_id: int) -> dict:
    return _substack_request("POST", f"/api/v1/drafts/{draft_id}/publish", {
        "send": True,
        "share_automatically": True,
        "is_v2": True,
    })


def publish_story(story: dict, all_stories: list[dict] | None = None) -> dict:
    """Full pipeline: generate digest + editorial → create draft → publish."""
    started = datetime.now(timezone.utc).isoformat()
    try:
        digest_html = generate_digest(all_stories or [story]) if all_stories else ""
        editorial = generate_editorial(story, digest_html)
        draft = create_draft(editorial["post_title"], editorial["subtitle"], editorial["body_html"])
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
            "model": GENERATE_MODEL,
            "was_fallback": editorial.get("_fallback", False),
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


# ── 12-hour scheduler ─────────────────────────────────────────────────────────
def _scheduler():
    """Publish at each hour in PUBLISH_HOURS, once per slot per day."""
    import news as _news
    last_slot: str = ""
    while True:
        now = datetime.now()
        if now.hour in PUBLISH_HOURS:
            slot = f"{now.date()}-{now.hour}"
            if slot != last_slot:
                last_slot = slot
                try:
                    feed = _news.get_feed(limit=30)
                    if feed:
                        best = max(feed, key=lambda s: (
                            s.get("uplifted", 0),
                            s.get("civic_score", 0),
                            s.get("coverage_count", 1),
                        ))
                        publish_story(best, all_stories=feed[:6])
                except Exception:
                    pass
        time.sleep(60)

threading.Thread(target=_scheduler, daemon=True).start()
