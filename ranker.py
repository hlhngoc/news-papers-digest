"""
ranker.py — the LLM brain of the digest, powered by Gemini.

Per the two-pool architecture:
  rank()      -> called ONCE PER POOL (papers, news). Reads candidate
                 titles+abstracts, returns the top-N ids with score + reason.
                 Citation counts (from Semantic Scholar) are shown to the
                 model when present.
  write_up()  -> called ONCE with BOTH pools' winners. Writes per-item
                 summary + personalised "why this matters for you" line,
                 plus ONE overview paragraph tying the whole day together.

Requires env var GEMINI_API_KEY. Uses the google-genai SDK.
"""

from __future__ import annotations

import json
import os
import time

from google import genai
from google.genai import errors as genai_errors

_client: genai.Client | None = None

# Retry on transient server-side issues (overloaded model, rate limit, etc).
# 503/429 are almost always temporary and clear up within a minute or two.
_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 5
_BASE_DELAY_S = 8  # exponential backoff: 8s, 16s, 32s, 64s...


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=key)
    return _client


def _generate_json(model: str, prompt: str) -> dict | list:
    """Call Gemini and parse a JSON response. Retries on transient server
    errors (503 overloaded, 429 rate-limited) with exponential backoff.
    Robust to stray json code fences in the response text."""
    last_err = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = _get_client().models.generate_content(
                model=model,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            text = (resp.text or "").strip()
            if text.startswith("```"):
                text = text.strip("`")
                text = text[text.find("{") if "{" in text else text.find("["):]
            return json.loads(text)
        except genai_errors.APIError as e:
            last_err = e
            code = getattr(e, "code", None)
            if code in _RETRYABLE_CODES and attempt < _MAX_ATTEMPTS:
                delay = _BASE_DELAY_S * (2 ** (attempt - 1))
                print(f"[gemini] {code} on attempt {attempt}/{_MAX_ATTEMPTS}, "
                      f"retrying in {delay}s...")
                time.sleep(delay)
                continue
            raise
    raise last_err


def generate_grounded_json(model: str, prompt: str) -> dict | list:
    """Like _generate_json but enables Gemini's Google Search tool so the model
    can actually browse the web. NOTE: grounding can't be combined with a forced
    JSON mime type, so we ask for JSON in the prompt and parse it loosely.
    Retries on transient 503/429 with the same backoff."""
    from google.genai import types

    last_err = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = _get_client().models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            text = (resp.text or "").strip()
            # Grounded responses often wrap JSON in prose/fences; extract it.
            if "```" in text:
                text = text.split("```")[1] if text.count("```") >= 2 else text
                text = text.lstrip("json").strip("`").strip()
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                text = text[start:end + 1]
            return json.loads(text)
        except genai_errors.APIError as e:
            last_err = e
            code = getattr(e, "code", None)
            if code in _RETRYABLE_CODES and attempt < _MAX_ATTEMPTS:
                delay = _BASE_DELAY_S * (2 ** (attempt - 1))
                print(f"[gemini] {code} on grounded attempt {attempt}/"
                      f"{_MAX_ATTEMPTS}, retrying in {delay}s...")
                time.sleep(delay)
                continue
            raise
        except (json.JSONDecodeError, ValueError) as e:
            # Grounded output is chattier and can fail to parse; don't kill the
            # whole run over a failed discovery pass.
            print(f"[discovery] could not parse grounded JSON: {e}")
            return {}
    raise last_err


# --------------------------------------------------------------------- rank
def rank(candidates: list[dict], profile: str, k: int, model: str,
         pool_name: str) -> list[dict]:
    """Pick the k most relevant candidates from ONE pool."""
    if not candidates:
        return []

    lines = []
    for i, c in enumerate(candidates):
        cites = c.get("citations")
        cite_tag = f" [citations: {cites}]" if cites else ""
        abstract = c["abstract"][:600]
        lines.append(
            f"[{i}] ({c['source']}){cite_tag} {c['title']}\n     {abstract}"
        )
    catalogue = "\n".join(lines)

    pool_hint = (
        "These are RESEARCH PAPERS. Reward methodological relevance and "
        "applicability to their projects; a nonzero citation count on a "
        "brand-new paper is a strong signal of traction."
        if pool_name == "papers" else
        "These are NEWS / INDUSTRY items (tech news, HN stories, consulting "
        "insights). Reward career relevance, industry shifts they should know "
        "about, and practical tools; penalise clickbait and product PR fluff."
    )

    prompt = f"""You are a research curator building a daily digest for one person.

THEIR PROFILE:
{profile}

{pool_hint}

Below are {len(candidates)} candidates, each tagged with an index [i]. Score
each 0-100 for relevance to THIS person specifically, then return the {k} BEST.

Return ONLY valid JSON, no prose, in this exact shape:
{{
  "picks": [
    {{"index": <int>, "score": <int 0-100>, "reason": "<short why, one line>"}}
  ]
}}
Order "picks" highest score first. Exactly {k} items (or fewer if fewer are
genuinely relevant — do not pad with junk).

CANDIDATES:
{catalogue}
"""

    data = _generate_json(model, prompt)
    picks = data.get("picks", []) if isinstance(data, dict) else []

    chosen: list[dict] = []
    for p in picks[:k]:
        idx = p.get("index")
        if isinstance(idx, int) and 0 <= idx < len(candidates):
            item = dict(candidates[idx])
            item["score"] = p.get("score")
            item["pick_reason"] = p.get("reason", "")
            chosen.append(item)
    return chosen


# ------------------------------------------------------------------ write_up
def write_up(items: list[dict], profile: str, model: str,
             language: str) -> dict:
    """Summaries + why-it-matters for BOTH pools' winners, one shared overview.

    `items` should be papers + news concatenated (pool tag preserved).
    Returns {"overview": str, "items": [ {..., summary, why, title_vi, ...} ]}
    """
    if not items:
        return {"overview": "", "items": []}

    lang_rule = (
        "Write summaries, why-it-matters, and the overview in VIETNAMESE. "
        "Also translate each title to Vietnamese ('title_vi'); keep the "
        "original in 'title_original'."
        if language == "vi" else
        "Write everything in ENGLISH. Set 'title_vi' equal to the original title."
    )

    blocks = []
    for i, it in enumerate(items):
        authors = ", ".join(it["authors"][:5]) if it["authors"] else "—"
        cites = it.get("citations")
        cite_line = f"\nCITATIONS: {cites}" if cites else ""
        blocks.append(
            f"[{i}] POOL: {it['pool']} | SOURCE: {it['source']}{cite_line}\n"
            f"TITLE: {it['title']}\n"
            f"AUTHORS: {authors}\n"
            f"ABSTRACT: {it['abstract'][:900]}"
        )
    body = "\n\n".join(blocks)

    prompt = f"""You are writing a daily research digest for one person.

THEIR PROFILE:
{profile}

{lang_rule}

For EACH item below write:
  - "summary": 2-3 sentences, plain and concrete, what it is and its key finding.
  - "why": ONE sentence on why it matters for THIS person given their profile
    (specific to their interests, not generic).
  - "title_vi" and "title_original" per the language rule above.

Also write "overview": ONE short paragraph (2-4 sentences) that ties the whole
day together ACROSS both papers and news — themes, connections, what to pay
attention to. Addressed to the reader.

Return ONLY valid JSON in this exact shape:
{{
  "overview": "<paragraph>",
  "items": [
    {{"index": <int>, "title_vi": "...", "title_original": "...",
      "summary": "...", "why": "..."}}
  ]
}}

ITEMS:
{body}
"""

    data = _generate_json(model, prompt)
    by_index = {d["index"]: d for d in data.get("items", [])
                if isinstance(d.get("index"), int)}

    enriched = []
    for i, it in enumerate(items):
        w = by_index.get(i, {})
        out = dict(it)
        out["title_vi"] = w.get("title_vi", it["title"])
        out["title_original"] = w.get("title_original", it["title"])
        out["summary"] = w.get("summary", it["abstract"][:300])
        out["why"] = w.get("why", "")
        enriched.append(out)

    return {"overview": data.get("overview", ""), "items": enriched}
