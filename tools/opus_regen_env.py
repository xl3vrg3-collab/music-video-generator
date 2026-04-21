"""Regenerate a single existing env's sheet with a sharper Opus-aligned prompt.

Usage: python tools/opus_regen_env.py <env_id>
"""
from __future__ import annotations
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

OPUS_PROMPTS = {
    "Digital Skyline Hyperspeed": (
        "Wide establishing cinematic shot of a futuristic Tokyo-like megalopolis at DAWN, "
        "rebuilding itself at hyperspeed — high-end Makoto Shinkai anime realism, Your Name / "
        "Weathering With You dawn aesthetic. Warm gold sunrise key light from behind-left, cool "
        "cyan fill from rising glass towers. Skyscrapers in mid-ascent with motion-blurred "
        "light-streak trails rising vertically from their bases. Glass curtain walls snapping "
        "into place, partial kanji neon signage mid-ignition, holographic advertisements "
        "resolving in streaks. Long horizontal light streaks trailing each rising tower. Crisp "
        "sharp focus in immediate foreground, progressive motion blur receding to vanishing "
        "point. Clean dawn air with soft volumetric god-rays. Dawn sky: warm amber-gold at "
        "horizon fading to lavender-cyan at zenith, a few cirrus clouds catching first light. "
        "Soft bloom on every neon source. Mood: rebirth, resolve, transcendent. NO POV DASHBOARD, "
        "NO HUD TEXT, NO WINDSHIELD, NO VEHICLE INTERIOR, NO UI OVERLAYS, NO SIGNAGE TEXT. "
        "Pure exterior cinematic wide environment plate. High-end anime style, cel-shaded, "
        "painterly clouds, rich color palette, high detail, atmospheric depth."
    ),
}


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python tools/opus_regen_env.py <env_id_or_name>")
        sys.exit(1)
    target = sys.argv[1]

    _ap.set_active_slug("default")
    pos = PromptOS()
    envs = pos.get_environments()
    env = next((e for e in envs if e["id"] == target or e["name"] == target), None)
    if not env:
        print(f"env not found: {target}")
        sys.exit(1)

    name = env["name"]
    eid = env["id"]
    prompt = OPUS_PROMPTS.get(name)
    if not prompt:
        print(f"no opus prompt override for {name}")
        sys.exit(1)

    print(f"[REGEN] {name} ({eid})")
    print(f"  prompt len: {len(prompt)}")

    preview_dir = ROOT / "output/projects/default/prompt_os/env_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    paths = gemini_generate_image(
        prompt=prompt,
        resolution="1K",
        aspect_ratio="16:9",
        num_images=1,
    )
    elapsed = time.time() - t0

    if not paths or not os.path.isfile(paths[0]):
        print("FAL returned no image")
        sys.exit(1)

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
    pos.update_environment(eid, {"previewImage": sheet_url})

    print(f"  ✓ {elapsed:.1f}s → {out_path.name}")
    print(f"  url: {sheet_url}")


if __name__ == "__main__":
    main()
