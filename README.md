# 📰 Daily Papers Digest

A self-running AI agent that fires every morning at 7 AM (Hanoi time): pulls
items from arXiv + Hacker News + RSS (McKinsey/HBR/MIT Sloan...), uses Gemini to
score them against your profile, picks the top 5, writes a summary + a
"why this matters for you" line, then emails an HTML digest via Gmail.

No server, no hosting bill — it all runs on **GitHub Actions**.

## Architecture

```
GitHub Actions cron (00:00 UTC = 07:00 Hanoi)
   └─ fetchers.py   PAPERS: arXiv API (+ Semantic Scholar citation counts)
                    NEWS:   HN API + TechCrunch/Verge AI + McKinsey/HBR/... + VnExpress Số hóa
   └─ db.py         SQLite dedupe (committed back to repo -> remembers old items)
   └─ ranker.py     Gemini: rank each pool separately -> top 2-3 papers + top 2-3 news
                    -> one write-up call: summaries + why-it-matters + day overview
   └─ emailer.py    two-section HTML (📄 Papers | 📰 News & Industry) -> Gmail SMTP
```
GitHub Actions cron (00:00 UTC = 07:00 Hanoi)
   └─ fetchers.py   arXiv API + HN API + RSS feeds
   └─ db.py         SQLite dedupe (committed back to repo -> remembers old items)
   └─ ranker.py     Gemini: score ~90 candidates -> pick 5 -> write summaries + overview
   └─ emailer.py    build HTML -> send via Gmail SMTP
```

## Setup (one time)

1. **Create a repo** and push this whole folder to it.
2. **Gemini API key**: grab a free one at https://aistudio.google.com/apikey
3. **Gmail App Password**: enable 2FA on Gmail, then create a 16-char App Password
   (same idea as your LAW1010 monitor). Do *not* use your normal Gmail password.
4. In the repo go to **Settings → Secrets and variables → Actions** and add:

   | Secret | Value |
   |---|---|
   | `GEMINI_API_KEY` | key from AI Studio |
   | `GMAIL_ADDRESS` | sending gmail, e.g. `abc@gmail.com` |
   | `GMAIL_APP_PASSWORD` | the 16-char app password |
   | `DIGEST_TO` | *(optional)* recipient email; leave empty to send to yourself |

5. Go to the **Actions** tab → pick the workflow → **Run workflow** to test it
   right away without waiting for 7 AM.

## Customizing

Only edit **`profile.yaml`** when your interests change:
- `profile:` — who you are (the LLM reads this to filter + write the "why").
- `papers_count:` / `news_count:` — items per section (2–3 each is the sweet spot).
- `sources.semantic_scholar:` — `true` attaches citation counts to arXiv papers
  (free, no key needed); set `false` to skip and save ~1 min per run.
- `language:` — `vi` or `en`.
- `model:` — `gemini-flash-latest` (default) or `gemini-3.1-flash-lite` (cheapest).
- `sources:` — add/remove arXiv categories, HN keywords, RSS feeds.

## Local testing

```bash
pip install -r requirements.txt
python main.py --dry-run     # fetch + dedupe only, no Gemini/email calls
GEMINI_API_KEY=... GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=... python main.py
```

## Cost

Each run = 3 Gemini calls (rank papers + rank news + write-up). With `gemini-3.1-flash-lite`,
that's a few thousand tokens a day → the **free tier is basically enough**.
A whole month runs a few cents at most.

## Technical notes

- **Dedupe**: `digest.db` (SQLite) is committed back to the repo after each run,
  so state persists across days with no external database. If you'd rather not
  commit the DB into the repo, switch to `actions/cache` — but the cache can be
  evicted after 7 days of no use.
- **arXiv skips weekends**: `arxiv_lookback_hours: 60` so a Monday run still
  catches Friday's papers. Dedupe handles any overlap.
- **VnExpress Số hóa** is in `news_feeds` as optional local tech news — remove
  the line if it's too noisy.
- **Semantic Scholar**: new arXiv papers usually have 0 citations; the value is
  catching the occasional fast riser. The free tier is rate-limited — the code
  fails soft (skips enrichment) if the API says no.
- **Consulting/news RSS**: if a feed dies (URL changes), fix it in `profile.yaml`;
  the fetcher skips broken feeds instead of killing the whole run.
- **Gemini model**: the 2.x Flash models were shut down by Google (June 2026),
  so the code uses `gemini-flash-latest` to always point at the newest Flash.
