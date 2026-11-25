# 📰 Daily Papers & News Digest

A self-hosted AI agent that emails you a personalised research + industry digest
every morning at **07:00 Hanoi time**.

Every day it pulls from arXiv, Hacker News, and ~18 RSS feeds, searches free
academic/news APIs for anything the feeds missed, ranks everything against a
written profile of your interests using Gemini, and emails you the best few
items with a Vietnamese summary and a "why this matters for you" line.

No server, no hosting bill — it runs entirely on **GitHub Actions**.

---

## Table of contents

- [What you actually receive](#what-you-actually-receive)
- [Architecture](#architecture)
- [How it works, step by step](#how-it-works-step-by-step)
- [File structure](#file-structure)
- [Setup](#setup)
- [Configuration reference](#configuration-reference)
- [Running it](#running-it)
- [Design decisions](#design-decisions)
- [Troubleshooting](#troubleshooting)
- [Cost](#cost)
- [Known limitations](#known-limitations)

---

## What you actually receive

One HTML email per day, in Vietnamese, split into two sections:

**📄 Papers** (3 items) — research papers from arXiv, with citation counts where
Semantic Scholar has them.

**📰 Tin tức & Ngành** (7 items) — tech news, Hacker News stories, and
consulting/strategy pieces. At least 2 are guaranteed to come from mainstream
outlets (TechCrunch, VentureBeat, WIRED, The Verge, Ars Technica, MIT Tech
Review) so you stay current on the field, not just on research.

Each item shows:
- source label, relevance score (`0–100`), and citation badge (`📈 N cites`) if available
- Vietnamese title, with the original English title underneath
- a 2–3 sentence Vietnamese summary
- a **💡 Vì sao liên quan tới mày** line — why *this specific person* should care
- a link to the original

Above both sections is a short overview paragraph tying the day's themes
together across papers and news.

Items discovered by the free-search layer (rather than the fixed feeds) are
tagged with **🔎** in the source label.

---

## Architecture

```
GitHub Actions cron — 00:00 UTC = 07:00 Hanoi
        │
        ▼
  FETCH LAYER (fetchers.py)
  ├─ PAPERS pool
  │    arXiv API — cs.LG, cs.AI, cs.CL, stat.ML, stat.AP, q-fin.TR, q-fin.ST
  │    └─ HTTP fallback if the arxiv package returns nothing
  │    Semantic Scholar batch — attaches citation counts
  │
  └─ NEWS pool
       Hacker News Firebase API — front page, keyword-filtered
       13 tech/AI RSS feeds — TechCrunch, Verge, Ars, VentureBeat, MIT TR,
                              HuggingFace, BAIR, The Gradient, MarkTechPost,
                              KDnuggets, TLDR AI, TLDR Data, VnExpress Số hóa
       5 consulting RSS feeds — McKinsey, MIT Sloan, HBR, BCG, Wharton
        │
        ▼
  DISCOVERY LAYER (discovery.py) — optional, free, no API key
       Semantic Scholar search  → papers matching your topic queries
       Hacker News Algolia API  → recent stories matching your topic queries
       Results tagged 🔎 and merged into the pools above
        │
        ▼
  DEDUPE & STORE (db.py) — SQLite, committed back to the repo
       Stable md5 ids; only items you were ACTUALLY SENT get marked seen
        │
        ▼
  LLM LAYER (ranker.py) — Gemini
       ① rank PAPERS pool   → top 3, with score + reason
       ② rank NEWS pool     → top 7, with score + reason
       ③ (if needed) rank mainstream-only pool to guarantee ≥2 big-outlet items
       ④ write-up: Vietnamese summaries + "why this matters" + day overview
          └─ output language is verified; English replies trigger a retry
        │
        ▼
  EMAIL (emailer.py) — two-section HTML → Gmail SMTP (TLS, port 587)
        │
        ▼
  RECORD — mark sent items seen, log to audit table, commit digest.db
```

---

## How it works, step by step

### 1. Fetch

`fetchers.fetch_all()` returns two separate lists — a **papers** pool and a
**news** pool. Every item, regardless of source, is normalised to one schema:

```python
{
  "id":        str,          # stable unique id, used for dedupe
  "pool":      str,          # "papers" | "news"
  "source":    str,          # "arXiv" | "Hacker News" | "<feed name>"
  "title":     str,
  "authors":   list[str],
  "abstract":  str,          # the text the ranker reads
  "url":       str,
  "published": str,          # ISO 8601
  "citations": int | None,   # papers only, via Semantic Scholar
}
```

arXiv is queried per category, newest-first, within a 60-hour window (wide
enough that a Monday run still catches Friday's papers — arXiv skips weekends).
If the `arxiv` package returns zero results — which it does intermittently when
throttled — a raw HTTP fallback hits the Atom API directly.

Any single feed or category that fails is logged and skipped. One broken source
never kills the run.

### 2. Discovery (optional)

`discovery.discover()` takes the topic queries from `profile.yaml` and runs each
against two **free, keyless** search APIs:

- **Semantic Scholar search** — returns papers with abstracts and citation
  counts; arXiv papers get their link rewritten to the real `arxiv.org/abs/…` URL
- **Hacker News Algolia search** — returns recent stories above a point threshold

This is how the digest surfaces things your fixed feed list would never see.
Results are tagged `🔎` and dropped into the normal pools, where they compete
on relevance like everything else — they are not given special treatment.

Anything without a real `http(s)` URL is discarded. The whole pass is
fail-soft: if the APIs rate-limit, you get zero discovered items and the run
continues normally.

### 3. Dedupe

`db.filter_new()` checks each item's id against the `seen` table in
`digest.db` (SQLite). The database file is committed back to the repo by the
workflow after each run, so state persists with no external database.

Ids are **deterministic md5 hashes** — this matters, see
[Design decisions](#design-decisions).

Each pool is then sorted newest-first and capped at 60 candidates to keep the
ranking prompt lean.

### 4. Rank

`ranker.rank()` is called **once per pool**, so papers and news never compete
against each other for slots. Each call sends all candidate titles + abstracts
to Gemini and asks for JSON: the top N indices with a `0–100` relevance score
and a one-line reason.

The two pools get different instructions:
- **papers** — reward methodological relevance and applicability; a nonzero
  citation count on a brand-new paper signals traction
- **news** — reward career relevance, industry shifts, practical tools, *and*
  notable mainstream AI news (model releases, funding, acquisitions); only skip
  pure clickbait; prefer a mix of sources over 3 items from one outlet

### 5. Mainstream guarantee

If fewer than `news_mainstream_min` of the chosen news items come from a
mainstream domain, the ranker runs once more over mainstream-only candidates,
and the best of those are slotted in first. Total stays at `news_count`.

If no mainstream outlet published anything that day, nothing breaks — you just
get the normal ranking.

### 6. Write-up

`ranker.write_up()` makes one call covering both pools' winners, producing per
item a Vietnamese title, summary, and "why this matters for you" line, plus one
overview paragraph tying the day together.

The output is then **verified**: `_looks_vietnamese()` checks the free-text
fields for Vietnamese diacritics *and* common Vietnamese function words. If
Gemini answered in English anyway (which overloaded models sometimes do), the
call is repeated with a stronger instruction. After two failed attempts the run
raises rather than shipping a wrong-language digest.

### 7. Email and record

The HTML is assembled with inline styles (email clients strip `<style>` blocks)
and sent via Gmail SMTP over TLS.

Only **after a successful send** are items marked seen. A failed run therefore
loses nothing and is always safe to retry.

---

## File structure

```
.
├── .github/workflows/digest.yml   # cron schedule + CI job
├── profile.yaml                   # ← THE ONLY FILE YOU NORMALLY EDIT
├── main.py                        # orchestrator
├── fetchers.py                    # arXiv, HN, RSS, Semantic Scholar enrichment
├── discovery.py                   # free-search layer (S2 + HN Algolia)
├── ranker.py                      # Gemini ranking, write-up, retries, language check
├── emailer.py                     # HTML builder + Gmail SMTP sender
├── db.py                          # SQLite dedupe + audit log
├── requirements.txt
├── digest.db                      # auto-created, auto-committed
└── sample_email.html              # rendered example you can open in a browser
```

---

## Setup

### 1. Push the repo

```bash
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

`.github/workflows/digest.yml` must sit at exactly that path, on the default
branch, or GitHub will not register the schedule.

### 2. Get a Gemini API key

Free at <https://aistudio.google.com/apikey>.

### 3. Create a Gmail App Password

Enable 2FA on the account, then create a 16-character App Password at
<https://myaccount.google.com/apppasswords>. Do **not** use your normal Gmail
password — SMTP will reject it.

### 4. Add repository secrets

**Settings → Secrets and variables → Actions**

| Secret | Required | Value |
|---|---|---|
| `GEMINI_API_KEY` | yes | key from AI Studio |
| `GMAIL_ADDRESS` | yes | the sending Gmail address |
| `GMAIL_APP_PASSWORD` | yes | the 16-character app password |
| `DIGEST_TO` | no | recipient, if different from the sender |

Type these by hand rather than pasting — a trailing newline in `GMAIL_ADDRESS`
produces a cryptic SMTP `555` error. (The code now strips whitespace and
validates both values with a clear error message, but clean input is still
better.)

### 5. Test

**Actions** tab → **daily-paper-digest** → **Run workflow**.

Check the log for `[fetch]`, `[dedupe]`, `[rank]`, and `[email] sent to …`.
First emails sometimes land in Spam.

---

## Configuration reference

Everything lives in `profile.yaml`.

### Identity

| Key | What it does |
|---|---|
| `profile` | Free-text description of you. Gemini reads this **verbatim** to decide relevance and to write the "why this matters" line. The single most important setting — be specific. Vague profiles produce vague digests. |

### Digest shape

| Key | Default | What it does |
|---|---|---|
| `papers_count` | `3` | items in the 📄 Papers section |
| `news_count` | `7` | items in the 📰 News section |
| `news_mainstream_min` | `2` | minimum items from big outlets; `0` disables |
| `news_mainstream_domains` | 6 domains | what counts as "mainstream" (matched against the URL) |
| `language` | `vi` | `vi` = Vietnamese summaries with original titles kept; `en` = everything English |

### Model

| Key | Default | Notes |
|---|---|---|
| `model` | `gemini-flash-latest` | always the newest Flash. `gemini-3.1-flash-lite` is cheapest and often less congested |

Fallback models are hardcoded in `ranker.py` (`_FALLBACK_MODELS`) — if the
chosen model stays overloaded, the code walks the chain automatically.

### Discovery

| Key | Default | Notes |
|---|---|---|
| `discovery` | `true` | master switch for the free-search layer |
| `discovery_queries` | 7 queries | the topics to search for — this is the discovery "profile", edit as interests shift |
| `discovery_per_query` | `3` | items pulled per query, per source |

### Sources

| Key | Notes |
|---|---|
| `arxiv_categories` | see the [arXiv taxonomy](https://arxiv.org/category_taxonomy) |
| `arxiv_lookback_hours` | `60` — keep above 24 so Monday catches Friday |
| `arxiv_per_category` | `40` candidates per category before ranking |
| `semantic_scholar` | `true` — attach citation counts; set `false` to save ~1 min/run |
| `hackernews_keywords` | title keywords; padded with spaces so `" ai "` doesn't match "brain" |
| `hackernews_min_score` | `50` points minimum |
| `news_feeds` | 13 tech/AI RSS feeds |
| `consulting_feeds` | 5 strategy/consulting RSS feeds |
| `max_candidates_per_pool` | `60` — caps prompt size and token cost |

---

## Running it

**Scheduled** — automatic, `0 0 * * *` UTC = 07:00 Hanoi.
GitHub cron is best-effort: runs are often minutes late, and the very first run
after a workflow is added is sometimes skipped entirely.

To change the time, edit the cron in `.github/workflows/digest.yml`.
Formula: `UTC hour = Hanoi hour − 7`. For 06:00 Hanoi, use `0 23 * * *`.

**Manual** — Actions tab → Run workflow.

**Local dry run** — fetch and dedupe only, no Gemini calls, no email:

```bash
pip install -r requirements.txt
python main.py --dry-run
```

**Local full run**:

```bash
GEMINI_API_KEY=... GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=... python main.py
```

Note the workflow commits `digest.db` back to the repo, so pull before pushing
your own changes:

```bash
git pull --no-rebase origin main
```

---

## Design decisions

These are the non-obvious choices, and why they're that way.

**Two separate pools, ranked separately.** If papers and news share one ranking,
one category swamps the other on any given day. Separate calls guarantee a
stable mix.

**Deterministic ids, not `hash()`.** Python's built-in `hash()` is salted per
process (`PYTHONHASHSEED`), so the same RSS article produced a *different* id
on every run and dedupe never matched — the digest repeated items daily. Ids
are now `md5` based. arXiv ids also drop the version suffix, so a paper going
`v1 → v2` isn't treated as new.

**Only sent items are marked seen.** Marking everything *fetched* as seen meant
that re-running a few times in one day exhausted the pool and the next digest
came back nearly empty. Now the fetched-but-unselected items stay eligible.

**Free search instead of Gemini grounding.** Letting Gemini browse via Google
Search grounding works, but it bills per search query (outside the free tier,
which returns `429`), returns occasionally imperfect links, and still has no
memory of what was sent yesterday — so it doesn't solve deduplication either.
Searching with free APIs and letting the free-tier model rank the results gives
the same "self-discovery" effect at zero cost, with real links.

**Fail loudly rather than degrade silently.** An earlier version fell back to
emailing raw English abstracts when Gemini was down. That shipped a visibly
broken digest. Now the run fails instead — and because nothing is marked seen
until the email sends, failing costs nothing.

**Patient retries over fast ones.** Delays are 30s → 60s → 120s → 120s per
model, then the next model in the chain. Overload is usually transient; a late
digest beats no digest.

**Language is verified, not assumed.** Models under load drop instructions.
The write-up output is checked for Vietnamese diacritics *and* function words
(so one accented name in an English paragraph doesn't pass), and retried if it
came back English.

---

## Troubleshooting

| Symptom in the log | Cause | What to do |
|---|---|---|
| `503 UNAVAILABLE` | Gemini overloaded | Expected under load. Retries + model fallback usually recover. If every model fails, re-run later — nothing was lost. |
| `429 RESOURCE_EXHAUSTED` (Gemini) | free-tier quota hit | You've run too many times today. Wait for quota reset. |
| `[discovery/s2] … HTTP 429` | Semantic Scholar rate limit | Harmless — discovery returns 0 and the run continues. A free S2 API key raises the limit a lot. |
| `[arxiv] … HTTP 429` | arXiv throttling your runner IP | Caused by running repeatedly in a short window. Wait it out. |
| `papers=0` | arxiv package returned nothing | The HTTP fallback should cover it; if not, arXiv is throttling. |
| `SMTPRecipientsRefused: {'': …}` | `GMAIL_ADDRESS` empty or malformed | Re-enter the secret by hand. The code now raises a clear message instead. |
| News section nearly empty | you re-ran several times in one day | Normal — the feeds need time to produce new items. Fixed by the sent-only dedupe. |
| Same items repeating daily | old `digest.db` with legacy random ids | Delete `digest.db` once (`git rm digest.db`); it rebuilds itself. |
| Summaries in English | Gemini ignored the language rule | Now auto-retried, then the run fails rather than sending it. |
| Workflow never runs on schedule | file not on the default branch, or GitHub skipped the first cycle | Confirm `.github/workflows/digest.yml` is on `main`; give it a day. |

**Reading the log:** GitHub interleaves stdout and stderr, so the traceback
often appears *above* the `[fetch]` / `[rank]` lines even though it happened
after them. Read the `[tag]` lines for the real sequence.

---

## Cost

| Component | Cost |
|---|---|
| GitHub Actions | free (public repos; private repos use free-tier minutes) |
| arXiv, Hacker News, RSS | free |
| Semantic Scholar | free, no key (rate-limited) |
| Gemini — 3–4 calls/run | free tier is normally sufficient |

A realistic month is cents at most, and usually nothing. Using
`gemini-3.1-flash-lite` keeps it firmly inside the free tier.

---

## Known limitations

- **GitHub cron is approximate.** Expect delays; occasional skipped days are
  normal GitHub behaviour, not a bug in this code.
- **TLDR feeds are community-maintained.** TLDR publishes no official RSS; the
  `bullrich.dev` mirrors can lag or break. They're skipped automatically if so.
- **Semantic Scholar's free tier is strict.** Discovery often returns nothing
  on busy days. A free API key would fix this.
- **Relevance is a model judgement.** If a source you like never appears, it's
  usually losing on relevance to your `profile` text — not a broken feed. Widen
  `profile`, raise `news_count`, or add the domain to
  `news_mainstream_domains`.
- **Gemini free tier has no uptime guarantee.** When Google is congested
  across the board, no amount of retrying helps; the next day's run recovers.
