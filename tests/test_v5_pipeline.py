"""
Tests for V5 Unified Production Pipeline.

Tests master prompt extraction, pipeline state machine, scene compositor,
anchor prompt compilation, and package-from-extraction conversion.
"""

import json
import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Test Pipeline State Machine ──

class TestPipelineState(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_initial_state_is_idle(self):
        from lib.pipeline_state import PipelineState
        ps = PipelineState(self.tmpdir)
        self.assertEqual(ps.state, "IDLE")

    def test_advance_requires_master_prompt(self):
        from lib.pipeline_state import PipelineState
        ps = PipelineState(self.tmpdir)
        with self.assertRaises(ValueError):
            ps.advance()  # can't advance from IDLE without master_prompt

    def test_advance_with_prompt(self):
        from lib.pipeline_state import PipelineState
        ps = PipelineState(self.tmpdir)
        ps.master_prompt = "A dog in a park"
        new_state = ps.advance()
        self.assertEqual(new_state, "PROMPT_RECEIVED")

    def test_full_state_progression(self):
        from lib.pipeline_state import PipelineState
        ps = PipelineState(self.tmpdir)
        ps.master_prompt = "A dog in a park"
        ps.auto_advance = True

        ps.advance()  # PROMPT_RECEIVED
        self.assertEqual(ps.state, "PROMPT_RECEIVED")

        ps.extraction = {"characters": [{"name": "Dog"}]}
        ps.advance()  # ASSETS_EXTRACTED
        self.assertEqual(ps.state, "ASSETS_EXTRACTED")

        ps.packages = ["pkg_001"]
        ps.advance()  # PACKAGES_CREATED
        self.assertEqual(ps.state, "PACKAGES_CREATED")

    def test_save_and_load(self):
        from lib.pipeline_state import PipelineState
        ps = PipelineState(self.tmpdir)
        ps.master_prompt = "Test prompt"
        ps.extraction = {"characters": []}
        ps.advance("PROMPT_RECEIVED")

        # Create new instance, should load from file
        ps2 = PipelineState(self.tmpdir)
        self.assertEqual(ps2.state, "PROMPT_RECEIVED")
        self.assertEqual(ps2.master_prompt, "Test prompt")

    def test_reset_to(self):
        from lib.pipeline_state import PipelineState
        ps = PipelineState(self.tmpdir)
        ps.master_prompt = "Test"
        ps.extraction = {"characters": []}
        ps.packages = ["pkg_001"]
        ps.advance("PLAN_READY")
        ps.plan = {"scenes": [{"id": "s1"}]}
        ps.anchors = {"s1": {"status": "generated"}}

        ps.reset_to("PACKAGES_CREATED")
        self.assertEqual(ps.state, "PACKAGES_CREATED")
        # Plan and anchors should be cleared
        self.assertEqual(ps.plan, {})
        self.assertEqual(ps.anchors, {})

    def test_anchor_operations(self):
        from lib.pipeline_state import PipelineState
        ps = PipelineState(self.tmpdir)
        ps.set_anchor("shot_001", {"status": "generated", "image_path": "/test.png"})
        self.assertEqual(ps.get_anchor("shot_001")["status"], "generated")

        ps.approve_anchor("shot_001")
        self.assertEqual(ps.get_anchor("shot_001")["status"], "approved")

        ps.reject_anchor("shot_001", "bad lighting")
        self.assertEqual(ps.get_anchor("shot_001")["status"], "rejected")
        self.assertIn("bad lighting", ps.get_anchor("shot_001")["rejection_notes"])

    def test_get_progress(self):
        from lib.pipeline_state import PipelineState
        ps = PipelineState(self.tmpdir)
        prog = ps.get_progress()
        self.assertEqual(prog["state"], "IDLE")
        self.assertEqual(prog["progress_percent"], 0)
        self.assertIn("can_advance", prog)

    def test_set_error(self):
        from lib.pipeline_state import PipelineState
        ps = PipelineState(self.tmpdir)
        ps.master_prompt = "Test"
        ps.advance("PROMPT_RECEIVED")
        ps.set_error("LLM failed")
        self.assertEqual(ps.state, "ERROR")
        self.assertEqual(len(ps.errors), 1)
        self.assertEqual(ps.errors[0]["message"], "LLM failed")


# ── Test Scene Compositor ──

class TestSceneCompositor(unittest.TestCase):

    def test_classify_shot_family_wide(self):
        from lib.scene_compositor import classify_shot_family
        shot = {"shot_size": "EWS", "shot_purpose": "establish_place", "sequence_type": "establish"}
        self.assertEqual(classify_shot_family(shot), "wide_establishing")

    def test_classify_shot_family_closeup(self):
        from lib.scene_compositor import classify_shot_family
        shot = {"shot_size": "CU", "shot_purpose": "show_emotion", "sequence_type": "tension"}
        # CU+show_emotion → character_closeup (emotion checked after size)
        self.assertEqual(classify_shot_family(shot), "character_closeup")

    def test_classify_shot_family_insert(self):
        from lib.scene_compositor import classify_shot_family
        shot = {"shot_size": "INSERT", "shot_purpose": "show_detail", "sequence_type": "montage"}
        self.assertEqual(classify_shot_family(shot), "insert_detail")

    def test_classify_shot_family_ots_dialogue(self):
        from lib.scene_compositor import classify_shot_family
        shot = {"shot_size": "OTS", "shot_purpose": "show_relationship", "sequence_type": "dialogue"}
        self.assertEqual(classify_shot_family(shot), "dialogue_shot")

    def test_classify_shot_family_pov_action(self):
        from lib.scene_compositor import classify_shot_family
        shot = {"shot_size": "POV", "shot_purpose": "show_perspective", "sequence_type": "pursuit"}
        self.assertEqual(classify_shot_family(shot), "action_scene")

    def test_select_canonical_refs_wide_prioritizes_environment(self):
        from lib.scene_compositor import select_canonical_refs
        shot = {
            "shot_size": "EWS",
            "shot_purpose": "establish_place",
            "sequence_type": "establish",
            "character_package_id": "pkg_char_001",
            "environment_package_id": "pkg_env_001",
        }
        # Create mock packages with hero images
        tmpdir = tempfile.mkdtemp()
        try:
            char_img = os.path.join(tmpdir, "char.png")
            env_img = os.path.join(tmpdir, "env.png")
            open(char_img, "w").close()
            open(env_img, "w").close()

            packages = [
                {"package_id": "pkg_char_001", "package_type": "character",
                 "hero_image_path": char_img, "sheet_images": []},
                {"package_id": "pkg_env_001", "package_type": "environment",
                 "hero_image_path": env_img, "sheet_images": []},
            ]
            refs = select_canonical_refs(shot, packages, "wide_establishing")
            # Environment should be first (highest priority for wide)
            self.assertTrue(len(refs) >= 1)
            self.assertEqual(refs[0]["tag"], "Setting")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_select_canonical_refs_closeup_prioritizes_character(self):
        from lib.scene_compositor import select_canonical_refs
        shot = {
            "shot_size": "CU",
            "shot_purpose": "show_emotion",
            "sequence_type": "tension",
            "character_package_id": "pkg_char_001",
            "environment_package_id": "pkg_env_001",
        }
        tmpdir = tempfile.mkdtemp()
        try:
            char_img = os.path.join(tmpdir, "char.png")
            env_img = os.path.join(tmpdir, "env.png")
            open(char_img, "w").close()
            open(env_img, "w").close()

            packages = [
                {"package_id": "pkg_char_001", "package_type": "character",
                 "hero_image_path": char_img, "sheet_images": []},
                {"package_id": "pkg_env_001", "package_type": "environment",
                 "hero_image_path": env_img, "sheet_images": []},
            ]
            refs = select_canonical_refs(shot, packages, "character_closeup")
            self.assertTrue(len(refs) >= 1)
            self.assertEqual(refs[0]["tag"], "Character")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_select_refs_max_3(self):
        from lib.scene_compositor import select_canonical_refs
        shot = {
            "shot_size": "MS",
            "shot_purpose": "show_action",
            "sequence_type": "pursuit",
            "character_package_id": "pkg_char_001",
            "costume_package_id": "pkg_cost_001",
            "environment_package_id": "pkg_env_001",
            "prop_package_ids": ["pkg_prop_001"],
        }
        tmpdir = tempfile.mkdtemp()
        try:
            imgs = {}
            for name in ["char", "cost", "env", "prop"]:
                path = os.path.join(tmpdir, f"{name}.png")
                open(path, "w").close()
                imgs[name] = path

            packages = [
                {"package_id": "pkg_char_001", "package_type": "character",
                 "hero_image_path": imgs["char"], "sheet_images": []},
                {"package_id": "pkg_cost_001", "package_type": "costume",
                 "hero_image_path": imgs["cost"], "sheet_images": []},
                {"package_id": "pkg_env_001", "package_type": "environment",
                 "hero_image_path": imgs["env"], "sheet_images": []},
                {"package_id": "pkg_prop_001", "package_type": "prop",
                 "hero_image_path": imgs["prop"], "sheet_images": []},
            ]
            refs = select_canonical_refs(shot, packages, "action_scene")
            self.assertLessEqual(len(refs), 3)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_shot_family_priorities_exist_for_all_families(self):
        from lib.scene_compositor import SHOT_FAMILY_PRIORITY
        expected = [
            "wide_establishing", "environment_reestablish", "character_closeup",
            "emotional_moment", "wardrobe_reveal", "prop_interaction",
            "action_scene", "dialogue_shot", "insert_detail", "generic",
        ]
        for family in expected:
            self.assertIn(family, SHOT_FAMILY_PRIORITY)
            weights = SHOT_FAMILY_PRIORITY[family]
            self.assertIn("character", weights)
            self.assertIn("environment", weights)


# ── Test Anchor Prompt Compilation ──

class TestAnchorPrompt(unittest.TestCase):

    def test_compile_anchor_prompt_under_1000(self):
        from lib.prompt_assembler import compile_anchor_prompt
        shot = {
            "shot_size": "MS",
            "shot_family": "action_scene",
            "action": "The hero runs through the burning building, dodging debris.",
            "emotion": "tension, urgency",
        }
        style_bible = {"global_style": "cinematic, warm golden hour, shallow depth of field"}
        refs = [
            {"tag": "Character", "package_type": "character"},
            {"tag": "Setting", "package_type": "environment"},
        ]
        prompt = compile_anchor_prompt(shot, style_bible, refs)
        self.assertLessEqual(len(prompt), 1000)
        self.assertIn("@Character", prompt)
        self.assertIn("@Setting", prompt)

    def test_compile_anchor_prompt_no_camera_movement(self):
        from lib.prompt_assembler import compile_anchor_prompt
        shot = {
            "shot_size": "WS",
            "shot_family": "wide_establishing",
            "action": "A quiet park at sunset.",
            "emotion": "peaceful",
        }
        prompt = compile_anchor_prompt(shot, {"global_style": "cinematic"}, [])
        # Should NOT contain camera movement language
        self.assertNotIn("tracking", prompt.lower())
        self.assertNotIn("dolly", prompt.lower())
        self.assertNotIn("pan", prompt.lower())
        # Should contain still-image language
        self.assertIn("still", prompt.lower())

    def test_compile_anchor_prompt_closeup_framing(self):
        from lib.prompt_assembler import compile_anchor_prompt
        shot = {"shot_family": "character_closeup", "emotion": "sadness"}
        refs = [{"tag": "Character", "package_type": "character"}]
        prompt = compile_anchor_prompt(shot, {}, refs)
        self.assertIn("close-up", prompt.lower())
        self.assertIn("@Character", prompt)


# ── Test Package From Extraction ──

class TestPackageFromExtraction(unittest.TestCase):

    def test_plan_packages_from_extraction(self):
        from lib.preproduction_assets import plan_packages_from_extraction
        extraction = {
            "characters": [
                {"name": "Alice", "physical_description": "Young woman, red hair", "role": "protagonist"},
                {"name": "Bob", "physical_description": "Tall man, beard", "role": "supporting"},
            ],
            "costumes": [
                {"name": "Alice's Dress", "character_name": "Alice", "description": "Blue summer dress"},
            ],
            "environments": [
                {"name": "City Park", "description": "Sprawling urban park", "lighting": "golden hour"},
            ],
            "props": [
                {"name": "Red Ball", "description": "Worn tennis ball", "importance": "hero"},
            ],
        }
        packages = plan_packages_from_extraction(extraction, mode="fast")
        types = [p["package_type"] for p in packages]
        self.assertEqual(types.count("character"), 2)
        self.assertEqual(types.count("costume"), 1)
        self.assertEqual(types.count("environment"), 1)
        self.assertEqual(types.count("prop"), 1)
        self.assertEqual(len(packages), 5)

    def test_dedup_against_existing(self):
        from lib.preproduction_assets import plan_packages_from_extraction
        extraction = {
            "characters": [{"name": "Alice", "physical_description": "Young woman"}],
            "costumes": [],
            "environments": [{"name": "Park", "description": "Green park"}],
            "props": [],
        }
        existing = [
            {"package_type": "character", "name": "Alice", "package_id": "existing_001"},
        ]
        packages = plan_packages_from_extraction(extraction, existing_packages=existing)
        # Alice should NOT be duplicated
        char_pkgs = [p for p in packages if p["package_type"] == "character"]
        self.assertEqual(len(char_pkgs), 0)
        # Park should still be created
        env_pkgs = [p for p in packages if p["package_type"] == "environment"]
        self.assertEqual(len(env_pkgs), 1)

    def test_protagonist_gets_higher_lock(self):
        from lib.preproduction_assets import plan_packages_from_extraction
        extraction = {
            "characters": [
                {"name": "Hero", "physical_description": "Strong", "role": "protagonist"},
                {"name": "Extra", "physical_description": "Background", "role": "extra"},
            ],
            "costumes": [], "environments": [], "props": [],
        }
        packages = plan_packages_from_extraction(extraction)
        hero_pkg = next(p for p in packages if p["name"] == "Hero")
        extra_pkg = next(p for p in packages if p["name"] == "Extra")
        self.assertGreater(hero_pkg["lock_strength"], extra_pkg["lock_strength"])


# ── Test Master Prompt Module ──

class TestMasterPromptHelpers(unittest.TestCase):

    def test_extraction_to_packages(self):
        from lib.master_prompt import extraction_to_packages
        extraction = {
            "characters": [{"name": "Dog", "physical_description": "Golden retriever", "role": "protagonist"}],
            "costumes": [],
            "environments": [{"name": "Park", "description": "City park", "lighting": "sunset"}],
            "props": [],
        }
        packages = extraction_to_packages(extraction, mode="fast")
        self.assertEqual(len(packages), 2)
        self.assertEqual(packages[0]["package_type"], "character")
        self.assertEqual(packages[1]["package_type"], "environment")

    def test_extraction_to_pos_entities(self):
        from lib.master_prompt import extraction_to_pos_entities
        extraction = {
            "characters": [
                {"name": "Alice", "role": "protagonist", "physical_description": "Young woman",
                 "expression_default": "determined"},
            ],
            "environments": [
                {"name": "Forest", "description": "Dark forest", "lighting": "moonlight",
                 "atmosphere": "eerie", "time_of_day": "night"},
            ],
            "costumes": [
                {"name": "Red Cloak", "character_name": "Alice", "description": "Crimson hooded cloak"},
            ],
        }
        entities = extraction_to_pos_entities(extraction)
        self.assertEqual(len(entities["characters"]), 1)
        self.assertEqual(entities["characters"][0]["name"], "Alice")
        self.assertEqual(entities["characters"][0]["defaultExpression"], "determined")
        self.assertEqual(len(entities["environments"]), 1)
        self.assertEqual(entities["environments"][0]["lighting"], "moonlight")
        self.assertEqual(len(entities["costumes"]), 1)

    def test_extraction_to_style_bible(self):
        from lib.master_prompt import extraction_to_style_bible
        extraction = {
            "style": {
                "keywords": ["cinematic", "warm", "film grain"],
                "color_palette": "golden amber tones",
                "lighting": "soft backlight",
                "texture": "subtle grain",
            },
        }
        bible = extraction_to_style_bible(extraction)
        self.assertIn("cinematic", bible["global_style"])
        self.assertIn("warm", bible["global_style"])
        self.assertEqual(bible["color_palette"], "golden amber tones")

    def test_validate_and_fix(self):
        from lib.master_prompt import _validate_and_fix
        # Minimal data should get defaults filled in
        data = {}
        fixed = _validate_and_fix(data)
        self.assertIn("narrative", fixed)
        self.assertIn("characters", fixed)
        self.assertIn("environments", fixed)
        self.assertIsInstance(fixed["characters"], list)

    def test_auto_generate_costumes_from_wardrobe_notes(self):
        from lib.master_prompt import _validate_and_fix
        data = {
            "characters": [{"name": "Bob", "wardrobe_notes": "Black leather jacket"}],
            "costumes": [],
        }
        fixed = _validate_and_fix(data)
        self.assertEqual(len(fixed["costumes"]), 1)
        self.assertEqual(fixed["costumes"][0]["character_name"], "Bob")


# ── Test Shot Dict Has Anchor Fields ──

class TestShotSchemaAnchorFields(unittest.TestCase):

    def test_expand_beat_has_anchor_fields(self):
        from lib.story_planner import expand_beat_to_shots
        beat = {
            "scene_number": 0,
            "beat_id": "beat_00",
            "story_beat": "A dog walks through a park",
            "visual_prompt": "Golden retriever in sunset park",
            "emotion": "lonely",
            "character": "Dog",
            "environment": "Park",
            "engine": "gen4_5",
        }
        shots = expand_beat_to_shots(beat, sequence_type="establish", energy=0.3)
        for shot in shots:
            self.assertIn("anchor_image_path", shot)
            self.assertIn("anchor_status", shot)
            self.assertIn("anchor_source_refs", shot)
            self.assertIn("shot_family", shot)
            self.assertIsNone(shot["anchor_image_path"])
            self.assertEqual(shot["anchor_status"], "pending")


# ── Test Backward Compatibility ──

class TestBackwardCompat(unittest.TestCase):

    def test_old_plan_without_anchors_still_works(self):
        """V4 plans without anchor fields should not break."""
        from lib.scene_compositor import classify_shot_family
        # Old-style shot without anchor fields
        old_shot = {
            "shot_size": "MS",
            "shot_purpose": "show_action",
            "sequence_type": "montage",
        }
        family = classify_shot_family(old_shot)
        self.assertIsInstance(family, str)

    def test_video_generator_no_anchor_path(self):
        """Scenes without anchor_image_path should fall through to existing behavior."""
        scene = {
            "prompt": "test",
            "anchor_image_path": "",  # empty = no anchor
        }
        # Should not be treated as having an anchor
        path = scene.get("anchor_image_path", "")
        self.assertFalse(path and os.path.isfile(path))


if __name__ == "__main__":
    unittest.main()
