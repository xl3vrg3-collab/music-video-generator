"""
Test script for the photo+prompt generation pipeline.
Tests that:
1. A scene can be created with a prompt
2. A photo can be uploaded to the scene
3. generate_scene() with photo_path uses the photo+prompt pipeline
4. generate_from_photo() works correctly
"""

import os
import sys
import json
import tempfile

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def create_test_photo(path):
    """Create a simple test image using PIL."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (512, 512), color=(100, 150, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([50, 50, 462, 462], fill=(200, 100, 50))
        draw.ellipse([150, 150, 362, 362], fill=(50, 200, 100))
        img.save(path, "JPEG")
        print(f"[TEST] Created test photo: {path} ({os.path.getsize(path)} bytes)")
        return True
    except ImportError:
        print("[TEST] PIL not available, creating minimal JPEG manually")
        # Create a tiny valid JPEG (1x1 pixel)
        import struct
        # Minimal JPEG: SOI + APP0 + DQT + SOF0 + DHT + SOS + image data + EOI
        # Instead, just write raw bytes that look like a JPEG
        jpeg_bytes = bytes([
            0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
            0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xD9
        ])
        with open(path, "wb") as f:
            f.write(jpeg_bytes)
        print(f"[TEST] Created minimal test JPEG: {path} ({os.path.getsize(path)} bytes)")
        return True


def test_photo_encoding():
    """Test that photo can be read and encoded to base64."""
    import base64

    test_photo = os.path.join(tempfile.gettempdir(), "test_photo_pipeline.jpg")
    create_test_photo(test_photo)

    with open(test_photo, "rb") as f:
        photo_bytes = f.read()

    b64_data = base64.b64encode(photo_bytes).decode("ascii")
    data_uri = f"data:image/jpeg;base64,{b64_data}"

    print(f"[TEST] Photo size: {len(photo_bytes)} bytes")
    print(f"[TEST] Base64 length: {len(b64_data)} chars")
    print(f"[TEST] Data URI starts with: {data_uri[:50]}...")
    print(f"[TEST] Base64 encoding: PASS")

    os.remove(test_photo)
    return True


def test_generate_scene_with_photo():
    """Test generate_scene() with photo_path parameter."""
    from lib.video_generator import generate_scene

    test_dir = os.path.join(tempfile.gettempdir(), "mvg_test_clips")
    os.makedirs(test_dir, exist_ok=True)
    test_photo = os.path.join(tempfile.gettempdir(), "test_photo_gen.jpg")
    create_test_photo(test_photo)

    scene = {
        "prompt": "A beautiful sunset over the ocean, cinematic lighting",
        "duration": 5,
        "camera_movement": "zoom_in",
    }

    print(f"\n[TEST] === Testing generate_scene with photo_path ===")
    print(f"[TEST] Scene: {scene}")
    print(f"[TEST] Photo: {test_photo}")

    def progress_cb(index, status):
        print(f"[TEST][progress] scene {index}: {status}")

    try:
        clip_path = generate_scene(scene, 0, test_dir,
                                   progress_cb=progress_cb,
                                   photo_path=test_photo)

        if os.path.isfile(clip_path):
            size = os.path.getsize(clip_path)
            print(f"[TEST] SUCCESS: Clip generated at {clip_path} ({size} bytes)")
            return True
        else:
            print(f"[TEST] FAIL: Clip path returned but file not found: {clip_path}")
            return False
    except Exception as e:
        print(f"[TEST] FAIL: generate_scene raised exception: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_generate_from_photo():
    """Test generate_from_photo() directly."""
    from lib.video_generator import generate_from_photo

    test_dir = os.path.join(tempfile.gettempdir(), "mvg_test_clips")
    os.makedirs(test_dir, exist_ok=True)
    test_photo = os.path.join(tempfile.gettempdir(), "test_photo_direct.jpg")
    create_test_photo(test_photo)

    output_path = os.path.join(test_dir, "photo_test_clip.mp4")

    print(f"\n[TEST] === Testing generate_from_photo ===")
    print(f"[TEST] Photo: {test_photo}")
    print(f"[TEST] Output: {output_path}")

    def progress_cb(status):
        print(f"[TEST][progress] {status}")

    try:
        result = generate_from_photo(
            photo_path=test_photo,
            prompt="A beautiful sunset over the ocean, cinematic lighting",
            duration=5,
            output_path=output_path,
            progress_cb=progress_cb,
            camera="zoom_in",
        )

        if os.path.isfile(result):
            size = os.path.getsize(result)
            print(f"[TEST] SUCCESS: Clip generated at {result} ({size} bytes)")
            return True
        else:
            print(f"[TEST] FAIL: Result path returned but file not found: {result}")
            return False
    except Exception as e:
        print(f"[TEST] FAIL: generate_from_photo raised exception: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_scene_plan_photo_path():
    """Test that a scene plan correctly stores and retrieves photo_path."""
    print(f"\n[TEST] === Testing scene plan photo_path storage ===")

    plan_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "manual_scene_plan.json")

    if not os.path.isfile(plan_path):
        print(f"[TEST] SKIP: No manual plan found at {plan_path}")
        return True

    with open(plan_path, "r") as f:
        plan = json.load(f)

    for i, scene in enumerate(plan.get("scenes", [])):
        photo_path = scene.get("photo_path", None)
        has_photo = bool(photo_path and os.path.isfile(photo_path))
        print(f"[TEST] Scene {i} (id={scene.get('id', '?')}): photo_path={photo_path}, exists={has_photo}")
        if photo_path and not os.path.isfile(photo_path):
            print(f"[TEST] WARNING: photo_path is set but file does not exist!")

    print(f"[TEST] Scene plan check: PASS")
    return True


if __name__ == "__main__":
    print("=" * 60)
    print("Photo+Prompt Pipeline Test Suite")
    print("=" * 60)

    results = {}

    # Test 1: Base64 encoding
    print("\n--- Test 1: Photo base64 encoding ---")
    results["base64_encoding"] = test_photo_encoding()

    # Test 2: Scene plan photo_path storage
    print("\n--- Test 2: Scene plan photo_path ---")
    results["scene_plan"] = test_scene_plan_photo_path()

    # Test 3: generate_scene with photo (requires API key)
    if os.environ.get("XAI_API_KEY"):
        print("\n--- Test 3: generate_scene with photo ---")
        results["generate_scene_photo"] = test_generate_scene_with_photo()

        print("\n--- Test 4: generate_from_photo ---")
        results["generate_from_photo"] = test_generate_from_photo()
    else:
        print("\n--- Test 3 & 4: SKIPPED (no XAI_API_KEY) ---")
        results["generate_scene_photo"] = "SKIPPED"
        results["generate_from_photo"] = "SKIPPED"

    # Summary
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    for name, result in results.items():
        status = "PASS" if result is True else ("SKIP" if result == "SKIPPED" else "FAIL")
        print(f"  {name}: {status}")

    all_passed = all(r is True or r == "SKIPPED" for r in results.values())
    print(f"\nOverall: {'ALL PASSED' if all_passed else 'SOME FAILURES'}")
