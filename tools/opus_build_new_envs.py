"""Build the 3 new environment sheets that Opus introduced in plan_final.

Zero-G Particle Void, White Void Grid, Digital Skyline Hyperspeed — each
generated via fal-gemini-3.1-flash @ 1K/16:9 to match the existing env library.
"""
from __future__ import annotations
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import active_project as _ap
from lib.prompt_os import PromptOS
from lib.fal_client import gemini_generate_image

STYLE_SUFFIX = (
    "high-end anime style, Makoto Shinkai-inspired, cinematic anime realism, "
    "soft volumetric light, painterly clouds, rich color palette, cel-shaded, "
    "high detail, atmospheric depth, strong sense of place. No people, no characters."
)

NEW_ENVS = [
    {
        "name": "Zero-G Particle Void",
        "locationType": "abstract",
        "location": "A lavender-glowing zero-gravity void where ambient particles hang suspended mid-drift",
        "description": (
            "Vast cinematic anime void in high-end Makoto Shinkai anime realism. "
            "Soft omnidirectional lavender glow — no sun, no key light, only the ambient "
            "particles themselves acting as diffuse light sources. Thousands of silvery-violet "
            "motes suspended in zero gravity, drifting slowly in no particular direction, "
            "some catching faint pink and cyan highlights. The space has no floor, no horizon, "
            "no walls — pure volumetric fog at deep mid-distance fading into velvet violet "
            "black. Silent, weightless atmosphere. Subtle chromatic aberration on the closest "
            "particles. Mood: vulnerable surrender, coming apart, the quiet inside a storm. "
            "Think Interstellar tesseract by way of anime cosmic lyricism."
        ),
        "architecture": "none — abstract cosmic void",
        "lighting": "soft omnidirectional lavender, particles are the only light source",
        "atmosphere": "vulnerable, surrendering, suspended",
        "location_scene_anchor": "scene 6",
        "weather": "zero gravity, slow particle drift, silent",
        "timeOfDay": "none — void",
        "keyProps": "drifting silvery-violet particles, no surfaces, no horizon",
        "architectureNotes": "no architecture — pure volumetric space",
        "materialNotes": "particles, soft haze, velvet deep-violet negative space",
        "tags": ["anime", "shinkai", "void", "zero-g", "particles", "lavender", "abstract", "surreal", "cosmic"],
    },
    {
        "name": "White Void Grid",
        "locationType": "abstract",
        "location": "A cold white grid void pierced by warm gold sunrise light entering from screen-right",
        "description": (
            "Vast cinematic anime void in high-end Makoto Shinkai anime realism. "
            "Cold pure-white base palette with a precise perspective grid receding to a "
            "vanishing point — faintly luminous thin lines, like a Mondrian-meets-anime "
            "dreamspace. From screen-right and slightly above, a warm gold sunrise light "
            "pours in at a low angle, casting long warm streaks across the grid floor and "
            "turning the cold white into a gradient of cream→amber→deep gold toward the "
            "source. A handful of silvery-violet particles drift in the mid-ground, carried "
            "over from the prior scene. Volumetric god-rays catching the gold light. Sharp "
            "horizon where white meets gold. Mood: reverent pivot, choosing to break like "
            "sunrise, finding gold in the cold. Abstract sacred geometry."
        ),
        "architecture": "pure white void with receding perspective grid",
        "lighting": "cold white base + warm gold sunrise key from screen-right, growing across the scene",
        "atmosphere": "reverent, pivoting, sacred",
        "location_scene_anchor": "scene 7",
        "weather": "none — abstract grid space",
        "timeOfDay": "dawn-as-metaphor",
        "keyProps": "the gold light itself, the receding grid floor, a few lingering particles",
        "architectureNotes": "faint perspective grid lines, no walls, infinite floor, infinite ceiling",
        "materialNotes": "cold white, warm gold, thin grid lines, volumetric god-rays",
        "tags": ["anime", "shinkai", "void", "grid", "white", "gold", "sunrise", "abstract", "sacred", "dawn"],
    },
    {
        "name": "Digital Skyline Hyperspeed",
        "locationType": "urban",
        "location": "A futuristic Tokyo-like skyline assembling itself at hyperspeed, gold key + cyan fill",
        "description": (
            "Cinematic anime hyperspeed vista in high-end Makoto Shinkai anime realism — "
            "Your Name / Weathering With You dawn aesthetic. A futuristic Tokyo-like "
            "megalopolis is reassembling itself at impossible speed, towers rising in light-"
            "streak blurs, glass curtain walls snapping into place, neon signage lighting "
            "mid-ascent, overhead holographic advertisements resolving in streaks. The camera "
            "holds still while the city builds around it. Warm gold sunrise key from behind-"
            "left, cyan fill light from the rising towers, sunrise palette carried from scene "
            "7. Motion blur on every structural element, but crisp sharp focus on the immediate "
            "foreground. Long horizontal light streaks trailing each rising tower. Soft bloom "
            "on every light source. Volumetric fog catching dawn. Mood: rebirth, resolve, "
            "standing still while the world reassembles — reborn state, post-break."
        ),
        "architecture": "futuristic cyberpunk Tokyo — densely packed rising skyscrapers with neon faces",
        "lighting": "warm gold key from behind + cyan fill from rising towers, sunrise palette",
        "atmosphere": "transcendent, resolute, reborn",
        "location_scene_anchor": "scene 8",
        "weather": "streaks of light as towers assemble in hyperspeed, no rain, clean dawn air",
        "timeOfDay": "dawn",
        "keyProps": "rebuilding towers in motion blur, light-streak trails, assembling city grid, resolving holograms",
        "architectureNotes": "towers in mid-ascent, partial glass facades, neon mid-ignition, hand-painted kanji signage forming",
        "materialNotes": "motion-blurred steel and glass, crisp foreground, long horizontal light streaks, bloom on every light",
        "tags": ["anime", "shinkai", "dawn", "cyberpunk", "tokyo", "rebirth", "hyperspeed", "gold", "cyan", "resolve"],
    },
]


def _build_env_prompt(env: dict) -> str:
    parts = [
        env.get("description", ""),
        env.get("location", ""),
        env.get("architecture", ""),
        f"lighting: {env['lighting']}",
        f"atmosphere: {env['atmosphere']}",
        env.get("weather", ""),
        env.get("timeOfDay", ""),
        f"key props: {env['keyProps']}",
        f"architecture: {env.get('architectureNotes', '')}",
        f"materials: {env.get('materialNotes', '')}",
    ]
    desc = ", ".join(p for p in parts if p and p.strip() and not p.endswith(": "))
    return f"Wide establishing cinematic shot of {desc}. {STYLE_SUFFIX}"


def main() -> None:
    _ap.set_active_slug("default")
    pos = PromptOS()

    existing = {e.get("name"): e for e in pos.get_environments()}
    print("=" * 78)
    print(f"Existing envs: {len(existing)}")
    for n in NEW_ENVS:
        status = "EXISTS" if n["name"] in existing else "NEW"
        print(f"  [{status}] {n['name']}")
    print("=" * 78)
    print()

    preview_dir = ROOT / "output/projects/default/prompt_os/env_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)

    for env_spec in NEW_ENVS:
        name = env_spec["name"]
        if name in existing:
            print(f"[SKIP] {name} already exists ({existing[name]['id']})")
            continue

        print(f"[CREATE] {name}")
        record_in = dict(env_spec)
        record_in.pop("location_scene_anchor", None)  # not a schema field
        record_in["approvalState"] = "draft"
        rec = pos.create_environment(record_in)
        if "error" in rec:
            print(f"  ERROR: {rec['error']}")
            continue
        eid = rec["id"]
        print(f"  → id {eid}")

        prompt = _build_env_prompt(env_spec)
        print(f"  → prompt len {len(prompt)} chars")
        print(f"  → calling fal.ai Gemini (1K / 16:9)")
        t0 = time.time()
        try:
            paths = gemini_generate_image(
                prompt=prompt,
                resolution="1K",
                aspect_ratio="16:9",
                num_images=1,
            )
        except Exception as e:
            print(f"  FAL ERROR: {e}")
            continue
        elapsed = time.time() - t0

        if not paths or not os.path.isfile(paths[0]):
            print("  FAL returned no image")
            continue

        out_filename = f"{eid}_full_{int(time.time())}.png"
        out_path = preview_dir / out_filename
        shutil.move(paths[0], str(out_path))
        sheet_url = f"/output/projects/default/prompt_os/env_previews/{out_filename}"

        sheet_data = {
            "url": sheet_url,
            "type": "full",
            "model": "fal-gemini-3.1-flash",
            "generatedAt": time.time(),
            "addedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        pos.add_sheet_image("environment", eid, sheet_data)
        pos.update_environment(eid, {
            "previewImage": sheet_url,
            "approvalState": "generated",
        })
        print(f"  ✓ {elapsed:.1f}s → {out_path.name}")
        print()

    print("=" * 78)
    final = pos.get_environments()
    print(f"Total envs now: {len(final)}")
    for n in NEW_ENVS:
        match = next((e for e in final if e["name"] == n["name"]), None)
        if match:
            print(f"  {match['id']}  {match['name']:<32}  {match.get('approvalState','?'):<10}  {match.get('previewImage','')[:60]}")


if __name__ == "__main__":
    main()
