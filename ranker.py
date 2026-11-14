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
from google.genai import types

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
        # Pin v1beta: google-genai >=3 defaults the Gemini API to "v1", where
        # aliases like "gemini-flash-latest" (and some config fields such as
        # response_json_schema) don't exist -> 404 NOT_FOUND.
        _client = genai.Client(
            api_key=key,
            # timeout in ms: a hung connection fails fast and gets retried
            # instead of eating the whole job's time budget.
            http_options=types.HttpOptions(api_version="v1beta",
                                           timeout=180_000),
        )
    return _client


def _generate_json(model: str, prompt: str, schema: dict | None = None,
                    max_output_tokens: int | None = None) -> dict | list:
    """Call Gemini and parse a JSON response. Retries on transient server
    errors (503 overloaded, 429 rate-limited) AND on malformed/truncated JSON
    (also usually transient — a re-roll or a different model fixes it), with
    backoff, falling back to alternate models if one stays overloaded.

    Thinking is disabled: this is plain extraction/translation, not multi-step
    reasoning, and on thinking-enabled models "thoughts" silently eat into the
    output token budget, which is exactly what caused truncated/invalid JSON
    on the fuller prompts (many items). Disabling it frees the whole budget
    for the actual answer. Some models reject thinking_budget=0 outright, so
    that combination is retried once without thinking_config before moving on.
    Robust to stray json code fences in the response text."""
    last_err = None
    for m in _model_chain(model):
        no_thinking = True
        attempt = 1
        while attempt <= _MAX_ATTEMPTS:
            config: dict = {"response_mime_type": "application/json"}
            if schema is not None:
                config["response_json_schema"] = schema
            if max_output_tokens:
                config["max_output_tokens"] = max_output_tokens
            if no_thinking:
                config["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            try:
                resp = _get_client().models.generate_content(
                    model=m, contents=prompt, config=config,
                )
                text = (resp.text or "").strip()
                if text.startswith("```"):
                    text = text.strip("`")
                    text = text[text.find("{") if "{" in text else text.find("["):]
                if not text:
                    raise ValueError("empty response text")
                data = json.loads(text)
                if m != model:
                    print(f"[gemini] succeeded on fallback model {m}")
                return data
            except genai_errors.ClientError as e:
                # Some models reject thinking_budget=0 with a 400 — retry the
                # same attempt once without it rather than burning a retry slot.
                if no_thinking and getattr(e, "code", None) == 400:
                    print(f"[gemini] {m}: rejected thinking_budget=0, "
                          f"retrying without it...")
                    no_thinking = False
                    continue
                last_err = e
                # Model retired/renamed/unavailable for this key -> don't kill
                # the whole run, move on to the next model in the chain.
                if getattr(e, "code", None) == 404:
                    print(f"[gemini] {m}: 404 model not found, "
                          f"falling back to next model...")
                    break
                if getattr(e, "code", None) == 429:
                    # ClientError covers 429 too; let the retry logic below
                    # handle it like other transient errors.
                    if attempt < _MAX_ATTEMPTS:
                        delay = _RETRY_DELAYS_S[attempt - 1]
                        print(f"[gemini] {m}: 429 attempt {attempt}/"
                              f"{_MAX_ATTEMPTS}, waiting {delay}s before retry...")
                        time.sleep(delay)
                        attempt += 1
                        continue
                    print(f"[gemini] {m} still 429, falling back to next model...")
                    break
                raise
            except genai_errors.APIError as e:
                last_err = e
                code = getattr(e, "code", None)
                if code in _RETRYABLE_CODES and attempt < _MAX_ATTEMPTS:
                    delay = _RETRY_DELAYS_S[attempt - 1]
                    print(f"[gemini] {m}: {code} attempt {attempt}/"
                          f"{_MAX_ATTEMPTS}, waiting {delay}s before retry...")
                    time.sleep(delay)
                    attempt += 1
                    continue
                if code in _RETRYABLE_CODES:
                    print(f"[gemini] {m} still {code} after {_MAX_ATTEMPTS} "
                          f"tries, falling back to next model...")
                    break  # exhausted this model -> try next in chain
                raise      # non-retryable error -> stop now
            except (json.JSONDecodeError, ValueError) as e:
                # Malformed/truncated JSON — treat like a retryable server
                # error: same model gets another roll, then fall back.
                last_err = e
                if attempt < _MAX_ATTEMPTS:
                    delay = _RETRY_DELAYS_S[attempt - 1]
                    print(f"[gemini] {m}: bad JSON ({e}) attempt {attempt}/"
                          f"{_MAX_ATTEMPTS}, waiting {delay}s before retry...")
                    time.sleep(delay)
                    attempt += 1
                    continue
                print(f"[gemini] {m} still returning bad JSON after "
                      f"{_MAX_ATTEMPTS} tries, falling back to next model...")
                break
            except Exception as e:
                # Network-level failures (server dropped the connection,
                # read timeout, connection reset...) come from the HTTP layer,
                # not as APIError. They're transient: retry, then fall back.
                last_err = e
                name = type(e).__name__
                if attempt < _MAX_ATTEMPTS:
                    delay = _RETRY_DELAYS_S[attempt - 1]
                    print(f"[gemini] {m}: network error {name}: {e} attempt "
                          f"{attempt}/{_MAX_ATTEMPTS}, waiting {delay}s...")
                    time.sleep(delay)
                    attempt += 1
                    continue
                print(f"[gemini] {m} still failing with {name}, "
                      f"falling back to next model...")
                break
    raise last_err


_RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "score", "reason"],
            },
        },
    },
    "required": ["picks"],
}


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

    # Output is just k short picks regardless of catalogue size — a small,
    # generous budget is plenty and keeps the model from stalling on it.
    out_tokens = min(2048, 300 * k + 400)
    data = _generate_json(model, prompt, schema=_RANK_SCHEMA,
                          max_output_tokens=out_tokens)
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
_WRITE_UP_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "title_vi": {"type": "string"},
                    "title_original": {"type": "string"},
                    "summary": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["index", "title_vi", "title_original",
                             "summary", "why"],
            },
        },
    },
    "required": ["overview", "items"],
}

# Vietnamese-specific diacritics — English text will contain none of these.
_VI_CHARS = set("ăâđêôơưàảãáạằẳẵắặầẩẫấậèẻẽéẹềểễếệ"
                "ìỉĩíịòỏõóọồổỗốộờởỡớợùủũúụừửữứựỳỷỹýỵ")
# Common Vietnamese function words, as a second signal.
_VI_WORDS = {"và", "của", "cho", "với", "này", "được", "các", "những",
             "trong", "là", "có", "người", "một", "để", "khi", "từ"}


def _has_vi_diacritics(text: str) -> bool:
    return any(ch in _VI_CHARS for ch in text.lower())


def _looks_vietnamese(text: str) -> bool:
    """Aggregate check for a longer chunk of text: requires both diacritics
    and common function words, so one stray accented name doesn't pass."""
    text = text.lower().strip()
    if not text:
        return False
    has_diacritics = any(ch in _VI_CHARS for ch in text)
    word_hits = sum(1 for w in _VI_WORDS if f" {w} " in f" {text} ")
    return has_diacritics and word_hits >= 2


def _validate_write_up(data, n_items: int, language: str) -> str | None:
    """None if `data` is a complete, correctly-languaged write-up; otherwise
    a short human-readable reason it was rejected (used both to decide
    whether to retry and, if retries run out, to explain the final failure).

    Checking completeness AND per-item language here (not just an aggregate
    "does the whole blob look Vietnamese" check) matters: if the model drops
    or skips an item, the rest of the response can still look fine in
    aggregate while that one item silently ends up without a translated
    summary — which is exactly what used to slip through and ship English
    text inside an otherwise-Vietnamese digest.
    """
    if not isinstance(data, dict):
        return "response was not a JSON object"
    items = data.get("items")
    if not isinstance(items, list):
        return "missing 'items' array"
    by_index = {d.get("index"): d for d in items if isinstance(d, dict)}
    missing = [i for i in range(n_items) if i not in by_index]
    if missing:
        return f"missing write-up for item index(es) {missing}"
    for i in range(n_items):
        d = by_index[i]
        summary = str(d.get("summary") or "").strip()
        why = str(d.get("why") or "").strip()
        if not summary or not why:
            return f"item {i} has an empty summary or why"
        if language == "vi" and not (_has_vi_diacritics(summary)
                                      and _has_vi_diacritics(why)):
            return f"item {i}'s summary/why came back in English"
    if language == "vi" and not _looks_vietnamese(str(data.get("overview", ""))):
        return "overview came back in English"
    return None


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

    # Generous per-item output budget — this is what was getting silently
    # squeezed by thinking tokens on the fuller (10-item) prompts, producing
    # truncated JSON. ~450 tokens/item covers summary+why+two titles.
    out_tokens = min(8192, 450 * len(items) + 700)

    # Ask, then VALIDATE: completeness (every item present) and, for "vi",
    # that EVERY item individually is actually in Vietnamese — not just the
    # response as a whole. Retry rather than ship a partial/wrong-language
    # digest; fail loudly if it still doesn't come together.
    data: dict = {}
    reason = None
    for attempt in range(1, 4):
        data = _generate_json(model, prompt, schema=_WRITE_UP_SCHEMA,
                              max_output_tokens=out_tokens)
        reason = _validate_write_up(data, len(items), language)
        if reason is None:
            break
        print(f"[write_up] attempt {attempt}/3 rejected ({reason}), "
              f"retrying...")
        prompt += (
            f"\n\nIMPORTANT: your previous answer was rejected — {reason}. "
            f"Return EVERY item index 0..{len(items) - 1} with a non-empty "
            f"summary and why"
            + (", written in VIETNAMESE." if language == "vi" else ".")
        )
    else:
        raise RuntimeError(
            f"Gemini could not produce a usable write-up after 3 tries "
            f"(last problem: {reason})"
        )

    by_index = {d.get("index"): d for d in data["items"] if isinstance(d, dict)}

    enriched = []
    for i, it in enumerate(items):
        w = by_index[i]
        out = dict(it)
        out["title_vi"] = w.get("title_vi") or it["title"]
        out["title_original"] = w.get("title_original") or it["title"]
        out["summary"] = w["summary"]
        out["why"] = w["why"]
        enriched.append(out)

    return {"overview": data.get("overview", ""), "items": enriched}
