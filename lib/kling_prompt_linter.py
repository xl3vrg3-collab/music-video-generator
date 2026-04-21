"""
Kling i2v prompt linter — pre-flight validation to catch the mistakes that
cause bad generations before we pay fal.

Rules come from user feedback (see memory: Kling I2V Prompt Rules):
  - 15-40 words is the sweet spot
  - no sound words (sound designers)
  - no subject re-description (anchor carries identity)
  - camera movement first, then 1-2 actions max
  - no generic dissolves / CGI / fantasy words

Returns severity levels: "error" blocks generation, "warn" surfaces to UI,
"info" is advisory. The caller can choose to enforce only errors or all.
"""

from __future__ import annotations

import re
from typing import Any

# Words that describe audio — Kling is silent video, adding these wastes
# the prompt budget and confuses the model.
SOUND_WORDS = {
    "sound", "audio", "music", "song", "singing", "voice", "whisper",
    "shout", "scream", "bark", "barking", "meow", "howl", "howling",
    "laughter", "laughing", "crying", "weeping", "loud", "quiet",
    "silent", "silence", "echo", "echoing", "ringing", "crackle",
    "rustling",  # borderline, but causes audio hallucination
    "thunder", "thunderclap", "explosion sound",
}

# Camera movement verbs — at least one should appear in the first 8 words
CAMERA_VERBS = {
    "pan", "pans", "panning", "tilt", "tilts", "tilting",
    "zoom", "zooms", "zooming", "dolly", "dollies", "tracking",
    "tracks", "orbit", "orbits", "orbiting", "push", "pushes",
    "pull", "pulls", "handheld", "steadicam", "crane", "rise",
    "rises", "rising", "descend", "descends", "descending",
    "static", "locked", "lock-off", "hold", "holds",
    "slow", "subtle",  # as modifiers still indicate intent
}

# Anti-patterns — never in Kling prompts.
# NOTE: "anime" / "cartoon" are NOT banned — they're valid style descriptors
# for anime projects (e.g., "Makoto Shinkai anime cel shading"). When the
# project intent is photorealism, catch drawn-style words via a different rule.
BAD_WORDS = {
    "dissolve", "cross-fade", "fade to black", "cgi", "3d render",
    "illustration", "sketch",
    "perfect", "flawless", "masterpiece",  # Kling ignores these
    "8k", "4k", "ultra hd",  # resolution hype is pointless in video
}

# Subject re-description red flags — usually signals anchor duplication
REDESC_PATTERNS = [
    r"\bwearing (a |an )?",
    r"\bdressed in\b",
    r"\bhair (is|are)\b",
    r"\beyes (are|is)\b",
    r"\bskin (is|tone)\b",
]


def _word_count(text: str) -> int:
    return len([w for w in re.findall(r"\b[\w'-]+\b", text) if w])


def lint_kling_prompt(
    prompt: str,
    strict: bool = False,
) -> dict[str, Any]:
    """
    Lint a Kling i2v prompt. Returns:
      {
        "ok": bool,             # True if no errors (warns allowed)
        "word_count": int,
        "issues": [
          {"severity": "error|warn|info", "rule": "...", "message": "..."}
        ],
        "suggestions": [str],   # quick-fix ideas
      }
    If strict=True, warnings are promoted to errors.
    """
    prompt = (prompt or "").strip()
    issues: list[dict[str, str]] = []
    suggestions: list[str] = []

    if not prompt:
        return {
            "ok": False,
            "word_count": 0,
            "issues": [{"severity": "error", "rule": "empty", "message": "Prompt is empty."}],
            "suggestions": ["Add camera movement + 1-2 actions (15-40 words)."],
        }

    wc = _word_count(prompt)
    lower = prompt.lower()
    tokens = re.findall(r"\b[\w'-]+\b", lower)

    # Rule 1: word count. Kling V3 handles longer prompts than V2; 90+ is still
    # where we see the model drop content. Warn earlier, error later.
    if wc < 10:
        issues.append({
            "severity": "warn",
            "rule": "word_count_low",
            "message": f"Prompt is {wc} words — Kling sweet spot is 15-40.",
        })
        suggestions.append("Add a camera move and one environmental micro-motion.")
    elif wc > 90:
        issues.append({
            "severity": "error",
            "rule": "word_count_high",
            "message": f"Prompt is {wc} words — Kling ignores content past ~90.",
        })
        suggestions.append("Trim to camera + 1-2 actions + 1 micro-motion.")
    elif wc > 60:
        issues.append({
            "severity": "warn",
            "rule": "word_count_over",
            "message": f"Prompt is {wc} words — tighter is better (15-40 sweet spot).",
        })

    # Rule 2: sound words
    bad_sound = [w for w in SOUND_WORDS if w in tokens]
    if bad_sound:
        issues.append({
            "severity": "error",
            "rule": "sound_words",
            "message": f"Remove audio words: {', '.join(bad_sound[:5])}. Kling is silent.",
        })
        suggestions.append("Describe visual motion instead of sound cues.")

    # Rule 3: camera intent in opening
    head_tokens = tokens[:12]
    if not any(cv in head_tokens for cv in CAMERA_VERBS):
        issues.append({
            "severity": "warn",
            "rule": "no_camera_intent",
            "message": "No camera movement in first 12 words. Lead with pan / tilt / dolly / static.",
        })
        suggestions.append("Prefix with 'Slow push-in:' or 'Static hold:' or 'Subtle handheld:'.")

    # Rule 4: anti-patterns
    bad_hits = [w for w in BAD_WORDS if w in lower]
    if bad_hits:
        issues.append({
            "severity": "error",
            "rule": "banned_words",
            "message": f"Remove banned terms: {', '.join(bad_hits[:5])}.",
        })

    # Rule 5: subject re-description
    for pat in REDESC_PATTERNS:
        if re.search(pat, lower):
            issues.append({
                "severity": "warn",
                "rule": "subject_redesc",
                "message": f"Possible subject re-description ('{pat.strip()}'). Anchor carries identity — remove.",
            })
            suggestions.append("Delete wardrobe/appearance descriptions; keep action only.")
            break

    # Rule 6: too many actions (heuristic — count commas between verbs after first)
    comma_count = prompt.count(",")
    if comma_count > 6:
        issues.append({
            "severity": "info",
            "rule": "many_clauses",
            "message": f"{comma_count} commas — check for run-on or multiple actions (max 2).",
        })

    # Promote warnings to errors in strict mode
    if strict:
        for it in issues:
            if it["severity"] == "warn":
                it["severity"] = "error"

    errors = [i for i in issues if i["severity"] == "error"]
    return {
        "ok": len(errors) == 0,
        "word_count": wc,
        "issues": issues,
        "suggestions": suggestions[:5],
    }


if __name__ == "__main__":
    tests = [
        "Slow push-in on Buddy running through fallen leaves, golden hour sun flickering through oaks.",
        "Buddy wearing a red collar, honey-gold coat, bright brown eyes, sitting there.",
        "wow",
        "Handheld tracking shot, Owen kneels, reaches toward Buddy, barking loudly, thunder crashes, dissolve",
        "A beautiful cinematic masterpiece of a dog in 8k ultra hd running perfectly through a magical fantasy forest",
    ]
    for t in tests:
        r = lint_kling_prompt(t)
        print(f"[{r['word_count']:3d}w ok={r['ok']}] {t[:70]}")
        for i in r["issues"]:
            print(f"   {i['severity']:<5} {i['rule']}: {i['message']}")
        print()
