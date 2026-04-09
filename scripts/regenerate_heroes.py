"""Regenerate ALL hero images as single clean shots via gemini_2.5_flash.

Fixes:
- Multi-panel turnaround grids -> single reference images
- Mixed engines -> all gemini_2.5_flash
- Missing Owen -> generate from scratch
- Inconsistent environment styles -> shared photorealistic style prefix
- Collar on wrong dog -> baked into Buddy's character prompt
"""
import json
import os
import shutil
import sys
import time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from lib.video_generator import generate_sheet_image

MODEL = "gemini_2.5_flash"
PKGS_PATH = "output/preproduction/packages.json"

# Consistent style for ALL environments — same park, same film stock, same color grade
ENV_STYLE = (
    "Photorealistic 35mm film photography, shallow depth of field, "
    "autumn city park, golden hour warm light, amber and crimson foliage, "
    "soft natural lighting, no people, no animals. "
    "Single wide establishing shot, cinematic composition."
)

# Character style — single clean reference, NOT a turnaround sheet
CHAR_STYLE = (
    "Photorealistic 35mm film photograph, single subject, "
    "3/4 front view, natural pose, clean neutral background, "
    "soft studio lighting, sharp focus. "
    "ONE image only, no collage, no multiple panels, no text."
)

# Prop style — single product shot
PROP_STYLE = (
    "Photorealistic product photography, single item, "
    "clean white background, soft studio lighting, sharp detail. "
    "ONE image only, no collage, no multiple panels, no text."
)

# ---- Prompts for each package ----

HERO_PROMPTS = {
    # CHARACTERS
    "pkg_char_c852b9c5": {
        "name": "Buddy",
        "prompt": (
            f"{CHAR_STYLE} "
            "Adult golden retriever, 3-4 years old, lush honey-gold coat, "
            "floppy ears, bright warm brown eyes, lean athletic build. "
            "Wearing a simple red nylon collar with a small round silver ID tag "
            "hanging from the D-ring. Standing naturally, friendly expression, "
            "tongue slightly out. Full body visible."
        ),
    },
    "pkg_char_0a0b6a7c": {
        "name": "Owen",
        "prompt": (
            f"{CHAR_STYLE} "
            "Male, early 60s, stocky warm build, warm brown skin, "
            "short silver-white hair, kind deep-set brown eyes, slight grey stubble, "
            "reading glasses perched on nose. Wearing a cozy brown autumn jacket "
            "over a dark flannel shirt, khaki pants. Gentle, fatherly expression. "
            "Full body visible, standing pose."
        ),
    },
    "pkg_char_67dab2d7": {
        "name": "Maya",
        "prompt": (
            f"{CHAR_STYLE} "
            "Female child, 7-8 years old, slight build, light tan skin, "
            "dark brown hair in two loose pigtails tied with small ribbons, "
            "bright hazel eyes, rosy cheeks. Wearing a blue denim pinafore dress "
            "over a cream striped t-shirt, white sneakers. "
            "Cheerful bright smile, standing with arms slightly out. Full body visible."
        ),
    },

    # ENVIRONMENTS — all share ENV_STYLE for cohesion
    "pkg_envi_a60066bf": {
        "name": "Park Entry Path",
        "prompt": (
            f"{ENV_STYLE} "
            "A wide gravel and packed-dirt path flanked by tall oak and maple trees, "
            "thick carpet of fallen amber and crimson leaves covering the ground, "
            "low wrought-iron fence at edges, dappled golden sunlight through canopy. "
            "The path stretches into the distance, inviting."
        ),
    },
    "pkg_envi_80af1e77": {
        "name": "Park Fountain Plaza",
        "prompt": (
            f"{ENV_STYLE} "
            "A circular stone plaza centered on a classic three-tiered stone fountain "
            "with water trickling gently, surrounded by wooden park benches, "
            "fallen autumn leaves scattered on the stone pavement. "
            "Tall trees with golden foliage frame the plaza. Warm late afternoon light."
        ),
    },
    "pkg_envi_63dba352": {
        "name": "Wooded Interior Path",
        "prompt": (
            f"{ENV_STYLE} "
            "A narrow dirt trail through a dense grove of maple and birch trees, "
            "leaves overhead forming a natural tunnel of orange and gold, "
            "dappled golden light filtering through the canopy. "
            "Quiet, intimate, peaceful autumn woodland path. "
            "Realistic trees, natural proportions, no fantasy elements."
        ),
    },
    "pkg_envi_773b7663": {
        "name": "Crowd Lawn",
        "prompt": (
            f"{ENV_STYLE} "
            "A broad open grass field in an autumn park, scattered mature trees "
            "with golden and amber foliage, fallen leaves drifting across the lawn. "
            "Picnic blankets and baskets visible on the grass, warm golden hour "
            "sunlight casting long shadows. Wide open sky."
        ),
    },
    "pkg_envi_82c911b9": {
        "name": "Reunion Bench",
        "prompt": (
            f"{ENV_STYLE} "
            "A single weathered wooden park bench beneath a massive old oak tree, "
            "thick blanket of golden fallen leaves on the ground, "
            "a warm glowing lamppost nearby. Deep golden hour sunlight "
            "raking horizontally through autumn trees. Quiet, intimate, tender mood."
        ),
    },

    # PROPS — only the ones actually referenced by scene anchors
    "pkg_prop_79931c8f": {
        "name": "Owen's Leash",
        "prompt": (
            f"{PROP_STYLE} "
            "A red nylon dog leash, approximately 6 feet long, with a silver metal clip. "
            "Coiled neatly on a surface. Simple, clean, well-used."
        ),
    },
    "pkg_prop_7f9074bd": {
        "name": "Stone Fountain",
        "prompt": (
            f"{PROP_STYLE} "
            "A classic three-tiered circular stone fountain, aged grey limestone "
            "with subtle green moss at the base, water trickling from each tier. "
            "Approximately 8 feet tall, photographed from 3/4 angle."
        ),
    },
    "pkg_prop_f60b170a": {
        "name": "Fallen Autumn Leaves",
        "prompt": (
            f"{PROP_STYLE} "
            "A scattered cluster of real dried maple and oak leaves in amber, "
            "crimson, and burnt orange tones. Photographed from above on a white surface."
        ),
    },
}

# ---- Main ----

def main():
    with open(PKGS_PATH) as f:
        data = json.load(f)
    packages = data["packages"]
    pkg_by_id = {p["package_id"]: p for p in packages}

    total = len(HERO_PROMPTS)
    done = 0
    failed = []

    for pkg_id, info in HERO_PROMPTS.items():
        done += 1
        name = info["name"]
        prompt = info["prompt"]

        print(f"\n{'='*60}")
        print(f"[{done}/{total}] Generating hero for {name} ({pkg_id})")
        print(f"{'='*60}")

        # Ensure output directory exists
        pkg_dir = f"output/preproduction/{pkg_id}"
        os.makedirs(pkg_dir, exist_ok=True)

        hero_dest = os.path.join(pkg_dir, "hero_gemini.png")

        try:
            tmp_path = generate_sheet_image(prompt, model=MODEL)
            if not tmp_path or not os.path.isfile(tmp_path):
                print(f"  FAILED: No image returned for {name}")
                failed.append(name)
                continue

            # Copy to package directory
            shutil.copy2(tmp_path, hero_dest)
            abs_hero = os.path.abspath(hero_dest)
            print(f"  SUCCESS: {abs_hero}")

            # Update package in memory
            if pkg_id in pkg_by_id:
                pkg_by_id[pkg_id]["hero_image_path"] = abs_hero
                pkg_by_id[pkg_id]["hero_view"] = "hero_gemini"
                pkg_by_id[pkg_id]["generation_metadata"] = {
                    "model": MODEL,
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "type": "single_hero",
                }
            else:
                print(f"  WARNING: {pkg_id} not in packages.json, skipping update")

        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append(name)

        # Small delay between calls to be nice to API
        time.sleep(1)

    # Save updated packages.json
    data["packages"] = packages
    with open(PKGS_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nPackages.json updated with new hero paths.")

    print(f"\n{'='*60}")
    print(f"DONE: {done - len(failed)}/{total} succeeded, {len(failed)} failed")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"{'='*60}")

    # Credit estimate
    print(f"\nEstimated credits used: {(done - len(failed)) * 5} (5 per gemini image)")


if __name__ == "__main__":
    main()
