"""Generate 5 scene anchors from shot_plan.json using character sheets + environment as @Tag refs."""
import json
import os
import shutil
import sys
import time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from lib.video_generator import _runway_generate_scene_image, _ENGINE_RATIO_MAP

MODEL = "gemini_2.5_flash"
RATIO = _ENGINE_RATIO_MAP.get(MODEL, "1344:768")
PLAN_PATH = "output/pipeline/shot_plan.json"
PKGS_PATH = "output/preproduction/packages.json"
ANCHOR_DIR = "output/pipeline/anchors"

os.makedirs(ANCHOR_DIR, exist_ok=True)

# Load
with open(PLAN_PATH) as f:
    plan = json.load(f)
with open(PKGS_PATH) as f:
    pkg_data = json.load(f)

pkg_index = {p["package_id"]: p for p in pkg_data["packages"]}
env_pkg = pkg_index[plan["environment_pkg"]]
env_hero = env_pkg["hero_image_path"]

print(f"Environment: {env_pkg['name']} -> {env_hero}")
print(f"Model: {MODEL}, Ratio: {RATIO}")
print(f"Shots: {len(plan['shots'])}")
print()

failed = []

for i, shot in enumerate(plan["shots"]):
    shot_id = shot["shot_id"]
    print(f"{'='*60}")
    print(f"[{i+1}/{len(plan['shots'])}] {shot_id} — {shot['moment'][:60]}")
    print(f"{'='*60}")

    # Build reference photos: Character + Setting + optional 2nd character
    ref_photos = []

    # Primary character is always first
    primary_pkg_id = shot["character_pkgs"][0]
    primary_pkg = pkg_index[primary_pkg_id]
    ref_photos.append({"path": primary_pkg["hero_image_path"], "tag": "Character"})
    print(f"  @Character = {primary_pkg['name']}")

    # Environment always second
    ref_photos.append({"path": env_hero, "tag": "Setting"})
    print(f"  @Setting = {env_pkg['name']}")

    # Second character in 3rd slot if present
    if len(shot["character_pkgs"]) > 1:
        second_pkg_id = shot["character_pkgs"][1]
        second_pkg = pkg_index[second_pkg_id]
        ref_photos.append({"path": second_pkg["hero_image_path"], "tag": "PropRef"})
        print(f"  @PropRef = {second_pkg['name']} (2nd character)")

    # Prompt
    prompt = shot["anchor_prompt"]
    print(f"  Prompt ({len(prompt)} chars): {prompt[:100]}...")

    # Output path
    anchor_path = os.path.join(ANCHOR_DIR, f"{shot_id}.png")
    if os.path.isfile(anchor_path):
        os.remove(anchor_path)

    try:
        tmp_path = _runway_generate_scene_image(
            prompt=prompt,
            reference_photos=ref_photos,
            ratio=RATIO,
            model=MODEL,
        )
        if not tmp_path or not os.path.isfile(tmp_path):
            print(f"  FAILED: No image returned")
            failed.append(shot_id)
            continue

        shutil.copy2(tmp_path, anchor_path)
        abs_path = os.path.abspath(anchor_path)
        shot["anchor_image_path"] = abs_path
        shot["status"] = "generated"
        print(f"  SUCCESS: {abs_path}")

    except Exception as e:
        print(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()
        failed.append(shot_id)

    time.sleep(1)

# Save updated plan
with open(PLAN_PATH, "w") as f:
    json.dump(plan, f, indent=2)

print(f"\n{'='*60}")
print(f"DONE: {len(plan['shots']) - len(failed)}/{len(plan['shots'])} anchors generated")
if failed:
    print(f"Failed: {', '.join(failed)}")
print(f"Credits used: ~{(len(plan['shots']) - len(failed)) * 5}")
print(f"{'='*60}")
