<div align="center">

# 📰 Daily Papers & News Digest

**A personalised AI research digest, delivered to your inbox every morning.**

Pulls from arXiv, Hacker News and 18 RSS feeds, ranks everything against *your*
interests with Gemini, and emails you the handful of items actually worth reading.

[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Gemini](https://img.shields.io/badge/LLM-Gemini_Flash-4285F4?logo=google&logoColor=white)](https://ai.google.dev/)
[![GitHub Actions](https://img.shields.io/badge/runs_on-GitHub_Actions-2088FF?logo=githubactions&logoColor=white)](https://github.com/features/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](#license)

</div>

---

## Overview

Keeping up with AI research and industry news means checking a dozen sources a
day and reading mostly noise. This project does it for you.

Every morning at **07:00 (Asia/Ho_Chi_Minh)** a GitHub Actions job fetches
several hundred candidate items, discards everything you have already been sent,
scores the rest against a plain-text description of your interests, and emails
you a short, two-section digest — each item summarised and paired with an
explicit reason it matters *to you specifically*.

It runs entirely on free infrastructure. No server, no database, no hosting bill.

> **Why not just use a newsletter?** A newsletter is written for everyone. This
> is ranked for one person — the `profile` block in `profile.yaml` is read
> verbatim by the model on every run, so changing your interests is a one-line edit.

### Features

- **Two-pool ranking** — research papers and industry news are ranked
  separately, so one never crowds out the other
- **Personalised relevance** — every item is scored `0–100` against your written
  profile; each one ships with a *why this matters for you* line
- **Guaranteed source mix** — a configurable minimum of items from major outlets
  so the digest stays grounded in what the field is actually talking about
- **Self-discovery** — free academic and news search APIs surface relevant work
  from outside your fixed feed list
- **Reliable deduplication** — SQLite-backed, with stable content hashes; you are
  never sent the same item twice
- **Bilingual output** — Vietnamese or English summaries, with original titles preserved
- **Resilient by design** — per-source failure isolation, model fallback chains,
  output-language verification, and no silent quality degradation

---

## Sample output

The digest arrives as a single responsive HTML email:

```
┌────────────────────────────────────────────────────┐
│  📰 Daily Papers Digest       Wednesday, 25 Nov     │
├────────────────────────────────────────────────────┤
│  TỔNG QUAN HÔM NAY                                 │
│  A short paragraph tying the day's themes together  │
├────────────────────────────────────────────────────┤
│  📄 Papers                                          │
│                                                     │
│  ARXIV                        92/100   📈 12 cites  │
│  Hiệu chuẩn cảm biến chi phí thấp bằng GNN          │
│  Low-Cost Sensor Calibration via Graph Networks     │
│  Two or three sentences of plain summary…           │
│  ┃ 💡 Vì sao liên quan tới mày: …                   │
│  Đọc bài gốc →                                      │
├────────────────────────────────────────────────────┤
│  📰 Tin tức & Ngành                                 │
│  …                                                  │
└────────────────────────────────────────────────────┘
```

Open [`sample_email.html`](sample_email.html) in a browser for the rendered version.

---

## Architecture

```
          GitHub Actions cron — 00:00 UTC (07:00 Hanoi)
                              │
          ┌───────────────────┴───────────────────┐
          ▼                                       ▼
   PAPERS POOL                              NEWS POOL
   arXiv API (7 categories)                 Hacker News API
   └─ raw-HTTP fallback                     13 tech / AI RSS feeds
   Semantic Scholar (citation counts)       5 consulting / strategy RSS feeds
          │                                       │
          └───────────────────┬───────────────────┘
                              ▼
              DISCOVERY — free search APIs (optional)
              Semantic Scholar search · HN Algolia search
                              │
                              ▼
                  DEDUPLICATION — SQLite (digest.db)
                  stable md5 ids · committed back to repo
                              │
                              ▼
                      RANKING — Gemini
              ① papers pool → top N   ② news pool → top N
              ③ mainstream top-up     ④ summaries + overview
                              │
                              ▼
            HTML EMAIL — two sections → Gmail SMTP (TLS)
                              │
                              ▼
                  RECORD — mark sent, commit digest.db
```

---

## Quickstart

### Prerequisites

- A GitHub account
- A [Gemini API key](https://aistudio.google.com/apikey) (free tier is sufficient)
- A Gmail account with 2FA enabled

### 1. Fork or clone

```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>
```

### 2. Create a Gmail App Password

Enable 2-Step Verification, then generate a 16-character app password at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
Your regular Gmail password will not work with SMTP.

### 3. Add repository secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Required | Description |
|--------|:--------:|-------------|
| `GEMINI_API_KEY` | ✅ | API key from Google AI Studio |
| `GMAIL_ADDRESS` | ✅ | Sending Gmail address |
| `GMAIL_APP_PASSWORD` | ✅ | 16-character app password |
| `DIGEST_TO` | — | Recipient, if different from the sender |

### 4. Edit your profile

Open `profile.yaml` and rewrite the `profile` block to describe yourself. This
single field drives the entire ranking — be specific about what you work on and
what you would actually act on.

### 5. Run it

**Actions → daily-paper-digest → Run workflow.**

After the first manual run succeeds, the schedule takes over automatically.

---

## Configuration

Everything is configured in [`profile.yaml`](profile.yaml). You should not need
to touch the Python.

### Identity

```yaml
profile: |
  I'm a Data Science student focused on quantitative finance,
  applied ML, and IoT sensing. I want practical, applicable work —
  things I could cite in a project or use to make a career decision.
```

The most important setting in the file. It is passed verbatim to the model for
both ranking and summary generation. Vague input produces a vague digest.

### Digest shape

| Key | Default | Description |
|-----|:-------:|-------------|
| `papers_count` | `3` | Items in the 📄 Papers section |
| `news_count` | `7` | Items in the 📰 News section |
| `news_mainstream_min` | `2` | Minimum items from major outlets (`0` disables) |
| `news_mainstream_domains` | 6 domains | Which domains count as mainstream |
| `language` | `vi` | `vi` for Vietnamese summaries, `en` for English |
| `model` | `gemini-flash-latest` | Any Gemini model ID |

### Discovery

| Key | Default | Description |
|-----|:-------:|-------------|
| `discovery` | `true` | Enables the free-search layer |
| `discovery_queries` | 7 queries | Topics to search for beyond your feeds |
| `discovery_per_query` | `3` | Results per query, per source |

### Sources

| Key | Default | Description |
|-----|:-------:|-------------|
| `arxiv_categories` | 7 | See the [arXiv taxonomy](https://arxiv.org/category_taxonomy) |
| `arxiv_lookback_hours` | `60` | Keep above 24 — arXiv does not publish on weekends |
| `arxiv_per_category` | `40` | Candidates per category before ranking |
| `semantic_scholar` | `true` | Attach citation counts to papers |
| `hackernews_keywords` | 14 | Title keywords for front-page filtering |
| `hackernews_min_score` | `50` | Minimum points |
| `news_feeds` | 13 | Tech / AI RSS feeds |
| `consulting_feeds` | 5 | Strategy and consulting RSS feeds |
| `max_candidates_per_pool` | `60` | Caps prompt size and token cost |

Broken or renamed feeds are skipped automatically, so editing the feed lists is safe.

### Changing the schedule

Edit the cron expression in `.github/workflows/digest.yml`. GitHub cron is
always UTC:

```yaml
- cron: "0 0 * * *"   # 00:00 UTC = 07:00 Hanoi
```

> `UTC hour = local hour − 7` for UTC+7. For 06:00 Hanoi, use `0 23 * * *`.

---

## Project structure

```
.
├── .github/workflows/digest.yml   Schedule and CI job
├── profile.yaml                   All configuration
├── main.py                        Pipeline orchestrator
├── fetchers.py                    arXiv, Hacker News, RSS, citation enrichment
├── discovery.py                   Free-search layer (Semantic Scholar, HN Algolia)
├── ranker.py                      Gemini ranking, summaries, retries, QA
├── emailer.py                     HTML composition and Gmail SMTP delivery
├── db.py                          SQLite deduplication and audit log
├── requirements.txt
├── digest.db                      Auto-created, auto-committed
└── sample_email.html              Rendered example
```

All fetchers normalise to one schema, so the rest of the pipeline is
source-agnostic:

```python
{
    "id":        str,          # stable content hash, used for deduplication
    "pool":      str,          # "papers" | "news"
    "source":    str,          # "arXiv" | "Hacker News" | feed name
    "title":     str,
    "authors":   list[str],
    "abstract":  str,          # the text the ranker reads
    "url":       str,
    "published": str,          # ISO 8601
    "citations": int | None,   # papers only
}
```

---

## How it works

**1 · Fetch.** Each arXiv category is queried newest-first within a 60-hour
window. If the `arxiv` package returns nothing — which happens intermittently
under throttling — a raw Atom API fallback takes over. RSS feeds and the Hacker
News front page are fetched in parallel to this. Any individual source that
fails is logged and skipped.

**2 · Discover.** Each configured query is run against Semantic Scholar's paper
search and Hacker News' Algolia index. Papers with an arXiv ID get their link
rewritten to the canonical `arxiv.org/abs/…` URL. Results are tagged `🔎` and
merged into the normal pools, where they compete on relevance like everything
else. The pass is fail-soft — rate limits yield zero items, never an error.

**3 · Deduplicate.** Item IDs are checked against the `seen` table in
`digest.db`, which the workflow commits back to the repository after each run.
This gives persistent state with no external database.

**4 · Rank.** `ranker.rank()` is called once per pool with pool-specific
instructions — papers are judged on methodological relevance and citation
traction, news on career relevance and industry significance. The model returns
JSON: the top N indices with a score and a one-line justification.

**5 · Top up.** If fewer than `news_mainstream_min` selected items come from a
major outlet, a second ranking pass runs over mainstream-only candidates and
slots the best in, keeping the total fixed.

**6 · Write up.** A single call generates summaries, per-item relevance lines,
and the overview paragraph. The output is then verified — if Vietnamese was
requested and the model answered in English, the call is retried with stronger
instructions, and the run fails rather than shipping the wrong language.

**7 · Send and record.** The email is composed with inline styles (mail clients
strip `<style>` blocks) and sent over TLS. Only *after* a successful send are
items marked as seen, so a failed run loses nothing and is always safe to retry.

---

## Development

Dry run — fetch and deduplicate only, no API calls, no email:

```bash
pip install -r requirements.txt
python main.py --dry-run
```

Full local run:

```bash
export GEMINI_API_KEY=...
export GMAIL_ADDRESS=...
export GMAIL_APP_PASSWORD=...
python main.py
```

The workflow commits `digest.db` back to the repository, so pull before pushing:

```bash
git pull --no-rebase origin main
```

---

## Design notes

Decisions that are deliberate rather than accidental:

**Pools are ranked separately.** With a single combined ranking, whichever
category had a busy day swamped the other. Two calls guarantee a stable mix.

**IDs are md5, not `hash()`.** Python's built-in `hash()` is salted per process,
so the same article produced a different ID on every run and deduplication
silently never matched. Content hashes fixed it. arXiv IDs also drop the version
suffix so `v1 → v2` is not treated as a new paper.

**Only *sent* items are marked seen.** Marking everything *fetched* exhausted the
candidate pool when the workflow ran several times in a day. Unselected items now
stay eligible.

**Search APIs instead of LLM web grounding.** Grounding bills per search query,
returns occasionally imperfect links, and has no memory of what was already sent —
so it does not solve deduplication either. Searching with free APIs and letting
the model rank the results achieves the same reach at zero cost, with real links.

**Failures are loud.** An earlier version degraded to emailing raw abstracts when
the model was unavailable. That shipped visibly broken digests. It now fails
instead — which costs nothing, because nothing is marked as seen until delivery.

**Retries are patient.** Backoff is 30s → 60s → 120s → 120s per model, then the
next model in the fallback chain. Overload is usually transient; a late digest
beats no digest.

---

## Troubleshooting

| Log message | Cause | Resolution |
|-------------|-------|------------|
| `503 UNAVAILABLE` | Model overloaded | Expected under load; retries and model fallback usually recover. Re-run later if not. |
| `429 RESOURCE_EXHAUSTED` | Gemini quota exhausted | Too many runs today. Wait for the quota window to reset. |
| `[discovery/s2] HTTP 429` | Semantic Scholar rate limit | Harmless — discovery returns zero and the run continues. |
| `[arxiv] HTTP 429` | arXiv throttling the runner IP | Caused by frequent runs in a short window. Wait it out. |
| `papers=0` | arXiv package returned nothing | The HTTP fallback normally covers this; otherwise arXiv is throttling. |
| `SMTPRecipientsRefused: {'': …}` | `GMAIL_ADDRESS` empty or malformed | Re-enter the secret by hand, without trailing whitespace. |
| Sparse news section | Several runs in one day | Normal — feeds need time to produce new items. |
| Repeated items | Legacy `digest.db` with old-format IDs | Delete `digest.db` once; it rebuilds itself. |
| Workflow never triggers | File not on the default branch, or first cycle skipped | Confirm `.github/workflows/digest.yml` is on `main` and allow a day. |

> **Reading the logs:** GitHub interleaves stdout and stderr, so a traceback
> often appears *above* the `[fetch]` and `[rank]` lines even though it occurred
> after them. Follow the `[tag]` lines for the true sequence.

---

## Cost

| Component | Cost |
|-----------|------|
| GitHub Actions | Free |
| arXiv · Hacker News · RSS | Free |
| Semantic Scholar | Free, no key required |
| Gemini (3–4 calls per run) | Free tier is normally sufficient |

Using `gemini-3.1-flash-lite` keeps usage comfortably inside the free tier.

---

## Limitations

- **GitHub cron is approximate.** Runs are frequently minutes late, and the first
  cycle after a workflow is added is sometimes skipped. This is GitHub behaviour,
  not a bug in this project.
- **Semantic Scholar's anonymous tier is strict.** Discovery often returns nothing
  on busy days. A free API key would raise the limit substantially.
- **TLDR feeds are community-maintained.** TLDR publishes no official RSS; the
  mirrors used here can lag or break, and are skipped automatically when they do.
- **Relevance is a model judgement.** If a favourite source never appears, it is
  usually losing on relevance to your `profile` text rather than failing to fetch.
- **Free-tier LLM availability is not guaranteed.** During broad congestion no
  amount of retrying helps; the next scheduled run recovers.

---

## Roadmap

- [ ] Semantic Scholar API key support for reliable discovery
- [ ] Per-source weighting in the ranking prompt
- [ ] Weekly digest mode in addition to daily
- [ ] Web archive of past digests

---

## License

Released under the MIT License. See [`LICENSE`](LICENSE) for details.

## Acknowledgements

Built on the open APIs of [arXiv](https://arxiv.org/help/api),
[Semantic Scholar](https://www.semanticscholar.org/product/api),
[Hacker News](https://github.com/HackerNews/API) and
[Algolia HN Search](https://hn.algolia.com/api), with summaries by
[Google Gemini](https://ai.google.dev/).
