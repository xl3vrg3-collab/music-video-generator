"""F1 Coverage-tier schema (2026-04-19).

Gives each scene row a declared coverage target (`coverageTier`) and a logical
grouping key (`sceneGroupId`) so the renderer/stitcher knows whether multiple
shots cover the same beat. Reconciles the P0/P1/P2 shorthand from
project_lumn_shot_coverage_template.md with the richer mode library in
lib/coverage_system.py.

Tier contract:

| Tier         | Required sizes             | coverage_mode  | Kling cost vs P0 |
|--------------|----------------------------|----------------|------------------|
| P0           | wide                       | minimal        | 1.00x            |
| P0+P1        | wide, medium, close        | standard       | 1.75x            |
| P0+P1+P2     | + ECU / insert             | full_cinematic | 1.85x            |
| full         | + env plate                | music_video    | 2.70x            |
"""
from __future__ import annotations

from typing import Iterable

VALID_TIERS = ("P0", "P0+P1", "P0+P1+P2", "full")
DEFAULT_TIER = "P0"

TIER_TO_MODE = {
    "P0":          "minimal",
    "P0+P1":       "standard",
    "P0+P1+P2":    "full_cinematic",
    "full":        "music_video",
}

TIER_REQUIRED_SIZES = {
    "P0":       ("wide",),
    "P0+P1":    ("wide", "medium", "close"),
    "P0+P1+P2": ("wide", "medium", "close", "insert"),
    "full":     ("wide", "medium", "close", "insert", "plate"),
}

_SIZE_ALIASES = {
    "wide establishing": "wide",
    "wide": "wide",
    "master wide": "wide",
    "medium shot": "medium",
    "medium": "medium",
    "medium front": "medium",
    "close-up": "close",
    "close up": "close",
    "close": "close",
    "close reaction": "close",
    "extreme close detail": "insert",
    "insert detail": "insert",
    "ecu": "insert",
    "macro": "insert",
    "insert": "insert",
    "cutaway environment": "plate",
    "plate": "plate",
    "texture / atmosphere plate": "plate",
}


def canon_size(s: str) -> str:
    if not s:
        return ""
    key = s.strip().lower()
    return _SIZE_ALIASES.get(key, key.split()[0] if key else "")


def validate_tier(tier: str) -> str:
    return tier if tier in VALID_TIERS else DEFAULT_TIER


def tier_to_mode(tier: str) -> str:
    return TIER_TO_MODE.get(validate_tier(tier), TIER_TO_MODE[DEFAULT_TIER])


def required_sizes(tier: str) -> tuple:
    return TIER_REQUIRED_SIZES.get(validate_tier(tier), TIER_REQUIRED_SIZES[DEFAULT_TIER])


def group_key(scene: dict) -> str:
    """Return the logical-scene key for a row. Prefers an explicit
    sceneGroupId, then opus_scene_id, then the row's own id."""
    return (
        str(scene.get("sceneGroupId") or "").strip()
        or str(scene.get("opus_scene_id") or "").strip()
        or str(scene.get("id") or "").strip()
    )


def group_scenes(rows: Iterable[dict]) -> dict:
    """Group scene rows by logical-scene key. Returns {group_key: [rows]}."""
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(group_key(r), []).append(r)
    return grouped


def observed_sizes(rows: Iterable[dict]) -> set:
    """Set of canonical shot sizes present in a group."""
    out = set()
    for r in rows:
        s = canon_size(r.get("cameraAngle", "") or r.get("shotSize", ""))
        if s:
            out.add(s)
    return out


def infer_tier(rows: Iterable[dict]) -> str:
    """Back-compute tier from the sizes actually in a group. Used by the
    backfill so existing scenes get a sensible tier without manual input."""
    sizes = observed_sizes(rows)
    if not sizes:
        return DEFAULT_TIER
    if {"wide", "medium", "close", "insert"}.issubset(sizes):
        return "P0+P1+P2"
    if {"wide", "medium", "close"}.issubset(sizes):
        return "P0+P1"
    if "plate" in sizes and len(sizes) >= 4:
        return "full"
    return "P0"


def coverage_report(rows: list[dict]) -> dict:
    """Per-group coverage report: declared tier vs observed sizes vs missing."""
    grouped = group_scenes(rows)
    report = []
    for gkey, members in sorted(grouped.items(), key=lambda kv: (kv[1][0].get("orderIndex") or 0)):
        declared = validate_tier(members[0].get("coverageTier", ""))
        need = set(required_sizes(declared))
        have = observed_sizes(members)
        missing = sorted(need - have)
        report.append({
            "group": gkey,
            "declared_tier": declared,
            "inferred_tier": infer_tier(members),
            "shot_count": len(members),
            "sizes_have": sorted(have),
            "sizes_need": sorted(need),
            "missing": missing,
            "row_ids": [r.get("id") for r in members],
        })
    return {
        "groups": report,
        "total_groups": len(report),
        "groups_missing_coverage": sum(1 for g in report if g["missing"]),
    }
