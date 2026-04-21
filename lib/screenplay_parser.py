"""Lightweight screenplay parser — maps plain-text or Fountain screenplays
into LUMN shot sheets.

Recognises:
- Scene headings (INT./EXT. LOCATION - TIME)
- Character cues (ALL-CAPS line followed by dialogue)
- Parentheticals (parenthesized lines under a character cue)
- Action lines (everything else)

Converts each scene heading into:
- An environment placeholder (location + time-of-day tone)
- 1-3 shots using story_planner coverage tables when available

Output is the same shape as the AI-generated shot sheet so it can feed
`_uploadShotSheet` directly.
"""

from __future__ import annotations
import re
from typing import List, Dict


SCENE_RE = re.compile(r"^\s*(INT\.?|EXT\.?|INT\.?/EXT\.?|I/E\.?)\s+(.+?)(?:\s+[-–—]\s+(.+))?\s*$", re.IGNORECASE)
CHARACTER_RE = re.compile(r"^\s*[A-Z][A-Z0-9 .'()\-]{1,40}\s*$")
PARENTHETICAL_RE = re.compile(r"^\s*\(.+\)\s*$")
PAGE_BREAK_RE = re.compile(r"^\s*(=+|#.*)\s*$")


def _is_character_cue(line: str, prev_blank: bool) -> bool:
    if not prev_blank:
        return False
    s = line.strip()
    if not s or len(s) > 42:
        return False
    if not CHARACTER_RE.match(s):
        return False
    # Exclude lines that are obviously action (contain lowercase words)
    if re.search(r"[a-z]", s.replace("(", "").replace(")", "")):
        return False
    return True


def parse(text: str) -> Dict:
    """Parse a screenplay into structured scenes, characters, and shots.

    Returns:
        {
          "scenes": [{"heading": str, "location": str, "time": str,
                      "action": str, "characters": [str],
                      "dialogue": [{"character": str, "line": str}]}],
          "characters": [str],
          "environments": [str],
          "shots": [{"id": str, "title": str, "prompt": str, "characters": [str]}]
        }
    """
    lines = text.splitlines()
    scenes: List[Dict] = []
    characters: set = set()
    environments: set = set()
    current: Dict = None
    prev_blank = True
    current_char = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            prev_blank = True
            current_char = None
            continue
        if PAGE_BREAK_RE.match(line):
            prev_blank = True
            continue

        m = SCENE_RE.match(line)
        if m:
            heading = line.strip()
            int_ext = m.group(1).upper().rstrip(".")
            loc_time = m.group(2).strip()
            time = (m.group(3) or "").strip()
            # If time wasn't on a separate dash, try splitting loc on last " - "
            if not time and " - " in loc_time:
                loc, time = loc_time.rsplit(" - ", 1)
            else:
                loc = loc_time
            environments.add(loc)
            current = {
                "heading": heading,
                "int_ext": int_ext,
                "location": loc,
                "time": time or "DAY",
                "action": "",
                "characters": [],
                "dialogue": [],
            }
            scenes.append(current)
            prev_blank = False
            current_char = None
            continue

        if current is None:
            # Pre-scene action/title page — skip
            prev_blank = False
            continue

        if _is_character_cue(line, prev_blank):
            name = re.sub(r"\(.+\)", "", line).strip()
            current_char = name
            characters.add(name)
            if name not in current["characters"]:
                current["characters"].append(name)
            prev_blank = False
            continue

        if current_char and PARENTHETICAL_RE.match(line):
            prev_blank = False
            continue

        if current_char:
            current["dialogue"].append({"character": current_char, "line": line.strip()})
            prev_blank = False
            continue

        # Action line
        current["action"] = (current["action"] + " " + line.strip()).strip()
        prev_blank = False

    # Build shots using simple coverage rules:
    # - Scene wide establish
    # - Medium on primary character (if any)
    # - One dialogue CU per character in the scene (capped at 3)
    shots: List[Dict] = []
    for idx, sc in enumerate(scenes, start=1):
        base_id = f"{idx}"
        loc = sc["location"]
        time = sc["time"]
        ext = sc["int_ext"]
        ambient = f"{time.lower()} light, {'exterior' if ext.startswith('EXT') else 'interior'}"

        # Wide establish
        shots.append({
            "id": f"{base_id}.1",
            "title": f"Wide {ext} {loc}",
            "prompt": f"Wide establishing shot. {loc}, {ambient}. {sc['action'][:200]}",
            "characters": list(sc["characters"]),
            "scene": idx,
        })

        # Medium on first character
        if sc["characters"]:
            primary = sc["characters"][0]
            shots.append({
                "id": f"{base_id}.2",
                "title": f"Medium ({primary})",
                "prompt": f"Medium shot of {primary}. {loc}, {ambient}. {sc['action'][:160]}",
                "characters": [primary],
                "scene": idx,
            })

        # Dialogue coverage (up to 3 unique speakers)
        speakers = []
        for d in sc["dialogue"]:
            if d["character"] not in speakers:
                speakers.append(d["character"])
        for i, spk in enumerate(speakers[:3], start=3):
            first_line = next((d["line"] for d in sc["dialogue"] if d["character"] == spk), "")
            shots.append({
                "id": f"{base_id}.{i}",
                "title": f"Close-up ({spk})",
                "prompt": f"Close-up on {spk}, dialogue. {loc}, {ambient}. Line: \"{first_line[:120]}\"",
                "characters": [spk],
                "scene": idx,
            })

    return {
        "scenes": scenes,
        "characters": sorted(characters),
        "environments": sorted(environments),
        "shots": shots,
    }


def to_shot_sheet_text(parsed: Dict) -> str:
    """Render the parsed shots as the legacy Shot Sheet text format that
    `_uploadShotSheet` already knows how to ingest."""
    lines = []
    for sh in parsed["shots"]:
        chars = sh.get("characters", [])
        char_suffix = f" ({', '.join(chars)})" if chars else ""
        lines.append(f"Shot {sh['id']} — {sh['title']}{char_suffix}")
        lines.append(sh["prompt"])
        lines.append("")
    return "\n".join(lines).strip()
