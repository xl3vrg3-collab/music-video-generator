"""
V6 Prompt Assembler — bridges packages.json asset metadata into V6 prompts.

Problem: V6 handlers received only raw camera/action text from the UI; the
character/costume/environment/prop fields the user filled in were saved to
packages.json but never reached Gemini or Kling. This module loads those
packages and interpolates their descriptions, must_keep, avoid, and canonical
notes into the prompt via an adlib-style template.

Design goals:
  - Auto-detect entities by case-insensitive name match against all packages
  - Respect Kling/Gemini prompt length budgets (target ~40 words video, ~120 anchor)
  - Return a report of what was injected so the UI can show it
  - Never describe refs in text when refs are attached (per user feedback) —
    caller decides via `include_description` flag. For anchor prompts (which
    build the identity), include_description=True. For video prompts when a
    locked anchor is attached, include_description=False; only enforce
    must_keep + avoid as continuity constraints.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES_PATH = os.path.join(PROJECT_DIR, "output", "preproduction", "packages.json")


def load_packages(
    path: str | None = None,
    project_slug: str | None = None,
) -> list[dict[str, Any]]:
    """Load packages from packages.json. Returns [] on missing/malformed.

    If `project_slug` is provided, only packages whose `project_slug` (or
    legacy `project`) field matches are returned. Packages with no project
    linkage are SKIPPED — this is the 2026-04-20 cross-project leak fix
    (legacy packages.json entries had no project field and were treated as
    globally available, mixing Buddy/Owen/Maya sheets into TB anchor gens).
    """
    target = path or PACKAGES_PATH
    if not os.path.exists(target):
        return []
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        packages = list(data.get("packages", []))
    elif isinstance(data, list):
        packages = list(data)
    else:
        return []
    if project_slug:
        filtered = []
        for p in packages:
            pkg_slug = (p.get("project_slug") or p.get("project") or "").strip()
            if pkg_slug and pkg_slug == project_slug:
                filtered.append(p)
        return filtered
    return packages


def index_packages(packages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return {lowercased_name: package} for fast detection."""
    idx: dict[str, dict[str, Any]] = {}
    for pkg in packages:
        name = (pkg.get("name") or "").strip()
        if name:
            idx[name.lower()] = pkg
    return idx


def detect_entities(
    raw_prompt: str,
    packages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return packages whose names appear as whole words in raw_prompt."""
    if not raw_prompt or not packages:
        return []
    lowered = raw_prompt.lower()
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pkg in packages:
        name = (pkg.get("name") or "").strip()
        if not name:
            continue
        pid = pkg.get("package_id") or name
        if pid in seen:
            continue
        # whole-word match, case-insensitive
        pattern = r"\b" + re.escape(name.lower()) + r"\b"
        if re.search(pattern, lowered):
            hits.append(pkg)
            seen.add(pid)
    return hits


def _split_by_type(
    entities: list[dict[str, Any]],
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    chars, costumes, envs, props = [], [], [], []
    for e in entities:
        t = (e.get("package_type") or "").lower()
        if t == "character":
            chars.append(e)
        elif t == "costume":
            costumes.append(e)
        elif t == "environment":
            envs.append(e)
        elif t == "prop":
            props.append(e)
    return chars, costumes, envs, props


def _dedupe_keep(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        k = (it or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(it.strip())
    return out


def _build_entity_clause(pkg: dict[str, Any], include_description: bool) -> str:
    """One adlib clause for a single entity."""
    name = pkg.get("name") or ""
    desc = (pkg.get("description") or "").strip()
    if include_description and desc:
        return f"{name} ({desc})"
    return name


def assemble_v6_prompt(
    raw_prompt: str,
    shot_context: dict[str, Any] | None = None,
    include_description: bool = True,
    max_chars: int = 900,
    packages: list[dict[str, Any]] | None = None,
    project_slug: str | None = None,
) -> dict[str, Any]:
    """
    Enrich a raw prompt with metadata from detected entity packages.

    Args:
        raw_prompt: UI-assembled camera/action text (may name characters)
        shot_context: optional dict with explicit package_ids to force-inject
          even if names aren't in the raw text (keys: character_ids, env_ids,
          costume_ids, prop_ids — each a list of package_ids)
        include_description: True for anchors (identity-building), False for
          video where a locked anchor reference is attached
        max_chars: soft budget for final enriched prompt
        packages: optionally pre-loaded package list (for tests / caching)
        project_slug: active project; when set, auto-detected and force-injected
          packages are filtered to those owned by this project. Packages with no
          project linkage are dropped. Prevents cross-project name-match leaks.

    Returns:
        {
          "enriched_prompt": str,
          "injected": [{package_id, name, type, ...brief}],
          "must_keep": [str],
          "avoid": [str],
          "report": str,     # human-readable one-liner
          "truncated": bool,
        }
    """
    raw_prompt = (raw_prompt or "").strip()
    pkgs = packages if packages is not None else load_packages(project_slug=project_slug)
    idx = index_packages(pkgs)

    # 1. Auto-detect by name
    detected = detect_entities(raw_prompt, pkgs)
    detected_ids = {p.get("package_id") for p in detected}

    # 2. Force-injected from shot_context (explicit UI selection)
    forced_ids: list[str] = []
    if shot_context:
        for key in ("character_ids", "costume_ids", "env_ids", "prop_ids"):
            vals = shot_context.get(key) or []
            if isinstance(vals, list):
                forced_ids.extend(str(v) for v in vals if v)

    by_id = {p.get("package_id"): p for p in pkgs if p.get("package_id")}
    for fid in forced_ids:
        if fid in by_id and fid not in detected_ids:
            detected.append(by_id[fid])
            detected_ids.add(fid)

    if not detected:
        return {
            "enriched_prompt": raw_prompt,
            "injected": [],
            "must_keep": [],
            "avoid": [],
            "report": "no entities detected",
            "truncated": False,
        }

    chars, costumes, envs, props = _split_by_type(detected)

    # 3. Build adlib clauses
    parts: list[str] = [raw_prompt] if raw_prompt else []

    if chars:
        char_clauses = [_build_entity_clause(c, include_description) for c in chars]
        parts.append("Featuring " + ", ".join(char_clauses))

    if costumes and include_description:
        cos_clauses = [_build_entity_clause(c, True) for c in costumes]
        parts.append("Wearing " + ", ".join(cos_clauses))

    if envs:
        env_clauses = [_build_entity_clause(e, include_description) for e in envs]
        parts.append("Set in " + ", ".join(env_clauses))

    if props and include_description:
        prop_clauses = [_build_entity_clause(p, True) for p in props]
        parts.append("With " + ", ".join(prop_clauses))

    # 4. Consolidate must_keep + avoid across all detected entities
    must_keep: list[str] = []
    avoid: list[str] = []
    for e in detected:
        mk = e.get("must_keep") or []
        av = e.get("avoid") or []
        if isinstance(mk, list):
            must_keep.extend(str(x) for x in mk if x)
        if isinstance(av, list):
            avoid.extend(str(x) for x in av if x)
    must_keep = _dedupe_keep(must_keep)
    avoid = _dedupe_keep(avoid)

    if must_keep:
        parts.append("Maintain: " + ", ".join(must_keep))
    if avoid:
        parts.append("Avoid: " + ", ".join(avoid))

    enriched = ". ".join(p.rstrip(". ") for p in parts if p).strip()
    if enriched and not enriched.endswith("."):
        enriched += "."

    truncated = False
    if len(enriched) > max_chars:
        enriched = enriched[: max_chars - 1].rstrip() + "…"
        truncated = True

    injected_brief = [
        {
            "package_id": e.get("package_id"),
            "name": e.get("name"),
            "type": e.get("package_type"),
            "hero_image_path": e.get("hero_image_path"),
            "lock_strength": e.get("lock_strength"),
        }
        for e in detected
    ]

    type_counts = {
        "character": len(chars),
        "costume": len(costumes),
        "environment": len(envs),
        "prop": len(props),
    }
    report = (
        f"injected {len(detected)} entities "
        f"(chars={type_counts['character']}, env={type_counts['environment']}, "
        f"costume={type_counts['costume']}, prop={type_counts['prop']}); "
        f"must_keep={len(must_keep)}, avoid={len(avoid)}"
    )

    return {
        "enriched_prompt": enriched,
        "injected": injected_brief,
        "must_keep": must_keep,
        "avoid": avoid,
        "report": report,
        "truncated": truncated,
    }


def resolve_reference_paths(
    injected: list[dict[str, Any]],
    limit: int = 4,
) -> list[str]:
    """Return hero_image_paths for injected entities (existing files only)."""
    paths: list[str] = []
    for e in injected:
        p = e.get("hero_image_path")
        if p and os.path.exists(p):
            paths.append(p)
            if len(paths) >= limit:
                break
    return paths


def _url_to_fs_path(url: str) -> str | None:
    """Convert a '/output/.../foo.png' URL to an absolute filesystem path.
    Returns None if the file does not exist on disk.
    """
    if not url or not isinstance(url, str):
        return None
    rel = url.lstrip("/\\").replace("/", os.sep)
    fs = os.path.join(PROJECT_DIR, rel)
    return fs if os.path.isfile(fs) else None


def load_motif_refs_for_shot(
    shot_id: str,
    project: str = "default",
    exclude_ids: set | None = None,
) -> list[str]:
    """Resolve motif approvedRef paths for a given shot_id.

    Accepts EITHER an opus_shot_id ("1a", "4b", ...) OR a scene UUID
    ("8b2684fe-1c8"). Scene UUIDs are resolved to opus_shot_id via scenes.json.

    Reads `output/projects/<project>/prompt_os/shot_ref_map_v8.json` to find
    which motif IDs this shot declares, then reads `references.json` from the
    same dir to get each motif's approvedRef URL, then resolves URLs to real
    filesystem paths. Returns only paths whose files exist.

    This is the bridge that lets anchor-gen pass motif reference images (beads,
    pawprint, gold fur, etc.) into Gemini edit alongside character + env refs,
    so motifs are actually referenced — not just described in prose.
    """
    if not shot_id:
        return []
    shot_id = str(shot_id).strip()
    prompt_os_dir = os.path.join(
        PROJECT_DIR, "output", "projects", project, "prompt_os"
    )
    shot_map_path = os.path.join(prompt_os_dir, "shot_ref_map_v8.json")
    refs_path = os.path.join(prompt_os_dir, "references.json")
    scenes_path = os.path.join(prompt_os_dir, "scenes.json")
    if not (os.path.exists(shot_map_path) and os.path.exists(refs_path)):
        return []
    try:
        with open(shot_map_path, "r", encoding="utf-8") as f:
            shot_map = json.load(f)
        with open(refs_path, "r", encoding="utf-8") as f:
            refs = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    # Resolve scene-UUID → opus_shot_id via scenes.json if needed.
    opus_shot_id = shot_id
    if os.path.exists(scenes_path):
        try:
            with open(scenes_path, "r", encoding="utf-8") as f:
                scenes = json.load(f)
            for sc in (scenes if isinstance(scenes, list) else []):
                if str(sc.get("id", "")) == shot_id:
                    mapped = sc.get("opus_shot_id")
                    if mapped:
                        opus_shot_id = str(mapped)
                    break
        except (OSError, json.JSONDecodeError):
            pass

    motif_ids: list[str] = []
    for s in shot_map.get("shots", []) or []:
        if str(s.get("id", "")).lower() == opus_shot_id.lower():
            motif_ids = [str(m) for m in (s.get("motifs") or []) if m]
            break
    if not motif_ids:
        return []

    ref_by_id: dict[str, dict] = {}
    if isinstance(refs, list):
        for r in refs:
            if isinstance(r, dict) and r.get("id"):
                ref_by_id[str(r["id"])] = r

    paths: list[str] = []
    seen: set[str] = set()
    excl = {str(e) for e in (exclude_ids or set())}
    for mid in motif_ids:
        if str(mid) in excl:
            continue
        entry = ref_by_id.get(mid)
        if not entry:
            continue
        url = entry.get("approvedRef") or ""
        fs = _url_to_fs_path(url)
        if fs and fs not in seen:
            paths.append(fs)
            seen.add(fs)
    return paths


def _pos_entity_sheet_path(entity: dict) -> str | None:
    """Pick the best sheet URL from a POS entity and resolve to fs path.
    Priority: approvedSheet → previewImage → latest sheetImages[-1].url.
    """
    if not isinstance(entity, dict):
        return None
    for key in ("approvedSheet", "previewImage"):
        url = entity.get(key) or ""
        fs = _url_to_fs_path(url) if url else None
        if fs:
            return fs
    sheets = entity.get("sheetImages") or []
    if isinstance(sheets, list):
        for s in reversed(sheets):
            url = (s or {}).get("url") if isinstance(s, dict) else None
            fs = _url_to_fs_path(url) if url else None
            if fs:
                return fs
    return None


def load_pos_entity_refs_for_shot(
    shot_id: str,
    project: str = "default",
    exclude_ids: set | None = None,
) -> list[str]:
    """Resolve scene-specific character + environment + costume sheet paths.

    Reads scenes.json to find the scene by UUID or opus_shot_id, then pulls the
    characterId / environmentId / costumeId FK fields, then reads the per-project
    POS JSON files (characters.json / environments.json / costumes.json) to get
    each entity's sheet URL, and resolves to filesystem paths.

    This is the fix for cross-project ref pollution (v9 backlog #7): the global
    /api/v6/references endpoint mixes Buddy/Owen/Maya with TB; scene-scoped
    resolution ensures only the shot's declared entities are uploaded to Gemini.
    Character first, then env, then costume — so the first 3 ref slots are
    deterministic and scene-correct.
    """
    if not shot_id:
        return []
    shot_id = str(shot_id).strip()
    prompt_os_dir = os.path.join(
        PROJECT_DIR, "output", "projects", project, "prompt_os"
    )
    scenes_path = os.path.join(prompt_os_dir, "scenes.json")
    if not os.path.exists(scenes_path):
        return []
    try:
        with open(scenes_path, "r", encoding="utf-8") as f:
            scenes = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    scene = None
    for sc in (scenes if isinstance(scenes, list) else []):
        sid = str(sc.get("id", ""))
        oid = str(sc.get("opus_shot_id", ""))
        if sid == shot_id or (oid and oid.lower() == shot_id.lower()):
            scene = sc
            break
    if not scene:
        return []

    def _load_entities(fname: str) -> dict[str, dict]:
        p = os.path.join(prompt_os_dir, fname)
        if not os.path.exists(p):
            return {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        items = raw if isinstance(raw, list) else (raw.get("items") or raw.get("characters") or raw.get("environments") or raw.get("costumes") or [])
        return {str(e.get("id", "")): e for e in items if isinstance(e, dict) and e.get("id")}

    chars_by_id = _load_entities("characters.json")
    envs_by_id = _load_entities("environments.json")
    costumes_by_id = _load_entities("costumes.json")

    paths: list[str] = []
    seen: set[str] = set()
    excl = {str(e) for e in (exclude_ids or set())}
    for fk, store in (
        (scene.get("characterId"), chars_by_id),
        (scene.get("environmentId"), envs_by_id),
        (scene.get("costumeId"), costumes_by_id),
    ):
        if not fk:
            continue
        if str(fk) in excl:
            continue
        entity = store.get(str(fk))
        if not entity:
            continue
        fs = _pos_entity_sheet_path(entity)
        if fs and fs not in seen:
            paths.append(fs)
            seen.add(fs)
    return paths


def load_pos_identity_clauses_for_shot(
    shot_id: str,
    project: str = "default",
    exclude_ids: set | None = None,
) -> str:
    """Return the scene's character identityMark text (orientation/shape lock).

    The sheet image alone does not guarantee emblem orientation — Gemini edit
    re-draws marks semantically. Feeding the character's `identityMark` text
    into the prompt gives Gemini an explicit orientation constraint that the
    image reference reinforces. Without this, a tips-up crescent on the sheet
    still drifts to arbitrary rotations in the anchor.

    Returns a single string (possibly empty) ready to append to the anchor
    prompt. Uses characterId from the scene to pull identityMark from the POS
    characters.json.
    """
    if not shot_id:
        return ""
    shot_id = str(shot_id).strip()
    prompt_os_dir = os.path.join(
        PROJECT_DIR, "output", "projects", project, "prompt_os"
    )
    scenes_path = os.path.join(prompt_os_dir, "scenes.json")
    chars_path = os.path.join(prompt_os_dir, "characters.json")
    if not (os.path.exists(scenes_path) and os.path.exists(chars_path)):
        return ""
    try:
        with open(scenes_path, "r", encoding="utf-8") as f:
            scenes = json.load(f)
        with open(chars_path, "r", encoding="utf-8") as f:
            chars = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""

    scene = None
    for sc in (scenes if isinstance(scenes, list) else []):
        sid = str(sc.get("id", ""))
        oid = str(sc.get("opus_shot_id", ""))
        if sid == shot_id or (oid and oid.lower() == shot_id.lower()):
            scene = sc
            break
    if not scene:
        return ""

    char_id = scene.get("characterId")
    if not char_id:
        return ""
    excl = {str(e) for e in (exclude_ids or set())}
    if str(char_id) in excl:
        return ""
    chars_list = chars if isinstance(chars, list) else []
    entity = next((c for c in chars_list if str(c.get("id", "")) == str(char_id)), None)
    if not entity:
        return ""

    name = (entity.get("name") or "character").strip()
    mark = (entity.get("identityMark") or "").strip()
    if not mark:
        return ""
    return f"{name} IDENTITY LOCK (mandatory, overrides all other emblem cues): {mark}"


_AT_MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_\- ]{0,60})")


_PAREN_ALIAS_RE = re.compile(r"\(([^)]+)\)")


def _entity_alias_tokens(it: dict) -> list[str]:
    """Collect alias tokens for an entity: parenthesized (TB), shortName,
    aliases[], and individual words of the name. Returned lowercase, deduped.
    """
    tokens: list[str] = []
    name = (it.get("name") or "").strip()
    if name:
        tokens.append(name.lower())
        for m in _PAREN_ALIAS_RE.findall(name):
            tokens.append(m.strip().lower())
        base = _PAREN_ALIAS_RE.sub("", name).strip()
        if base and base.lower() != name.lower():
            tokens.append(base.lower())
    for fld in ("shortName", "short_name", "alias", "nickname"):
        v = it.get(fld)
        if isinstance(v, str) and v.strip():
            tokens.append(v.strip().lower())
    aliases = it.get("aliases") or it.get("altNames") or []
    if isinstance(aliases, list):
        for a in aliases:
            if isinstance(a, str) and a.strip():
                tokens.append(a.strip().lower())
    seen = set()
    out = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def parse_at_mentions(text: str, project: str = "default") -> list[dict]:
    """Scan text for @<name> tokens and resolve them to POS entities.

    Matches against the entity's canonical name AND its aliases — including
    parenthesized callsigns like "Trillion Bear (TB)" → @TB, shortName /
    aliases[] fields, and substring containment as the loosest fallback.
    Priority: character > environment > costume > motif; within each kind,
    exact alias > prefix > substring.

    Returns [{"kind": "char|env|costume|motif", "id": str, "name": str,
              "sheet_path": str|None}] per unique resolution.
    """
    if not text or "@" not in text:
        return []
    matches = _AT_MENTION_RE.findall(text)
    if not matches:
        return []

    prompt_os_dir = os.path.join(
        PROJECT_DIR, "output", "projects", project, "prompt_os"
    )

    def _load(fname: str, key_plural: str):
        p = os.path.join(prompt_os_dir, fname)
        if not os.path.exists(p):
            return []
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(raw, list):
            return raw
        return raw.get("items") or raw.get(key_plural) or []

    chars = _load("characters.json", "characters")
    envs = _load("environments.json", "environments")
    costumes = _load("costumes.json", "costumes")
    refs = _load("references.json", "references")

    resolved: list[dict] = []
    seen_ids: set[str] = set()

    def _try_resolve(query: str, items: list, kind: str):
        q = query.strip().lower()
        q_compact = q.replace(" ", "")
        if not q:
            return None
        # Three-tier search: exact alias > prefix > substring. We scan all
        # items per tier so the best match wins, not just the first file entry.
        best_exact = None
        best_prefix = None
        best_substr = None
        for it in items:
            if not isinstance(it, dict):
                continue
            aliases = _entity_alias_tokens(it)
            if not aliases:
                continue
            matched = None
            for a in aliases:
                a_compact = a.replace(" ", "")
                if a == q or a_compact == q_compact:
                    matched = "exact"
                    break
                if matched is None and (a.startswith(q) or q.startswith(a)):
                    matched = "prefix"
                if matched in (None, "prefix") and (q in a or q in a_compact):
                    matched = matched or "substr"
                    if matched == "prefix":
                        pass  # prefer prefix
            if matched == "exact" and best_exact is None:
                best_exact = it
                break
            if matched == "prefix" and best_prefix is None:
                best_prefix = it
            if matched == "substr" and best_substr is None:
                best_substr = it
        winner = best_exact or best_prefix or best_substr
        if not winner:
            return None
        return {
            "kind": kind,
            "id": str(winner.get("id", "")),
            "name": winner.get("name"),
            "sheet_path": _pos_entity_sheet_path(winner),
        }

    for raw_match in matches:
        query = raw_match.strip().rstrip(",.;:").strip()
        if not query:
            continue
        # The regex is greedy and allows spaces (so @Young TB works), but that
        # means @bear followed by normal prose captures "bear trudges through
        # rain". Fall back to progressively shorter prefixes so the mention
        # still resolves.
        words = query.split()
        hit = None
        for i in range(len(words), 0, -1):
            attempt = " ".join(words[:i])
            for items, kind in (
                (chars, "char"),
                (envs, "env"),
                (costumes, "costume"),
                (refs, "motif"),
            ):
                hit = _try_resolve(attempt, items, kind)
                if hit:
                    break
            if hit:
                break
        if hit and hit["id"] and hit["id"] not in seen_ids:
            seen_ids.add(hit["id"])
            resolved.append(hit)
    return resolved


if __name__ == "__main__":
    # Self-test against real packages.json
    test_prompts = [
        "Medium shot of Buddy running through the park, golden hour",
        "Owen kneels down to pet Buddy, warm reunion",
        "Wide shot of Autumn Park, empty path, leaves falling",
        "A random shot with no entities mentioned",
    ]
    for tp in test_prompts:
        print("RAW:", tp)
        r = assemble_v6_prompt(tp)
        print("  ->", r["enriched_prompt"])
        print("  report:", r["report"])
        print()
