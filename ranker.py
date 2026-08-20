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

# Be patient rather than fast: when Gemini is overloaded, waiting a minute or
# two usually works, and a late digest beats a bad one. Delays PER model:
_RETRY_DELAYS_S = [30, 60, 120, 120]     # 4 attempts, ~5.5 min per model
_MAX_ATTEMPTS = len(_RETRY_DELAYS_S)

# When a model stays overloaded, fall back to these in order. Overload is
# usually per-model, so a different Flash variant often goes through.
_FALLBACK_MODELS = ["gemini-3.1-flash-lite", "gemini-flash-latest",
                    "gemini-2.5-flash"]


def _model_chain(primary: str) -> list[str]:
    """Primary model first, then fallbacks (de-duplicated)."""
    chain, seen = [], set()
    for m in [primary, *_FALLBACK_MODELS]:
        if m and m not in seen:
            seen.add(m)
            chain.append(m)
    return chain


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
    errors (503 overloaded, 429 rate-limited) with exponential backoff, and
    falls back to alternate models if one stays overloaded.
    Robust to stray json code fences in the response text."""
    last_err = None
    for m in _model_chain(model):
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = _get_client().models.generate_content(
                    model=m,
                    contents=prompt,
                    config={"response_mime_type": "application/json"},
                )
                text = (resp.text or "").strip()
                if text.startswith("```"):
                    text = text.strip("`")
                    text = text[text.find("{") if "{" in text else text.find("["):]
                if m != model:
                    print(f"[gemini] succeeded on fallback model {m}")
                return json.loads(text)
            except genai_errors.APIError as e:
                last_err = e
                code = getattr(e, "code", None)
                if code in _RETRYABLE_CODES and attempt < _MAX_ATTEMPTS:
                    delay = _RETRY_DELAYS_S[attempt - 1]
                    print(f"[gemini] {m}: {code} attempt {attempt}/"
                          f"{_MAX_ATTEMPTS}, waiting {delay}s before retry...")
                    time.sleep(delay)
                    continue
                if code in _RETRYABLE_CODES:
                    print(f"[gemini] {m} still {code} after {_MAX_ATTEMPTS} "
                          f"tries, falling back to next model...")
                    break  # exhausted this model -> try next in chain
                raise      # non-retryable error -> stop now
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
        "about, practical tools, AND notable mainstream AI news such as major "
        "model releases, funding rounds, acquisitions, and product launches "
        "from outlets like TechCrunch/VentureBeat/WIRED — these keep them "
        "current on the field. Only skip pure clickbait with no substance. "
        "Aim for a mix of sources rather than 3 items from the same one."
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
# Vietnamese-specific diacritics — English text will contain none of these.
_VI_CHARS = set("ăâđêôơưàảãáạằẳẵắặầẩẫấậèẻẽéẹềểễếệ"
                "ìỉĩíịòỏõóọồổỗốộờởỡớợùủũúụừửữứựỳỷỹýỵ")
# Common Vietnamese function words, as a second signal.
_VI_WORDS = {"và", "của", "cho", "với", "này", "được", "các", "những",
             "trong", "là", "có", "người", "một", "để", "khi", "từ"}


def _looks_vietnamese(data: dict) -> bool:
    """True if the model's write-up is actually in Vietnamese.

    Checks the free-text fields (not titles, which may legitimately stay
    English). Requires both diacritics and common Vietnamese words, so an
    English text with one stray accented name doesn't pass.
    """
    if not isinstance(data, dict):
        return False
    chunks = [str(data.get("overview", ""))]
    for d in data.get("items", []):
        if isinstance(d, dict):
            chunks.append(str(d.get("summary", "")))
            chunks.append(str(d.get("why", "")))
    text = " ".join(chunks).lower().strip()
    if not text:
        return False
    has_diacritics = any(ch in _VI_CHARS for ch in text)
    word_hits = sum(1 for w in _VI_WORDS if f" {w} " in f" {text} ")
    return has_diacritics and word_hits >= 2


def write_up(items: list[dict], profile: str, model: str,
             language: str) -> dict:
    """Summaries + why-it-matters for BOTH pools' winners, one shared overview.

    `items` should be papers + news concatenated (pool tag preserved).
    Returns {"overview": str, "items": [ {..., summary, why, title_vi, ...} ]}
    """
    if not items:
        return {"overview": "", "items": []}

    lang_rule = (
        "CRITICAL LANGUAGE REQUIREMENT: every 'summary', every 'why', the "
        "'overview', and every 'title_vi' MUST be written in VIETNAMESE. "
        "Do NOT write them in English. The source material is in English — "
        "translate and rewrite it into natural Vietnamese. Keep the untouched "
        "English title in 'title_original'. Technical terms (machine learning, "
        "transformer, backtesting, etc.) may stay in English inside otherwise "
        "Vietnamese sentences."
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

    # Ask, then VERIFY the language. Models under load sometimes ignore the
    # language instruction and answer in English; retry rather than ship it.
    data = {}
    for lang_try in range(1, 3):
        data = _generate_json(model, prompt)
        if language != "vi" or _looks_vietnamese(data):
            break
        print(f"[lang] output came back in English on try {lang_try}/2, "
              f"asking again in Vietnamese...")
        prompt += ("\n\nIMPORTANT: your previous answer was in English. "
                   "Rewrite EVERYTHING (summary, why, overview, title_vi) "
                   "in VIETNAMESE this time.")
    else:
        # Both tries came back English — better to fail than send a digest in
        # the wrong language; the caller will surface the error.
        raise RuntimeError(
            "Gemini kept returning English despite Vietnamese being requested"
        )

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
