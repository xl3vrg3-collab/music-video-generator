"""One-off: generate 4 Gemini hero background candidates for LUMN welcome page.

Saves to public/hero_candidates/ with human-readable names so we can compare them
side-by-side. Aspect 16:9, 1K resolution (upsize later if one is picked).
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.fal_client import gemini_generate_image

DEST = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public", "hero_candidates"))
os.makedirs(DEST, exist_ok=True)

CANDIDATES = [
    (
        "01_cold_architectural_figure",
        "A single tiny human silhouette, back to camera, standing at the edge of a "
        "vast brutalist concrete monolith. Pre-dawn cold blue light. Massive scale "
        "contrast: figure is 5% of frame, the rest is geometry and deep negative space "
        "above. Fog at the base. Hard shadows. Cinematic 16:9 establishing shot in the "
        "style of Denis Villeneuve's Blade Runner 2049 and Dune. No text, no logos, "
        "no foreground clutter. Photorealistic, high dynamic range, cold color grade "
        "(deep blacks, desaturated blue highlights). Wide lens, architectural composition."
    ),
    (
        "02_cathedral_of_light",
        "Interior of a massive dark empty industrial cathedral. Parallel shafts of "
        "cold volumetric god-light cut vertically through thick atmospheric haze, "
        "pouring from tall narrow windows. No people, no furniture. Pure atmosphere. "
        "The space feels sacred and mechanical at once. Deep blacks dominate the frame. "
        "Dust particles catch in the light beams. Cinematic 16:9. In the style of "
        "Stanley Kubrick's 2001, Roger Deakins lighting, and Hiroshi Sugimoto theater "
        "interiors. Photorealistic, cold palette (black, charcoal, pale desaturated "
        "blue-white light), architectural minimalism, massive negative space for titles."
    ),
    (
        "03_noir_cityscape_rain",
        "High aerial view of a night city, looking down at a grid of wet empty streets "
        "reflecting cold white and pale amber streetlights. Low dark clouds moving "
        "across the top of frame. Light rain falling through the amber glow. No cars, "
        "no people, no visible signage. Deep black sky fills the upper two-thirds for "
        "text. Cinematic 16:9, photorealistic, in the style of Michael Mann's Collateral, "
        "Blade Runner 2049, and Christopher Nolan's Gotham aerials. Cold desaturated "
        "palette, heavy blacks, wet asphalt reflections, moody atmospheric noir."
    ),
    (
        "04_sugimoto_monolith_horizon",
        "A minimalist fog landscape. A single horizon line divides the frame: charcoal "
        "sky above, soft grey fog below. One sharp vertical dark silhouette breaks the "
        "horizon asymmetrically on the right third — a distant monolith, radio tower, "
        "or architectural column. No other features. No people. Extreme minimalism. "
        "Cinematic 16:9, photorealistic, in the style of Hiroshi Sugimoto seascapes and "
        "Michael Kenna long-exposure photography. Pure tonal study: black, charcoal, "
        "soft pale grey. Massive calm negative space. Gallery-wall composition, patient "
        "and meditative."
    ),
]


def main():
    results = []
    for slug, prompt in CANDIDATES:
        print(f"\n[HERO] Generating {slug} ...")
        try:
            paths = gemini_generate_image(
                prompt=prompt,
                resolution="1K",
                aspect_ratio="16:9",
                num_images=1,
            )
            if not paths:
                print(f"[HERO] {slug}: no image returned")
                results.append((slug, None))
                continue
            src = paths[0]
            dest = os.path.join(DEST, f"{slug}.png")
            shutil.move(src, dest)
            print(f"[HERO] {slug}: saved -> {dest}")
            results.append((slug, dest))
        except Exception as e:
            print(f"[HERO] {slug} FAILED: {e}")
            results.append((slug, None))

    print("\n=== SUMMARY ===")
    for slug, path in results:
        status = path if path else "FAILED"
        print(f"  {slug}: {status}")


if __name__ == "__main__":
    main()
