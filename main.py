"""
main.py — the orchestrator. Run this once a day.

Pipeline (two-pool architecture):
    fetch (papers + news pools)
      -> dedupe each pool (SQLite)
      -> rank each pool SEPARATELY (Gemini)   [papers top 2-3 | news top 2-3]
      -> one shared write_up: summaries + why + overview tying the day together
      -> two-section HTML email (📄 Papers | 📰 News & Industry)
      -> Gmail SMTP -> mark seen

Local dry run (no email, no API key needed for fetch stage):
    python main.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import yaml

import db
import discovery
import emailer
import fetchers
import ranker


def load_config(path: str = "profile.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _prep_pool(items: list[dict], cap: int) -> list[dict]:
    """Dedupe against DB, newest first, cap the candidate count."""
    fresh = db.filter_new(items)
    fresh.sort(key=lambda x: x.get("published", ""), reverse=True)
    return fresh[:cap]


def run(dry_run: bool = False) -> None:
    cfg = load_config()
    lang = cfg.get("language", "en")
    model = cfg.get("model", "gemini-flash-latest")
    k_papers = cfg.get("papers_count", 3)
    k_news = cfg.get("news_count", 3)
    cap = cfg.get("max_candidates_per_pool", 60)

    db.init()

    # 1. FETCH -----------------------------------------------------------
    papers_raw, news_raw = fetchers.fetch_all(cfg)

    # 1b. DISCOVERY (optional) — free search APIs for extra items ---------
    if cfg.get("discovery", False):
        found = discovery.discover(
            cfg.get("discovery_queries", []),
            cfg["sources"]["hackernews_min_score"],
            cfg.get("discovery_per_query", 3),
        )
        for it in found:
            (papers_raw if it["pool"] == "papers" else news_raw).append(it)

    # 2. DEDUPE + trim per pool -------------------------------------------
    papers = _prep_pool(papers_raw, cap)
    news = _prep_pool(news_raw, cap)
    print(f"[dedupe] papers: {len(papers)}/{len(papers_raw)} new | "
          f"news: {len(news)}/{len(news_raw)} new")

    if not papers and not news:
        print("[done] nothing new today — no email sent.")
        return

    if dry_run:
        print(f"\n--- DRY RUN ---")
        print(f"PAPERS pool ({len(papers)}):")
        for c in papers[:10]:
            print(f"  ({c['source']}) {c['title'][:75]}")
        print(f"NEWS pool ({len(news)}):")
        for c in news[:10]:
            print(f"  ({c['source']}) {c['title'][:75]}")
        print("Stopping before any Gemini/email calls.")
        return

    # 3. RANK — each pool separately --------------------------------------
    top_papers = ranker.rank(papers, cfg["profile"], k_papers, model,
                             pool_name="papers") if papers else []
    top_news = ranker.rank(news, cfg["profile"], k_news, model,
                           pool_name="news") if news else []

    # 3b. GUARANTEE a minimum of mainstream news (TechCrunch/VentureBeat/etc).
    # If the normal ranking didn't surface enough, rank the mainstream-only
    # candidates and slot the best of them in, keeping total at k_news.
    min_ms = cfg.get("news_mainstream_min", 0)
    domains = [d.lower() for d in cfg.get("news_mainstream_domains", [])]
    if news and min_ms > 0 and domains:
        def _is_mainstream(it: dict) -> bool:
            url = (it.get("url") or "").lower()
            return any(d in url for d in domains)

        have = [it for it in top_news if _is_mainstream(it)]
        if len(have) < min_ms:
            ms_pool = [it for it in news if _is_mainstream(it)]
            if ms_pool:
                ms_top = ranker.rank(ms_pool, cfg["profile"], min_ms, model,
                                     pool_name="news")
                merged, seen_ids = [], set()
                for it in ms_top:                    # reserved mainstream first
                    if it["id"] not in seen_ids:
                        merged.append(it); seen_ids.add(it["id"])
                for it in top_news:                  # fill the rest by rank
                    if len(merged) >= k_news:
                        break
                    if it["id"] not in seen_ids:
                        merged.append(it); seen_ids.add(it["id"])
                top_news = merged[:k_news]
                print(f"[rank] guaranteed {len(ms_top)} mainstream news items")

    print(f"[rank] papers picked: {len(top_papers)} | news picked: {len(top_news)}")

    chosen = top_papers + top_news
    if not chosen:
        print("[done] ranker found nothing relevant.")
        return  # nothing sent -> mark nothing, keep items eligible next run

    # 4. WRITE-UP — one call covering both pools ---------------------------
    digest = ranker.write_up(chosen, cfg["profile"], model, lang)

    # 5. EMAIL -------------------------------------------------------------
    subject = (
        f"📰 Daily Papers — {datetime.now():%d/%m}"
        if lang == "vi" else
        f"📰 Daily Papers Digest — {datetime.now():%b %d}"
    )
    html_body = emailer.build_html(digest, lang)
    emailer.send(subject, html_body)

    # 6. RECORD -------------------------------------------------------------
    # Only mark items we ACTUALLY SENT as seen. Items fetched but not selected
    # stay eligible for future runs, so the digest doesn't run dry when you
    # re-run several times in one day.
    db.mark_seen(digest["items"])
    db.log_sent(digest["items"])
    print("[done] digest complete.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + dedupe only, no Gemini/email")
    args = ap.parse_args()
    try:
        run(dry_run=args.dry_run)
    except Exception as e:
        print(f"[fatal] {e}", file=sys.stderr)
        raise
