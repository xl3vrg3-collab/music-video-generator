"""
Unit tests for V6 core library modules:

  - lib.v6_prompt_assembler  (entity detection, injection, clip-target strip)
  - lib.kling_prompt_linter  (all rules)
  - lib.identity_gate        (lock/unlock/auto-lock single-vs-multi-subject)
  - lib.moderation           (CSAM, NSFW, public-figure blocks, copyright warn)

These use stdlib unittest so they run without pip deps. Each test module
that touches persistent state writes to a tempdir and resets env vars.

Run:
  python -m unittest tests/test_v6_core.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

# Make project root importable
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)


# ---------------------------------------------------------------------------
# v6_prompt_assembler
# ---------------------------------------------------------------------------

BUDDY_PKG = {
    "package_id": "pkg_char_buddy",
    "package_type": "character",
    "name": "Buddy",
    "description": "Adult golden retriever, honey-gold fur, pendant ears, red collar with silver tag",
    "must_keep": ["golden retriever", "red collar", "silver tag", "pendant ears"],
    "avoid": ["pointed ears", "dark fur", "blue collar"],
    "lock_strength": "hard",
    "canonical_notes": "Always warm amber eyes",
    "hero_image_path": "",
}

OWEN_PKG = {
    "package_id": "pkg_char_owen",
    "package_type": "character",
    "name": "Owen",
    "description": "Early 30s man, short brown hair, navy jacket",
    "must_keep": ["brown hair", "navy jacket"],
    "avoid": [],
    "hero_image_path": "",
}

PARK_PKG = {
    "package_id": "pkg_env_park",
    "package_type": "environment",
    "name": "Autumn Park",
    "description": "Wooded urban park with oak trees and fallen leaves",
    "must_keep": ["golden hour", "fallen leaves"],
    "avoid": [],
    "hero_image_path": "",
}


class TestPromptAssembler(unittest.TestCase):
    def setUp(self):
        from lib import v6_prompt_assembler as mod
        self.mod = mod
        self.tmp = tempfile.mkdtemp()
        self.pkg_path = os.path.join(self.tmp, "packages.json")
        with open(self.pkg_path, "w", encoding="utf-8") as f:
            json.dump({"packages": [BUDDY_PKG, OWEN_PKG, PARK_PKG]}, f)

    def _assemble(self, raw, **kw):
        pkgs = self.mod.load_packages(self.pkg_path)
        return self.mod.assemble_v6_prompt(
            raw_prompt=raw, packages=pkgs, **kw,
        )

    def test_detects_buddy_by_name(self):
        r = self._assemble("Medium shot of Buddy running through leaves", include_description=True)
        self.assertIn("Buddy", r["enriched_prompt"])
        names = [e["name"] for e in r["injected"]]
        self.assertIn("Buddy", names)
        # Description paragraph should include the golden retriever phrase
        self.assertIn("golden retriever", r["enriched_prompt"].lower())

    def test_detects_multiple_entities(self):
        r = self._assemble(
            "Wide shot of Owen walking with Buddy in Autumn Park",
            include_description=True,
        )
        names = sorted(e["name"] for e in r["injected"])
        self.assertEqual(names, ["Autumn Park", "Buddy", "Owen"])

    def test_case_insensitive_detection(self):
        r = self._assemble("close-up of buddy in AUTUMN PARK", include_description=True)
        names = sorted(e["name"] for e in r["injected"])
        self.assertIn("Buddy", names)
        self.assertIn("Autumn Park", names)

    def test_must_keep_extracted(self):
        r = self._assemble("Buddy portrait", include_description=True)
        mk = [m.lower() for m in r["must_keep"]]
        self.assertTrue(any("red collar" in m for m in mk))
        self.assertTrue(any("pendant ears" in m for m in mk))

    def test_clip_target_strips_description(self):
        # For clip prompts (anchor carries identity), description paragraph
        # should be omitted.
        r = self._assemble(
            "Slow push-in on Buddy",
            include_description=False, max_chars=400,
        )
        self.assertNotIn("adult golden retriever", r["enriched_prompt"].lower())

    def test_shot_context_force_injection(self):
        # No name in prompt, but shot_context.character_ids forces Buddy.
        r = self._assemble(
            "Slow push-in on the subject",
            shot_context={"character_ids": ["pkg_char_buddy"]},
            include_description=True,
        )
        names = [e["name"] for e in r["injected"]]
        self.assertIn("Buddy", names)

    def test_substring_false_positive_guarded(self):
        # "Buddy" should NOT fire on "buddyism" — whole-word match
        r = self._assemble("Abstract shot of buddyism concept", include_description=True)
        names = [e["name"] for e in r["injected"]]
        self.assertNotIn("Buddy", names)


# ---------------------------------------------------------------------------
# kling_prompt_linter
# ---------------------------------------------------------------------------

class TestKlingLinter(unittest.TestCase):
    def setUp(self):
        from lib.kling_prompt_linter import lint_kling_prompt
        self.lint = lint_kling_prompt

    def test_good_prompt_passes(self):
        r = self.lint(
            "Slow push-in on Buddy running through fallen leaves, golden hour sun flickering through oaks"
        )
        self.assertTrue(r["ok"])

    def test_sound_words_flagged(self):
        r = self.lint("Handheld track as the dog is barking loudly at the camera")
        rules = [i["rule"] for i in r["issues"]]
        self.assertIn("sound_words", rules)

    def test_banned_words_flagged(self):
        r = self.lint("Slow dolly, masterpiece 8k ultra hd dissolve")
        rules = [i["rule"] for i in r["issues"]]
        self.assertIn("banned_words", rules)

    def test_no_camera_intent_flagged(self):
        r = self.lint("A man walks into a room looking sad and pensive")
        rules = [i["rule"] for i in r["issues"]]
        self.assertIn("no_camera_intent", rules)

    def test_word_count_too_low(self):
        r = self.lint("Slow push-in")
        rules = [i["rule"] for i in r["issues"]]
        self.assertTrue(any(r.startswith("word_count") for r in rules))

    def test_word_count_too_high(self):
        words = ["slow push-in"] + ["and another thing"] * 30
        r = self.lint(" ".join(words))
        rules = [i["rule"] for i in r["issues"]]
        self.assertTrue(any(r.startswith("word_count") for r in rules))


# ---------------------------------------------------------------------------
# identity_gate
# ---------------------------------------------------------------------------

class TestIdentityGate(unittest.TestCase):
    def setUp(self):
        # Redirect state to a tempfile so we don't clobber production state.
        import lib.identity_gate as gate
        self.gate = gate
        self.tmp = tempfile.mkdtemp()
        self._orig_path = gate.STATE_PATH
        gate.STATE_PATH = os.path.join(self.tmp, "identity_gate.json")

    def tearDown(self):
        self.gate.STATE_PATH = self._orig_path

    def test_lock_then_check(self):
        self.gate.lock_identity("Buddy", "/tmp/x.png", "s1", qa_overall=0.9, qa_identity=0.92)
        self.assertTrue(self.gate.is_locked("Buddy"))
        result = self.gate.check_gate(["Buddy"])
        self.assertTrue(result["all_locked"])
        self.assertEqual(result["locked"], ["Buddy"])
        self.assertEqual(result["unlocked"], [])

    def test_check_reports_unlocked(self):
        self.gate.lock_identity("Buddy", "/tmp/x.png", "s1", 0.9, 0.9)
        r = self.gate.check_gate(["Buddy", "Owen"])
        self.assertFalse(r["all_locked"])
        self.assertEqual(r["unlocked"], ["Owen"])

    def test_unlock(self):
        self.gate.lock_identity("Buddy", "/tmp/x.png", "s1", 0.9, 0.9)
        self.assertTrue(self.gate.unlock_identity("Buddy"))
        self.assertFalse(self.gate.is_locked("Buddy"))

    def test_auto_lock_single_subject_passes(self):
        qa = {
            "candidates": {"A": {"overall": 0.92, "identity": 0.94}},
            "pick": "A",
        }
        r = self.gate.maybe_auto_lock(["Buddy"], "/tmp/x.png", "s1", qa)
        self.assertEqual(r, ["Buddy"])
        self.assertTrue(self.gate.is_locked("Buddy"))

    def test_auto_lock_single_subject_below_floor_skipped(self):
        qa = {
            "candidates": {"A": {"overall": 0.60, "identity": 0.70}},
            "pick": "A",
        }
        r = self.gate.maybe_auto_lock(["Buddy"], "/tmp/x.png", "s1", qa)
        self.assertEqual(r, [])
        self.assertFalse(self.gate.is_locked("Buddy"))

    def test_auto_lock_skipped_for_multi_subject(self):
        qa = {
            "candidates": {"A": {"overall": 0.95, "identity": 0.95}},
            "pick": "A",
        }
        r = self.gate.maybe_auto_lock(["Buddy", "Owen"], "/tmp/x.png", "s1", qa)
        # Multi-subject shots are ambiguous — never auto-lock.
        self.assertEqual(r, [])
        self.assertFalse(self.gate.is_locked("Buddy"))
        self.assertFalse(self.gate.is_locked("Owen"))


# ---------------------------------------------------------------------------
# moderation
# ---------------------------------------------------------------------------

class TestModeration(unittest.TestCase):
    def setUp(self):
        from lib.moderation import moderate_prompt
        self.mod = moderate_prompt

    def test_benign_passes(self):
        r = self.mod("Slow push-in on a dog running through fallen leaves")
        self.assertTrue(r["allowed"])
        self.assertEqual(r["severity"], "ok")

    def test_csam_term_blocked(self):
        r = self.mod("shota character in scene")
        self.assertFalse(r["allowed"])
        self.assertEqual(r["severity"], "hard")

    def test_sexual_minor_combo_blocked(self):
        r = self.mod("A nude child in a bedroom")
        self.assertFalse(r["allowed"])

    def test_public_figure_blocked(self):
        r = self.mod("Donald Trump at a rally")
        self.assertFalse(r["allowed"])
        self.assertTrue(any("public_figure" in x for x in r["reasons"]))

    def test_copyright_character_warned_not_blocked(self):
        r = self.mod("Mickey Mouse in a noir alley")
        self.assertTrue(r["allowed"])
        self.assertEqual(r["severity"], "warn")
        # Redaction should have removed the character name
        self.assertNotIn("mickey mouse", r["redacted_prompt"].lower())

    def test_violence_warned(self):
        r = self.mod("A decapitation scene from the war")
        self.assertTrue(r["allowed"])
        self.assertEqual(r["severity"], "warn")

    def test_false_positive_guarded(self):
        # "kid" should not fire on "kidney"
        r = self.mod("A kidney transplant in a hospital drama")
        self.assertTrue(r["allowed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
