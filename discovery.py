"""
discovery.py — find extra items BEYOND the fixed feeds, using FREE search APIs.

Instead of paying Gemini to browse (grounding), we do the searching ourselves
with keyless, free endpoints, then let the normal (free-tier) ranker pick the
best. This gives the "self-discovery" effect with real links and zero cost.

Sources (both free, no API key):
  - Semantic Scholar paper search  -> papers matching your topics
  - Hacker News Algolia search      -> recent stories matching your topics

Search topics come from `discovery_queries` in profile.yaml. Edit that list
when your interests shift. The pass fails soft: any error yields fewer/zero
items, never a crash.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

import requests

_HTTP_TIMEOUT = 20
_S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
_HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"


def _stable_id(prefix: str, key: str) -> str:
    return f"{prefix}-{hashlib.md5(key.encode('utf-8')).hexdigest()[:16]}"


# ------------------------------------------------------- Semantic Scholar
def _search_papers(query: str, limit: int, since: datetime) -> list[dict]:
    try:
        r = requests.get(
            _S2_SEARCH,
            params={
                "query": query,
                "limit": limit,
                "fields": "title,abstract,url,year,citationCount,"
                          "publicationDate,externalIds,authors",
                "sort": "publicationDate:desc",
            },
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 429:
            time.sleep(5)  # S2 free tier is strict; back off once and retry
            r = requests.get(
                _S2_SEARCH,
                params={
                    "query": query, "limit": limit,
                    "fields": "title,abstract,url,year,citationCount,"
                              "publicationDate,externalIds,authors",
                    "sort": "publicationDate:desc",
                },
                timeout=_HTTP_TIMEOUT,
            )
        if r.status_code != 200:
            print(f"[discovery/s2] '{query}' -> HTTP {r.status_code}, skipping")
            return []
        data = r.json().get("data", []) or []
    except Exception as e:
        print(f"[discovery/s2] '{query}' failed: {e}")
        return []

    out = []
    for p in data:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        # recency filter: keep if published within window, else drop
        pub = p.get("publicationDate")
        if pub:
            try:
                if datetime.fromisoformat(pub).replace(
                        tzinfo=timezone.utc) < since:
                    continue
            except ValueError:
                pass

        ext = p.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv")
        url = (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id
               else p.get("url") or "")
        if not url:
            continue
        authors = [a.get("name", "") for a in (p.get("authors") or [])][:6]
        out.append({
            "id": _stable_id("disc", url),
            "pool": "papers",
            "source": "🔎 Semantic Scholar",
            "title": title,
            "authors": authors,
            "abstract": (p.get("abstract") or "").strip()[:1200],
            "url": url,
            "published": (pub or datetime.now(timezone.utc).isoformat()),
            "citations": p.get("citationCount"),
        })
    return out


# ----------------------------------------------------------- HN Algolia
def _search_news(query: str, limit: int, min_points: int,
                 since: datetime) -> list[dict]:
    try:
        r = requests.get(
            _HN_SEARCH,
            params={
                "query": query,
                "tags": "story",
                "numericFilters": f"points>={min_points},"
                                  f"created_at_i>{int(since.timestamp())}",
                "hitsPerPage": limit,
            },
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[discovery/hn] '{query}' -> HTTP {r.status_code}, skipping")
            return []
        hits = r.json().get("hits", []) or []
    except Exception as e:
        print(f"[discovery/hn] '{query}' failed: {e}")
        return []

    out = []
    for h in hits:
        title = (h.get("title") or "").strip()
        oid = h.get("objectID")
        if not title or not oid:
            continue
        url = h.get("url") or f"https://news.ycombinator.com/item?id={oid}"
        out.append({
            "id": _stable_id("disc", url),
            "pool": "news",
            "source": "🔎 HN Search",
            "title": title,
            "authors": [],
            "abstract": (f"HN story, {h.get('points', 0)} points, "
                         f"{h.get('num_comments', 0)} comments. {url}"),
            "url": url,
            "published": h.get("created_at", ""),
            "citations": None,
        })
    return out


# --------------------------------------------------------------- driver
def discover(queries: list[str], min_points: int, per_query: int,
             lookback_days: int = 14) -> list[dict]:
    """Run every discovery query against the free search APIs.

    Returns items in the standard fetcher schema, tagged 🔎 and deduped by url.
    """
    if not queries:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    seen_ids: set[str] = set()
    out: list[dict] = []
    for q in queries:
        for item in _search_papers(q, per_query, since):
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                out.append(item)
        time.sleep(3)  # be polite to Semantic Scholar's free tier
        for item in _search_news(q, per_query, min_points, since):
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                out.append(item)

    papers = sum(1 for i in out if i["pool"] == "papers")
    news = sum(1 for i in out if i["pool"] == "news")
    print(f"[discovery] found {papers} papers + {news} news via free search")
    return out
