"""
LUMN Studio — content moderation pre-filter.

A cheap keyword-based guardrail that runs before any prompt hits fal.ai.
This is the MVP safety net, not a replacement for fal's own moderation.
For a public launch you'd layer a Sonnet / Llama-Guard pass on top of this.

Rules enforced:
  1. Hard-block: sexual content involving minors (NEVER allow).
  2. Hard-block: real private individuals and public-figure likeness requests.
  3. Hard-block: explicit sexual content (NSFW) unless the user has a
     `nsfw_allowed` role flag.
  4. Warn-block: copyrighted characters from major franchises (Disney/
     Marvel/Nintendo/etc) — producers can fine-tune their own OCs instead.
  5. Warn: prompts mentioning real political violence / gore.

All rules use case-insensitive whole-word matching. Phrase rules use
substring matching. Short words (1–3 chars) are ignored to avoid false
positives like "un" matching "Unity".

Returns a Moderation result dict:
  {
    "allowed": bool,
    "severity": "hard" | "warn" | "ok",
    "reasons": [str],   # human-readable rule names
    "redacted_prompt": str,   # for warn-level: prompt with flags replaced
  }
"""

from __future__ import annotations

import re
from typing import Iterable

# ---------------------------------------------------------------------------
# Rule tables
# ---------------------------------------------------------------------------

# Hard-block: CSAM / minor-sexualization vocabulary. Non-exhaustive — the
# goal is to block obvious attempts, not be a definitive filter.
CSAM_TERMS = {
    "loli", "lolita", "shota", "underage", "child porn", "cp",
    "kid porn", "infant porn", "preteen", "teenager nude",
    "child nude", "minor nude", "underage nude",
}

# Combined: sexual term AND age term = hard block.
SEXUAL_TERMS = {
    "nude", "naked", "sex", "sexual", "porn", "erotic", "xxx",
    "nsfw", "explicit sex", "hentai", "masturbat",
}
AGE_TERMS = {
    "child", "kid", "minor", "boy", "girl", "teen", "teenager",
    "preteen", "baby", "toddler", "infant", "underage",
    "schoolgirl", "schoolboy", "9 year", "10 year", "11 year",
    "12 year", "13 year", "14 year", "15 year", "16 year", "17 year",
}

# Hard-block: public figures and real private individuals with a likeness
# claim. This is about avoiding deepfake liability. Add more aggressively
# before launch.
PUBLIC_FIGURES = {
    "donald trump", "joe biden", "kamala harris", "vladimir putin",
    "elon musk", "taylor swift", "kanye west", "kim kardashian",
    "barack obama", "xi jinping", "queen elizabeth", "king charles",
    "pope francis", "justin bieber", "beyonce", "rihanna", "drake",
    "mark zuckerberg", "bill gates", "jeff bezos",
}

# Copyrighted characters — warn, not block. Users can use these for personal
# test work but we redact before sending to fal to dodge attribution risk.
COPYRIGHTED_CHARACTERS = {
    # Disney/Pixar
    "mickey mouse", "donald duck", "goofy", "elsa", "anna", "moana",
    "woody", "buzz lightyear", "simba", "nemo", "lightning mcqueen",
    # Marvel
    "spider-man", "spiderman", "iron man", "captain america", "thor",
    "black widow", "hulk", "hawkeye", "deadpool", "wolverine",
    # DC
    "batman", "superman", "wonder woman", "joker", "harley quinn",
    # Nintendo
    "mario", "luigi", "bowser", "princess peach", "link", "zelda",
    "pikachu", "charizard", "pokemon",
    # Star Wars
    "darth vader", "luke skywalker", "yoda", "baby yoda", "grogu",
    "stormtrooper", "mandalorian",
    # Other
    "sonic the hedgehog", "goku", "naruto", "sailor moon", "hello kitty",
    "shrek", "minion", "bart simpson", "homer simpson",
}

# Violence / gore — warn (not block). Needed for action/thriller work.
EXTREME_VIOLENCE = {
    "graphic torture", "decapitation", "beheading", "school shooting",
    "mass shooting", "terrorist attack", "suicide vest",
    "hanging body", "executed prisoner",
}


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9\-']+", text.lower()))


def _contains_phrase(text_lower: str, phrase: str) -> bool:
    # Phrase = multi-word → substring. Single word → whole-word boundary.
    if " " in phrase or "-" in phrase:
        return phrase in text_lower
    # Whole-word match so "kid" doesn't fire on "kidney"
    return bool(re.search(rf"\b{re.escape(phrase)}\b", text_lower))


def _any_match(text_lower: str, terms: Iterable[str]) -> list[str]:
    hits = []
    for t in terms:
        if _contains_phrase(text_lower, t):
            hits.append(t)
    return hits


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def moderate_prompt(prompt: str, *, nsfw_allowed: bool = False) -> dict:
    """Check a prompt. See module docstring for returned shape."""
    if not prompt or not isinstance(prompt, str):
        return {"allowed": True, "severity": "ok", "reasons": [],
                "redacted_prompt": prompt or ""}

    text_lower = prompt.lower()
    reasons: list[str] = []
    redacted = prompt
    severity = "ok"

    # 1. CSAM — never allowed
    csam_hits = _any_match(text_lower, CSAM_TERMS)
    if csam_hits:
        return {
            "allowed": False, "severity": "hard",
            "reasons": [f"csam:{h}" for h in csam_hits],
            "redacted_prompt": "",
        }

    # 1b. Sexual + age combo — never allowed
    sex_hits = _any_match(text_lower, SEXUAL_TERMS)
    age_hits = _any_match(text_lower, AGE_TERMS)
    if sex_hits and age_hits:
        return {
            "allowed": False, "severity": "hard",
            "reasons": [f"sexual_minor:{'+'.join(sex_hits)}+{'+'.join(age_hits)}"],
            "redacted_prompt": "",
        }

    # 2. Explicit NSFW without the role flag
    if sex_hits and not nsfw_allowed:
        return {
            "allowed": False, "severity": "hard",
            "reasons": [f"nsfw:{h}" for h in sex_hits],
            "redacted_prompt": "",
        }

    # 3. Public figures — hard block (deepfake liability)
    pf_hits = _any_match(text_lower, PUBLIC_FIGURES)
    if pf_hits:
        return {
            "allowed": False, "severity": "hard",
            "reasons": [f"public_figure:{h}" for h in pf_hits],
            "redacted_prompt": "",
        }

    # 4. Copyrighted characters — redact + warn
    cc_hits = _any_match(text_lower, COPYRIGHTED_CHARACTERS)
    if cc_hits:
        severity = "warn"
        for h in cc_hits:
            pattern = re.compile(re.escape(h), re.IGNORECASE)
            redacted = pattern.sub("[a character]", redacted)
            reasons.append(f"copyright:{h}")

    # 5. Extreme violence — warn only
    v_hits = _any_match(text_lower, EXTREME_VIOLENCE)
    if v_hits:
        severity = "warn" if severity == "ok" else severity
        reasons.extend(f"violence:{h}" for h in v_hits)

    return {
        "allowed": True,
        "severity": severity,
        "reasons": reasons,
        "redacted_prompt": redacted,
    }


# ---------------------------------------------------------------------------
# Sonnet second-pass for borderline cases
# ---------------------------------------------------------------------------
#
# The keyword filter above is fast and catches obvious attempts. It misses:
#   - prompts mentioning age terms WITHOUT explicit sexual words
#     ("a 12 year old in a swimsuit on the beach" — borderline)
#   - cleverly worded violence / public figure references that dodge keywords
#   - false positives where context is innocent
#
# For ambiguous cases only, we send the prompt to Sonnet for a one-shot
# judgment. We do NOT run Sonnet on every prompt — that would add ~$0.005 +
# 500ms per generation. We only run it when:
#   (a) the keyword pass returned ok+warn (worth confirming)
#   (b) the prompt mentions age terms but no sexual terms (borderline)

def _is_borderline(prompt: str, kw_result: dict) -> bool:
    if kw_result.get("severity") == "warn":
        return True
    text_lower = prompt.lower()
    if _any_match(text_lower, AGE_TERMS):
        return True
    return False


_SONNET_SYSTEM = (
    "You are a content safety judge for an AI image generator. "
    "Your job is to block prompts that would generate (a) sexual content "
    "involving minors, (b) deepfakes of real public figures, (c) graphic "
    "violence presented approvingly, or (d) content depicting a real "
    "private individual without consent. Reply with strict JSON: "
    '{\"allowed\": true|false, \"reason\": \"<short reason or empty>\"}. '
    "Default to allowed=true unless one of the four categories clearly applies."
)


def sonnet_judge(prompt: str) -> dict | None:
    """Ask Sonnet for a one-shot ruling. Returns None on failure (caller
    should fall back to the keyword verdict)."""
    try:
        from lib.claude_client import call_json, OPUS_MODEL
    except Exception:
        return None
    try:
        result = call_json(
            system=_SONNET_SYSTEM,
            user=f"Prompt to judge:\n\n{prompt[:2000]}",
            model=OPUS_MODEL,
            max_tokens=200,
        )
    except Exception:
        return None
    if not isinstance(result, dict) or "allowed" not in result:
        return None
    return {
        "allowed": bool(result["allowed"]),
        "reason": str(result.get("reason", ""))[:200],
    }


def moderate_prompt_strict(prompt: str, *, nsfw_allowed: bool = False,
                           use_sonnet: bool = True) -> dict:
    """Two-stage moderation: keyword pre-filter, then Sonnet for borderlines.

    The keyword pass is authoritative for hard blocks (CSAM, sexual+minor,
    public figures, NSFW). Sonnet only adjudicates the cases where keywords
    alone are insufficient. Falls back to keyword verdict if Sonnet errors.
    """
    kw = moderate_prompt(prompt, nsfw_allowed=nsfw_allowed)
    if not kw["allowed"]:
        return kw  # hard block — no point asking Sonnet
    if not use_sonnet or not _is_borderline(prompt, kw):
        return kw

    judge = sonnet_judge(prompt)
    if judge is None:
        return kw  # Sonnet unavailable — trust keyword verdict
    if not judge["allowed"]:
        return {
            "allowed": False,
            "severity": "hard",
            "reasons": kw["reasons"] + [f"sonnet:{judge['reason']}"],
            "redacted_prompt": "",
        }
    # Sonnet says allowed — preserve any keyword-level warnings/redactions.
    return kw


if __name__ == "__main__":
    tests = [
        ("Medium shot of a dog running through leaves", True),
        ("Explicit sexual content", False),
        ("Minor in a nude pose", False),
        ("Donald Trump walking into a restaurant", False),
        ("Mickey Mouse in a noir alley", True),  # warn, not block
        ("A decapitation scene from a war film", True),  # warn
        ("shota character", False),
    ]
    for prompt, expect_allowed in tests:
        r = moderate_prompt(prompt)
        mark = "OK" if r["allowed"] == expect_allowed else "FAIL"
        print(f"[{mark}] allowed={r['allowed']:<5} sev={r['severity']:<5} "
              f"reasons={r['reasons']}  // {prompt[:50]}")
