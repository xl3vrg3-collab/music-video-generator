"""Generate sheets for ALL packages using gemini_2.5_flash.

Uses the existing preproduction_assets prompt system (HERO_PROMPTS).
Characters get 8-panel turnaround sheets.
Environments get multi-panel reference collages.
All via gemini_2.5_flash for consistency.
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

from lib.preproduction_assets import build_sheet_prompt, analyze_sheet_quality
from lib.video_generator import generate_sheet_image

MODEL = "gemini_2.5_flash"
PKGS_PATH = "output/preproduction/packages.json"

# Shared style injected into environment prompts for cohesion
ENV_STYLE_INJECT = (
    "Photorealistic 35mm film photography, autumn city park setting, "
    "golden hour warm light, amber and crimson foliage throughout."
)


def main():
    with open(PKGS_PATH) as f:
        data = json.load(f)
    packages = data["packages"]

    total = len(packages)
    done = 0
    failed = []

    for pkg in packages:
        done += 1
        pkg_id = pkg["package_id"]
        pkg_type = pkg["package_type"]
        name = pkg["name"]

        print(f"\n{'='*60}")
        print(f"[{done}/{total}] {pkg_type.upper()}: {name} ({pkg_id})")
        print(f"{'='*60}")

        # Build prompt using existing system
        prompt = build_sheet_prompt(pkg)

        # Inject shared environment style for cohesion
        if pkg_type == "environment":
            prompt = f"{ENV_STYLE_INJECT} {prompt}"
            prompt = prompt[:1000]

        print(f"  Prompt ({len(prompt)} chars): {prompt[:120]}...")

        # Ensure output directory
        pkg_dir = os.path.join("output", "preproduction", pkg_id)
        os.makedirs(pkg_dir, exist_ok=True)
        sheet_dest = os.path.join(pkg_dir, "sheet.png")

        # Delete old if exists
        if os.path.isfile(sheet_dest):
            os.remove(sheet_dest)
            print(f"  Deleted old sheet")

        try:
            tmp_path = generate_sheet_image(prompt, model=MODEL)
            if not tmp_path or not os.path.isfile(tmp_path):
                print(f"  FAILED: No image returned")
                failed.append(name)
                continue

            shutil.copy2(tmp_path, sheet_dest)
            abs_path = os.path.abspath(sheet_dest)
            print(f"  Generated: {abs_path}")

            # Update package
            pkg["hero_image_path"] = abs_path
            pkg["hero_view"] = "sheet"
            pkg["sheet_images"] = [{
                "view": "sheet",
                "label": f"{pkg_type.title()} Reference",
                "image_path": abs_path,
                "status": "generated",
                "seed": None,
                "prompt_used": prompt,
            }]
            pkg["generation_metadata"] = {
                "model": MODEL,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "type": "sheet",
            }
            pkg["status"] = "generated"
            pkg["prompt_used"] = prompt

            # QA analysis
            print(f"  Running QA...")
            qa = analyze_sheet_quality(abs_path, pkg)
            pkg["qa_result"] = qa
            passed = qa.get("pass", False)
            skipped = qa.get("skipped", False)
            if skipped:
                print(f"  QA: skipped (no vision API)")
            elif passed:
                scores = {k: qa[k] for k in ["panel_separation", "photorealism", "content_match", "multi_view", "quality"] if k in qa}
                print(f"  QA PASS: {scores}")
            else:
                issues = qa.get("issues", [])
                print(f"  QA FAIL: {issues}")

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            failed.append(name)

        time.sleep(1)

    # Save
    data["packages"] = packages
    with open(PKGS_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\n{'='*60}")
    print(f"DONE: {done - len(failed)}/{total} succeeded, {len(failed)} failed")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"Credits used: ~{(done - len(failed)) * 5}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
