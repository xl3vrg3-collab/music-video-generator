"""
Preproduction + Taste Profile Tests — validate asset packages, taste profiles,
shot binding, and backward compatibility.

Run with:  pytest tests/test_preproduction.py -v
"""

import math
import sys
import os
import json
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from lib.preproduction_assets import (
    create_package, build_sheet_prompt, get_sheet_plan, update_sheet_image,
    select_hero_ref, approve_package, reject_package,
    validate_package_completeness, validate_preproduction,
    bind_shots_to_packages, get_shot_package_notes,
    plan_packages_from_beats, generate_preproduction_report,
    PreproductionStore, SHEET_VIEWS, REQUIRED_VIEWS,
)
from lib.taste_profile import (
    create_profile, get_quiz_pairs, process_quiz_answers, update_from_sliders,
    record_behavior, blend_profiles, generate_taste_summary,
    taste_to_prompt_modifiers, taste_to_pacing_bias, TasteStore,
    TASTE_DIMENSIONS, QUIZ_PAIRS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def char_pkg():
    return create_package("character", "Luna",
                          description="Dark-haired woman with violet eyes",
                          mode="production",
                          must_keep=["violet eyes", "dark hair"],
                          avoid=["hat"])


@pytest.fixture
def env_pkg():
    return create_package("environment", "Dark Alley",
                          description="Narrow urban alley, wet pavement, neon signs",
                          mode="fast")


@pytest.fixture
def shot_list():
    return [
        {"shot_id": "s0", "subject": "Luna", "characterName": "Luna",
         "environmentName": "Dark Alley", "shot_size": "WS",
         "character_package_id": None, "environment_package_id": None,
         "costume_package_id": None, "prop_package_ids": []},
        {"shot_id": "s1", "subject": "Luna", "characterName": "Luna",
         "environmentName": "Dark Alley", "shot_size": "CU",
         "character_package_id": None, "environment_package_id": None,
         "costume_package_id": None, "prop_package_ids": []},
        {"shot_id": "s2", "subject": "Max", "characterName": "Max",
         "environmentName": "Rooftop", "shot_size": "MS",
         "character_package_id": None, "environment_package_id": None,
         "costume_package_id": None, "prop_package_ids": []},
    ]


# ---------------------------------------------------------------------------
# Preproduction Package Tests
# ---------------------------------------------------------------------------

class TestPreproductionPackages:

    def test_create_character_package(self, char_pkg):
        assert char_pkg["package_type"] == "character"
        assert char_pkg["name"] == "Luna"
        assert char_pkg["status"] == "draft"
        assert len(char_pkg["sheet_images"]) == 1  # single composite sheet
        assert char_pkg["sheet_images"][0]["view"] == "sheet"
        assert "violet eyes" in char_pkg["must_keep"]
        assert "hat" in char_pkg["avoid"]

    def test_create_fast_vs_production(self):
        fast = create_package("character", "Test", mode="fast")
        prod = create_package("character", "Test", mode="production")
        # Both have 1 sheet now (composite image)
        assert len(fast["sheet_images"]) == 1
        assert len(prod["sheet_images"]) == 1

    def test_all_package_types(self):
        for t in ("character", "costume", "environment", "prop"):
            pkg = create_package(t, f"Test {t}", mode="fast")
            assert pkg["package_type"] == t
            assert len(pkg["sheet_images"]) >= 1

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError):
            create_package("invalid_type", "Test")

    def test_sheet_prompt_under_1000(self, char_pkg):
        prompt = build_sheet_prompt(char_pkg)
        assert len(prompt) <= 1000, f"Prompt is {len(prompt)} chars"

    def test_sheet_prompt_contains_description(self, char_pkg):
        prompt = build_sheet_prompt(char_pkg)
        assert "violet eyes" in prompt.lower() or "dark-haired" in prompt.lower()
        assert "portrait" in prompt.lower() or "reference" in prompt.lower()

    def test_update_sheet_image(self, char_pkg):
        pkg = update_sheet_image(char_pkg, "sheet", "/fake/sheet.png",
                                 seed=42, prompt_used="test prompt")
        img = next(i for i in pkg["sheet_images"] if i["view"] == "sheet")
        assert img["image_path"] == "/fake/sheet.png"
        assert img["status"] == "generated"
        assert img["seed"] == 42
        # Auto hero selection
        assert pkg["hero_image_path"] == "/fake/sheet.png"
        assert pkg["hero_view"] == "sheet"

    def test_select_hero_ref(self, char_pkg):
        pkg = update_sheet_image(char_pkg, "sheet", "/a.png")
        assert pkg["hero_image_path"] == "/a.png"
        assert pkg["hero_view"] == "sheet"

    def test_approve_requires_hero(self, char_pkg):
        with pytest.raises(ValueError, match="no hero ref"):
            approve_package(char_pkg)

    def test_approve_with_hero(self, char_pkg):
        pkg = update_sheet_image(char_pkg, "sheet", "/a.png")
        pkg = approve_package(pkg)
        assert pkg["status"] == "approved"

    def test_reject_package(self, char_pkg):
        pkg = reject_package(char_pkg, "Doesn't match vision")
        assert pkg["status"] == "rejected"
        assert any("Rejected" in n for n in pkg["canonical_notes"])


class TestPreproductionValidation:

    def test_package_completeness_missing_views(self, char_pkg):
        result = validate_package_completeness(char_pkg)
        assert not result["complete"]
        assert "sheet" in result["missing_views"]

    def test_package_completeness_all_generated(self, char_pkg):
        for view in REQUIRED_VIEWS["character"]:
            char_pkg = update_sheet_image(char_pkg, view, f"/fake/{view}.png")
        result = validate_package_completeness(char_pkg)
        assert result["complete"]
        assert len(result["missing_views"]) == 0

    def test_validate_preproduction_fast_warns(self, shot_list):
        """Fast mode warns but doesn't block on missing packages."""
        result = validate_preproduction([], shot_list, mode="fast")
        assert result["ready"]  # fast mode doesn't block
        assert len(result["warnings"]) > 0

    def test_validate_preproduction_production_blocks(self, shot_list):
        """Production mode blocks on missing main character package."""
        result = validate_preproduction([], shot_list, mode="production")
        assert not result["ready"]
        assert any("Luna" in e for e in result["errors"])


class TestShotBinding:

    def test_bind_character(self, shot_list, char_pkg):
        bind_shots_to_packages(shot_list, [char_pkg])
        luna_shots = [s for s in shot_list if s["subject"] == "Luna"]
        assert all(s["character_package_id"] == char_pkg["package_id"] for s in luna_shots)
        # Max should not be bound
        max_shots = [s for s in shot_list if s["subject"] == "Max"]
        assert all(s["character_package_id"] is None for s in max_shots)

    def test_bind_environment(self, shot_list, env_pkg):
        bind_shots_to_packages(shot_list, [env_pkg])
        alley_shots = [s for s in shot_list if s["environmentName"] == "Dark Alley"]
        assert all(s["environment_package_id"] == env_pkg["package_id"] for s in alley_shots)

    def test_package_notes_collected(self, char_pkg):
        pkg = update_sheet_image(char_pkg, "sheet", "/a.png")
        pkg = approve_package(pkg)
        shot = {"character_package_id": pkg["package_id"]}
        notes = get_shot_package_notes(shot, [pkg])
        assert "violet eyes" in notes["must_keep"]
        assert "hat" in notes["avoid"]


class TestPreproductionStore:

    def test_save_and_load(self, tmp_dir, char_pkg):
        store = PreproductionStore(tmp_dir)
        store.save_package(char_pkg)
        loaded = store.get_by_id(char_pkg["package_id"])
        assert loaded is not None
        assert loaded["name"] == "Luna"

    def test_get_by_type(self, tmp_dir, char_pkg, env_pkg):
        store = PreproductionStore(tmp_dir)
        store.save_package(char_pkg)
        store.save_package(env_pkg)
        chars = store.get_by_type("character")
        assert len(chars) == 1
        envs = store.get_by_type("environment")
        assert len(envs) == 1

    def test_remove_package(self, tmp_dir, char_pkg):
        store = PreproductionStore(tmp_dir)
        store.save_package(char_pkg)
        store.remove_package(char_pkg["package_id"])
        assert store.get_by_id(char_pkg["package_id"]) is None

    def test_mode_persistence(self, tmp_dir):
        store = PreproductionStore(tmp_dir)
        store.set_mode("production")
        assert store.get_mode() == "production"


class TestPlanPackages:

    def test_plan_from_characters(self):
        chars = [{"name": "Luna", "description": "Dark-haired woman"}]
        envs = [{"name": "Forest", "description": "Dense pine forest"}]
        pkgs = plan_packages_from_beats([], chars, envs, mode="fast")
        names = {p["name"] for p in pkgs}
        assert "Luna" in names
        assert "Forest" in names

    def test_no_duplicate_packages(self):
        chars = [{"name": "Luna"}]
        existing = [create_package("character", "Luna")]
        pkgs = plan_packages_from_beats([], chars, [], mode="fast",
                                        existing_packages=existing)
        assert len(pkgs) == 0  # already exists


class TestPreproductionReport:

    def test_report_structure(self, char_pkg, shot_list):
        report = generate_preproduction_report([char_pkg], shot_list)
        assert report["total_packages"] == 1
        assert "character" in report["by_type"]
        assert len(report["packages"]) == 1
        assert "name" in report["packages"][0]


# ---------------------------------------------------------------------------
# Taste Profile Tests
# ---------------------------------------------------------------------------

class TestTasteProfile:

    def test_create_profile(self):
        p = create_profile("Test", is_overall=True)
        assert p["is_overall"]
        assert all(d in p["dimensions"] for d in TASTE_DIMENSIONS)
        assert all(p["dimensions"][d] == 0.0 for d in TASTE_DIMENSIONS)

    def test_quiz_pairs_complete(self):
        pairs = get_quiz_pairs()
        assert len(pairs) >= 10
        dims_covered = {p["dimension"] for p in pairs}
        assert len(dims_covered) >= 8  # at least 8 dimensions covered

    def test_process_quiz_all_a(self):
        p = create_profile("Test")
        answers = [{"question_id": q["id"], "choice": "a"} for q in QUIZ_PAIRS[:10]]
        p = process_quiz_answers(p, answers)
        # All "a" choices lean negative
        negative_dims = sum(1 for d in TASTE_DIMENSIONS if p["dimensions"][d] < 0)
        assert negative_dims >= 5, "Most dimensions should be negative with all 'a' choices"

    def test_process_quiz_all_b(self):
        p = create_profile("Test")
        answers = [{"question_id": q["id"], "choice": "b"} for q in QUIZ_PAIRS[:10]]
        p = process_quiz_answers(p, answers)
        positive_dims = sum(1 for d in TASTE_DIMENSIONS if p["dimensions"][d] > 0)
        assert positive_dims >= 5

    def test_quiz_skip_neutral(self):
        p = create_profile("Test")
        answers = [{"question_id": q["id"], "choice": "skip"} for q in QUIZ_PAIRS[:10]]
        p = process_quiz_answers(p, answers)
        # All skipped = all neutral
        assert all(p["dimensions"][d] == 0.0 for d in TASTE_DIMENSIONS)

    def test_slider_update(self):
        p = create_profile("Test")
        p = update_from_sliders(p, {"lighting": -0.7, "texture": 0.9})
        assert p["dimensions"]["lighting"] == -0.7
        assert p["dimensions"]["texture"] == 0.9
        assert p["confidence"]["lighting"] >= 0.7

    def test_slider_clamp(self):
        p = create_profile("Test")
        p = update_from_sliders(p, {"lighting": -5.0, "texture": 99.0})
        assert p["dimensions"]["lighting"] == -1.0
        assert p["dimensions"]["texture"] == 1.0


class TestTasteBlending:

    def test_blend_overall_only(self):
        overall = create_profile("Overall", is_overall=True)
        overall = update_from_sliders(overall, {"lighting": -0.8, "texture": 0.5})
        result = blend_profiles(overall, None)
        assert result["dimensions"]["lighting"] == -0.8

    def test_blend_project_override(self):
        overall = create_profile("Overall", is_overall=True)
        overall = update_from_sliders(overall, {"lighting": -0.8})
        project = create_profile("Project")
        project = update_from_sliders(project, {"lighting": 0.6})
        project["inherit_overall"] = False
        result = blend_profiles(overall, project)
        # Project override should win
        assert result["dimensions"]["lighting"] == 0.6

    def test_blend_empty_neutral(self):
        result = blend_profiles(None, None)
        assert all(result["dimensions"][d] == 0.0 for d in TASTE_DIMENSIONS)

    def test_summary_generation(self):
        p = create_profile("Test")
        p = update_from_sliders(p, {"lighting": -0.8, "texture": 0.8, "realism": -0.6})
        summary = generate_taste_summary(p)
        assert "warm" in summary.lower()
        assert "gritty" in summary.lower()


class TestTasteIntegration:

    def test_prompt_modifiers(self):
        blended = {
            "dimensions": {"lighting": -0.8, "texture": 0.8, "realism": -0.6,
                           "composition": 0.0, "density": 0.0, "tone": 0.0,
                           "focus": 0.0, "pacing": 0.0, "wardrobe": 0.0, "framing": 0.0},
            "confidence": {d: 0.8 for d in TASTE_DIMENSIONS},
        }
        mods = taste_to_prompt_modifiers(blended)
        assert "warm" in mods.get("lighting", "").lower()
        assert "grain" in mods.get("texture", "").lower() or "grit" in mods.get("texture", "").lower()

    def test_pacing_bias(self):
        blended = {
            "dimensions": {"pacing": 0.8, "framing": -0.6,
                           **{d: 0.0 for d in TASTE_DIMENSIONS if d not in ("pacing", "framing")}},
        }
        bias = taste_to_pacing_bias(blended)
        assert bias["duration_scale"] < 1.0  # aggressive = shorter
        assert bias["cut_density"] > 1.0  # aggressive = more cuts
        assert bias["closeup_bias"] > 0  # intimate framing = more close-ups


class TestTasteStore:

    def test_save_and_load_overall(self, tmp_dir):
        store = TasteStore(tmp_dir)
        p = create_profile("My Style", is_overall=True)
        p = update_from_sliders(p, {"lighting": -0.5})
        store.save_overall(p)
        loaded = store.get_overall()
        assert loaded is not None
        assert loaded["dimensions"]["lighting"] == -0.5

    def test_project_profile(self, tmp_dir):
        store = TasteStore(tmp_dir)
        p = create_profile("Project Style", project_id="proj_001")
        p = update_from_sliders(p, {"texture": 0.9})
        store.save_project_profile("proj_001", p)
        loaded = store.get_project_profile("proj_001")
        assert loaded["dimensions"]["texture"] == 0.9

    def test_blended_from_store(self, tmp_dir):
        store = TasteStore(tmp_dir)
        overall = create_profile("Overall", is_overall=True)
        overall = update_from_sliders(overall, {"lighting": -0.8})
        store.save_overall(overall)
        blended = store.get_blended()
        assert blended["dimensions"]["lighting"] == -0.8


# ---------------------------------------------------------------------------
# Backward Compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompat:

    def test_v3_plan_still_works(self):
        """Old V3 plan without preproduction/taste fields should still score."""
        from lib.quality_metrics import score_plan_quality
        v3_plan = {
            "plan_version": 3,
            "scenes": [
                {"shot_id": f"s{i}", "shot_size": "MS", "target_duration": 4.0,
                 "characterId": "c1", "screen_direction": "L2R"}
                for i in range(6)
            ],
        }
        result = score_plan_quality(v3_plan)
        assert "total" in result
        assert result["total"] >= 0

    def test_shots_without_packages_work(self):
        """Shots with no package bindings should not crash binding."""
        shots = [{"shot_id": "s0", "subject": "Unknown"}]
        result = bind_shots_to_packages(shots, [])
        assert result[0].get("character_package_id") is None
