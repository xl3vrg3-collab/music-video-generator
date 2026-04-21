"""Run Opus director against TB 'Lifestream Static' to get a fresh storyline."""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.opus_director import direct_story, direct_critique  # noqa: E402


def main() -> None:
    # ── Load TB assets ──────────────────────────────────────────────────────
    t = json.loads((ROOT / "output/projects/default/audio/timing.json").read_text(encoding="utf-8"))
    envs = json.loads((ROOT / "output/projects/default/prompt_os/environments.json").read_text(encoding="utf-8"))
    env_items = envs.get("environments", envs) if isinstance(envs, dict) else envs
    env_names = [
        e.get("name") for e in (env_items or [])
        if isinstance(e, dict) and e.get("name")
    ]

    # Song analysis in direct_story's shape
    sections = []
    for s in (t.get("sections") or []):
        sections.append({
            "label": s.get("label") or s.get("id") or "?",
            "start_s": round(s.get("start", 0), 2),
            "end_s": round(s.get("end", 0), 2),
        })
    lyrics_timed = []
    for L in (t.get("lyrics", {}).get("lines") or []):
        lyrics_timed.append({
            "start_s": L.get("start"),
            "end_s": L.get("end"),
            "text": L.get("text", ""),
        })
    song_analysis = {
        "bpm": (t.get("tempo") or {}).get("bpm"),
        "sections": sections,
        "lyrics_timed": lyrics_timed,
    }
    # Duration — use last beat or last section end
    beats = t.get("beats") or []
    duration_s = 272.2
    if beats:
        last = beats[-1]
        if isinstance(last, (int, float)):
            duration_s = float(last)
        elif isinstance(last, dict):
            duration_s = float(last.get("time", last.get("t", 272.2)))

    brief = (
        "Trillion Bear 'Lifestream Static' music video. Protagonist: TB, a small chibi-proportioned "
        "anime bear with glowing red-orange eyes, mauve muzzle, navy hoodie, blue beaded necklace, "
        "and a crescent moon emblem on his forehead. Shinkai anime-realism.\n\n"
        "Arc: TB wanders a violet rain-glass city under the hum of a 'lifestream static'. A signal "
        "calls to him through the noise. As he draws closer, the city shatters into memory fragments "
        "around him. He nearly dissolves in the chaos but finds a spark — he lets himself break like "
        "sunrise and rebuilds as light above the rebuilt rooftop. He accepts the breaking and the "
        "rebuilding both. End on him gazing at the aurora over the new city.\n\n"
        "Emotional registers to hit: melancholic wonder (verses), reverent awe (pre-chorus), "
        "chaotic grief (choruses), vulnerable acceptance (bridge), transcendent resolve (outro)."
    )

    print("=" * 78)
    print(f"[opus_director] calling direct_story()")
    print(f"  brief len:    {len(brief)}")
    print(f"  duration_s:   {duration_s:.1f}")
    print(f"  environments: {len(env_names)} — {', '.join(env_names)}")
    print(f"  sections:     {len(sections)}")
    print(f"  lyrics_timed: {len(lyrics_timed)} lines")
    print(f"  bpm:          {song_analysis['bpm']}")
    print("=" * 78)

    t0 = time.time()
    plan = direct_story(
        brief=brief,
        duration_s=duration_s,
        project="default",
        song_analysis=song_analysis,
        environments=env_names,
        thinking_budget=8000,
    )
    elapsed = time.time() - t0

    out_dir = ROOT / "output/pipeline/opus_storylines"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    plan_path = out_dir / f"plan_{stamp}.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    scenes = plan.get("scenes") or []
    total_shots = sum(len(sc.get("shots") or []) for sc in scenes)
    print()
    print(f"[done] {elapsed:.1f}s elapsed, {len(scenes)} scenes / {total_shots} shots → {plan_path}")
    print()
    print("=" * 78)
    print(f"TITLE:     {plan.get('title')}")
    print(f"LOGLINE:   {plan.get('logline')}")
    print(f"THEME:     {plan.get('theme')}")
    proto = plan.get("protagonist") or {}
    print(f"HERO:      {proto.get('name')} — need={proto.get('internal_need')} / want={proto.get('external_want')}")
    print("=" * 78)
    print()
    for sc in scenes:
        print(f"SCENE {sc.get('id','?')}  {sc.get('beat_name','?')}  |  "
              f"{sc.get('location','?')}  |  {sc.get('time_start',0):.1f}–{sc.get('time_end',0):.1f}s  "
              f"|  emotion={sc.get('emotion','?')}")
        if sc.get("dramatic_action"):
            print(f"  ACTION: {sc['dramatic_action']}")
        if sc.get("performance_arc"):
            print(f"  ARC:    {sc['performance_arc']}")
        if sc.get("lyric_anchor"):
            print(f"  LYRIC:  {sc['lyric_anchor']}")
        cont = sc.get("continuity_anchors") or {}
        if cont:
            print(f"  CONT:   light={cont.get('lighting','')[:40]} | wardrobe={cont.get('wardrobe','')[:30]} | eyeline={cont.get('eyeline_target','')[:30]}")
        shots = sc.get("shots") or []
        for sh in shots:
            print(
                f"    [{sh.get('id','?'):<4}] "
                f"{sh.get('shot_size','?'):<6} "
                f"{sh.get('duration_s','?'):<5}s "
                f"cam={(sh.get('camera','?'))[:22]:<22} "
                f"act={(sh.get('acting','?'))[:18]:<18} "
                f"t-in={(sh.get('transition_in','?'))[:11]:<11}"
            )
            if sh.get("continuity_in"):
                print(f"          ←  {sh['continuity_in'][:110]}")
        print()
    print()
    notes = plan.get("coverage_notes") or []
    if notes:
        print("COVERAGE NOTES:")
        for n in notes:
            print(f"  - {n}")

    # Opus self-critique pass
    print()
    print("=" * 78)
    print("[opus_director] running direct_critique() on the plan…")
    print("=" * 78)
    t0 = time.time()
    crit = direct_critique(
        scene_plan=plan,
        project="default",
        thinking_budget=4000,
    )
    crit_elapsed = time.time() - t0
    crit_path = out_dir / f"critique_{stamp}.json"
    crit_path.write_text(json.dumps(crit, indent=2), encoding="utf-8")
    print(f"  {crit_elapsed:.1f}s → {crit_path}")
    print(f"  verdict: {crit.get('verdict')}")
    print(f"  highest_impact_fix: {crit.get('highest_impact_fix')}")
    arc = crit.get("arc_health") or {}
    print(f"  arc_health: {arc}")
    issues = crit.get("issues") or []
    print(f"  issues: {len(issues)}")
    for i in issues[:8]:
        print(f"    [{i.get('severity','?')}] {i.get('scene_id','?')}: {i.get('problem','')} → {i.get('fix','')}")


if __name__ == "__main__":
    main()
