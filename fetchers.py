"""
fetchers.py — pull raw candidates from arXiv, Semantic Scholar, HN, and RSS.

Items are tagged with a "pool":
    pool = "papers"  -> arXiv (optionally enriched via Semantic Scholar)
    pool = "news"    -> Hacker News, tech-news RSS, consulting RSS

Unified item schema:
    {
        "id":        str,   # stable unique id (used for dedupe)
        "pool":      str,   # "papers" | "news"
        "source":    str,   # "arXiv" | "Hacker News" | "<feed name>"
        "title":     str,
        "authors":   list[str],
        "abstract":  str,   # text the ranker reads
        "url":       str,
        "published": str,   # ISO 8601
        "citations": int | None,   # papers only, via Semantic Scholar
    }
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import arxiv
import feedparser
import requests

_HTTP_TIMEOUT = 20


def _stable_id(prefix: str, key: str) -> str:
    """Deterministic id across runs. Python's built-in hash() is salted
    per-process (PYTHONHASHSEED), so it MUST NOT be used for dedupe keys —
    it produced a new id every run and broke RSS dedupe. md5 is stable."""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


# --------------------------------------------------------------------- arXiv
def fetch_arxiv(categories: list[str], lookback_hours: int,
                per_category: int) -> list[dict]:
    """Query each arXiv category, keep papers submitted within the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)
    out: list[dict] = []

    for cat in categories:
        search = arxiv.Search(
            query=f"cat:{cat}",
            max_results=per_category,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        try:
            for r in client.results(search):
                published = r.published
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                if published < cutoff:
                    break  # newest-first, safe to stop early
                out.append({
                    "id": r.get_short_id().split("v")[0],   # versionless, e.g. 2401.01234
                    "pool": "papers",
                    "source": "arXiv",
                    "title": r.title.strip().replace("\n", " "),
                    "authors": [a.name for a in r.authors],
                    "abstract": r.summary.strip().replace("\n", " "),
                    "url": r.entry_id,
                    "published": published.isoformat(),
                    "citations": None,
                })
        except Exception as e:  # one bad category shouldn't kill the run
            print(f"[arxiv] {cat} failed: {e}")
        time.sleep(1)

    if not out:
        print("[arxiv] package returned 0 papers, trying HTTP fallback...")
        out = _fetch_arxiv_http(categories, cutoff, per_category)

    return out


_ARXIV_HTTP = "https://export.arxiv.org/api/query"


def _fetch_arxiv_http(categories: list[str], cutoff, per_category: int) -> list[dict]:
    """Fallback arXiv fetch via the raw Atom API + feedparser, used when the
    `arxiv` package returns nothing (it intermittently yields 0 on throttling)."""
    out: list[dict] = []
    for cat in categories:
        try:
            r = requests.get(
                _ARXIV_HTTP,
                params={
                    "search_query": f"cat:{cat}",
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                    "max_results": per_category,
                },
                timeout=_HTTP_TIMEOUT,
            )
            parsed = feedparser.parse(r.text)
            for e in parsed.entries:
                published = _entry_datetime(e)
                if published and published < cutoff:
                    continue
                raw_id = e.get("id", "")
                short = raw_id.split("/abs/")[-1].split("v")[0]
                if not short:
                    continue
                out.append({
                    "id": short,
                    "pool": "papers",
                    "source": "arXiv",
                    "title": e.get("title", "").strip().replace("\n", " "),
                    "authors": [a.get("name", "") for a in e.get("authors", [])],
                    "abstract": e.get("summary", "").strip().replace("\n", " "),
                    "url": e.get("link", raw_id),
                    "published": (published or datetime.now(timezone.utc)).isoformat(),
                    "citations": None,
                })
        except Exception as ex:
            print(f"[arxiv-http] {cat} failed: {ex}")
        time.sleep(3)
    return out


# ---------------------------------------------------- Semantic Scholar enrich
_S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"


def enrich_semantic_scholar(papers: list[dict]) -> None:
    """Attach citationCount to arXiv papers in-place (best effort).

    Uses the free batch endpoint with ARXIV:<id> keys. New papers will mostly
    have 0 citations — the value is catching the occasional fast riser, and
    the ranker prompt mentions citations when present.
    """
    if not papers:
        return
    ids = [f"ARXIV:{p['id'].split('v')[0]}" for p in papers]
    try:
        # batch endpoint accepts up to 500 ids per call
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            resp = requests.post(
                _S2_BATCH,
                params={"fields": "citationCount,influentialCitationCount"},
                json={"ids": chunk},
                timeout=_HTTP_TIMEOUT,
            )
            if resp.status_code != 200:
                print(f"[s2] batch call returned {resp.status_code}, skipping")
                return
            for paper, meta in zip(papers[start:start + 400], resp.json()):
                if isinstance(meta, dict):
                    paper["citations"] = meta.get("citationCount")
            time.sleep(1)
        n = sum(1 for p in papers if p.get("citations"))
        print(f"[s2] enriched: {n} papers have >0 citations")
    except Exception as e:
        print(f"[s2] enrichment skipped: {e}")


# ---------------------------------------------------------------- Hacker News
_HN_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
_HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"


def fetch_hackernews(keywords: list[str], min_score: int,
                     scan: int = 120) -> list[dict]:
    """Scan the HN front page; keep stories whose title matches a keyword."""
    kws = [k.lower() for k in keywords]
    out: list[dict] = []
    try:
        ids = requests.get(_HN_TOP, timeout=_HTTP_TIMEOUT).json()[:scan]
    except Exception as e:
        print(f"[hn] topstories failed: {e}")
        return out

    for hid in ids:
        try:
            item = requests.get(
                _HN_ITEM.format(hid), timeout=_HTTP_TIMEOUT
            ).json()
        except Exception:
            continue
        if not item or item.get("type") != "story":
            continue
        title = item.get("title", "")
        if item.get("score", 0) < min_score:
            continue
        # pad with spaces so " ai " matches whole words, not "brain"
        hay = f" {title.lower()} "
        if not any(k in hay for k in kws):
            continue
        url = item.get("url") or f"https://news.ycombinator.com/item?id={hid}"
        out.append({
            "id": f"hn-{hid}",
            "pool": "news",
            "source": "Hacker News",
            "title": title.strip(),
            "authors": [],
            "abstract": (
                f"Hacker News front-page story ({item.get('score', 0)} points, "
                f"{item.get('descendants', 0)} comments). Link: {url}"
            ),
            "url": url,
            "published": datetime.fromtimestamp(
                item.get("time", 0), tz=timezone.utc
            ).isoformat(),
            "citations": None,
        })

    return out


# ----------------------------------------------------------------------- RSS
def fetch_rss(feeds: list[str], lookback_hours: int = 168,
              per_feed: int = 8) -> list[dict]:
    """Parse RSS feeds (tech news + consulting) into the NEWS pool."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out: list[dict] = []

    for feed_url in feeds:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"[rss] {feed_url} failed: {e}")
            continue

        source = parsed.feed.get("title") or urlparse(feed_url).netloc
        for entry in parsed.entries[:per_feed]:
            published = _entry_datetime(entry)
            if published and published < cutoff:
                continue
            guid = entry.get("id") or entry.get("link") or entry.get("title", "")
            summary = entry.get("summary", "") or entry.get("description", "")
            out.append({
                "id": _stable_id("rss", guid),
                "pool": "news",
                "source": source,
                "title": entry.get("title", "").strip(),
                "authors": [entry.get("author")] if entry.get("author") else [],
                "abstract": _strip_html(summary)[:1200],
                "url": entry.get("link", ""),
                "published": (published or datetime.now(timezone.utc)).isoformat(),
                "citations": None,
            })

    return out


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text or "").strip()


# ----------------------------------------------------------------- aggregate
def fetch_all(cfg: dict) -> tuple[list[dict], list[dict]]:
    """Run every fetcher. Returns (papers_pool, news_pool)."""
    src = cfg["sources"]

    # PAPERS pool ---------------------------------------------------------
    papers = fetch_arxiv(
        src["arxiv_categories"],
        src["arxiv_lookback_hours"],
        src["arxiv_per_category"],
    )
    if src.get("semantic_scholar", False):
        enrich_semantic_scholar(papers)

    # NEWS pool -----------------------------------------------------------
    news: list[dict] = []
    news += fetch_hackernews(
        src["hackernews_keywords"],
        src["hackernews_min_score"],
    )
    news += fetch_rss(src.get("news_feeds", []))
    news += fetch_rss(src.get("consulting_feeds", []))

    print(f"[fetch] papers={len(papers)} news={len(news)}")
    return papers, news
