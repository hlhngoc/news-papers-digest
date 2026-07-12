"""
discovery.py — let Gemini (with Google Search grounding) find extra items
beyond the fixed arXiv/RSS feeds.

This is the "hybrid" layer: the curated feeds stay the reliable backbone
(real links, solid dedupe), and this pass surfaces a few things the feeds
might have missed. Discovered items are tagged and then compete in the normal
ranker alongside feed items, so they only appear if they're actually relevant.

Grounding costs a few billable search queries per run (not free-tier), and the
model occasionally returns an imperfect link — that's the trade-off for reach.
The pass fails soft: any error just yields zero discovered items, never a crash.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import ranker


def _stable_id(prefix: str, key: str) -> str:
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def discover(profile: str, model: str, count: int) -> list[dict]:
    """Ask Gemini to search the web for `count` recent, relevant items.

    Returns items in the same schema as fetchers, tagged pool="papers"|"news"
    and marked as discovered so the email can show a 🔎 label.
    """
    if count <= 0:
        return []

    prompt = f"""You are a research scout with live Google Search access.

THIS PERSON'S PROFILE:
{profile}

Search the web RIGHT NOW and find up to {count} genuinely recent items
(published within the last ~4 days) that this person would find valuable —
new papers, tools, industry news, or consulting/strategy pieces that match
their interests. Prefer things that are NOT just the top of arXiv or big-tech
PR — surface things a curated feed might miss.

For each item you MUST verify it exists via search and give its REAL url.
Classify each as either a research "paper" or general "news".

Return ONLY a JSON object, no prose, in exactly this shape:
{{
  "items": [
    {{
      "pool": "papers" or "news",
      "title": "...",
      "source": "publication or site name",
      "url": "https://real-url-you-found",
      "summary": "1-2 sentence factual summary",
      "published": "YYYY-MM-DD or empty if unknown"
    }}
  ]
}}
Only include items with a real, working URL you actually found via search.
If you cannot verify {count} items, return fewer — never invent links."""

    data = ranker.generate_grounded_json(model, prompt)
    if not isinstance(data, dict):
        return []

    now = datetime.now(timezone.utc).isoformat()
    out: list[dict] = []
    for d in data.get("items", []):
        url = (d.get("url") or "").strip()
        title = (d.get("title") or "").strip()
        if not url or not url.startswith("http") or not title:
            continue  # skip anything without a real link
        pool = "papers" if d.get("pool") == "papers" else "news"
        source = (d.get("source") or "Discovered").strip()
        out.append({
            "id": _stable_id("disc", url),
            "pool": pool,
            "source": f"🔎 {source}",
            "title": title,
            "authors": [],
            "abstract": (d.get("summary") or "").strip(),
            "url": url,
            "published": (d.get("published") or now),
            "citations": None,
        })

    print(f"[discovery] Gemini surfaced {len(out)} items via web search")
    return out
