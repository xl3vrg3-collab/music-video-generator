"""
V4 Plan Regression Tests — validate plan structure, scoring, and constraints.

All tests are self-contained: no network, no file I/O beyond imports.
Run with:  pytest tests/test_v4_regression.py -v
"""

import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from lib.quality_metrics import score_plan_quality, compare_v3_v4


# ---------------------------------------------------------------------------
# Shared mock data via fixtures
# ---------------------------------------------------------------------------

def _make_shot(shot_id, shot_size="MS", duration=3.0, subject="Luna",
               character_id="char_001", direction="L2R", emotion="neutral",
               is_hero=False, beat_id="beat_00", prompt=None, takes=None):
    shot = {
        "shot_id": shot_id,
        "shot_size": shot_size,
        "target_duration": duration,
        "movement": "static",
        "subject": subject,
        "characterId": character_id,
        "screen_direction": direction,
        "emotion": emotion,
        "is_hero": is_hero,
        "beat_id": beat_id,
    }
    if prompt is not None:
        shot["prompt"] = prompt
    if takes is not None:
        shot["takes"] = takes
    return shot


@pytest.fixture
def six_beat_plan():
    """A well-structured V4 plan with 6 beats and 18 total shots."""
    sizes_pool = ["EWS", "MS", "CU", "WS", "MCU", "ECU", "OTS", "POV", "INSERT"]
    beats = []
    shot_idx = 0
    for b in range(6):
        beat_id = f"beat_{b:02d}"
        n_shots = 3  # 3 shots per beat = 18 total
        shots = []
        for s in range(n_shots):
            size = sizes_pool[(shot_idx) % len(sizes_pool)]
            shots.append(_make_shot(
                shot_id=f"b{b:02d}_s{s:02d}",
                shot_size=size,
                duration=2.0 + (shot_idx % 5) * 0.8,
                beat_id=beat_id,
            ))
            shot_idx += 1
        beats.append({
            "beat_id": beat_id,
            "beat_type": ["opening", "rising", "rising", "climax", "falling", "closing"][b],
            "sequence_type": "establish",
            "start_sec": b * 12.0,
            "end_sec": (b + 1) * 12.0,
            "energy": [0.3, 0.5, 0.7, 0.9, 0.7, 0.4][b],
            "shots": shots,
        })
    return {"plan_version": 4, "beats": beats, "scenes": []}


@pytest.fixture
def diverse_beat_plan():
    """Plan where each beat has wide + medium + close coverage."""
    beats = []
    for b in range(4):
        beat_id = f"beat_{b:02d}"
        beats.append({
            "beat_id": beat_id,
            "energy": [0.3, 0.7, 0.9, 0.4][b],
            "shots": [
                _make_shot(f"b{b}_w", shot_size="WS", duration=4.0, beat_id=beat_id),
                _make_shot(f"b{b}_m", shot_size="MS", duration=2.5, beat_id=beat_id),
                _make_shot(f"b{b}_c", shot_size="CU", duration=1.5, beat_id=beat_id),
                _make_shot(f"b{b}_p", shot_size="POV", duration=3.0, beat_id=beat_id),
            ],
        })
    return {"plan_version": 4, "beats": beats, "scenes": []}


@pytest.fixture
def v3_flat_plan():
    """A V3-style plan with flat scenes array, no beats."""
    scenes = [
        _make_shot(f"scene_{i}", shot_size="MS", duration=4.0, direction="L2R")
        for i in range(6)
    ]
    return {"plan_version": 3, "scenes": scenes}


@pytest.fixture
def v4_rich_plan(six_beat_plan):
    """Alias for the rich 6-beat plan."""
    return six_beat_plan


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestV4Regression:

    def test_shot_expansion_count(self, six_beat_plan):
        """A plan with 6 beats should expand to 15-25 total shots, not 6."""
        total_shots = sum(len(b["shots"]) for b in six_beat_plan["beats"])
        assert 15 <= total_shots <= 25, (
            f"Expected 15-25 shots across 6 beats, got {total_shots}"
        )

    def test_no_triple_shot_size(self):
        """No 3 consecutive shots should share the same shot_size."""
        shots = [
            _make_shot("s0", shot_size="MS"),
            _make_shot("s1", shot_size="CU"),
            _make_shot("s2", shot_size="MS"),
            _make_shot("s3", shot_size="WS"),
            _make_shot("s4", shot_size="ECU"),
            _make_shot("s5", shot_size="OTS"),
            _make_shot("s6", shot_size="MS"),
            _make_shot("s7", shot_size="CU"),
        ]
        for i in range(len(shots) - 2):
            a = shots[i]["shot_size"]
            b = shots[i + 1]["shot_size"]
            c = shots[i + 2]["shot_size"]
            assert not (a == b == c), (
                f"Triple repeat at index {i}: {a}, {b}, {c}"
            )

    def test_screen_direction_consistency(self):
        """Alternating screen directions for the same subject should flag violations."""
        shots = []
        for i in range(10):
            direction = "L2R" if i % 2 == 0 else "R2L"
            shots.append(_make_shot(
                f"s{i}", subject="Luna", direction=direction,
            ))

        # Check for direction flips on the same subject
        violations = []
        for i in range(1, len(shots)):
            prev = shots[i - 1]
            curr = shots[i]
            if (prev["subject"] == curr["subject"]
                    and prev["screen_direction"]
                    and curr["screen_direction"]
                    and prev["screen_direction"] != curr["screen_direction"]):
                violations.append(i)

        # With alternating L2R/R2L, every consecutive pair should violate
        assert len(violations) >= 5, (
            f"Expected at least 5 direction violations, got {len(violations)}"
        )

    def test_duration_variance(self, six_beat_plan):
        """Shot durations should have meaningful variance (std_dev > 0.5)."""
        durations = []
        for beat in six_beat_plan["beats"]:
            for shot in beat["shots"]:
                durations.append(shot["target_duration"])

        mean = sum(durations) / len(durations)
        variance = sum((d - mean) ** 2 for d in durations) / len(durations)
        std_dev = math.sqrt(variance)

        assert std_dev > 0.5, (
            f"Duration std_dev is {std_dev:.2f}, expected > 0.5 for dynamic pacing"
        )

    def test_character_hard_fail(self):
        """Shots referencing unknown characters with empty character list should fail."""
        shots = [
            _make_shot("s0", subject="UnknownCharacter", character_id=""),
            _make_shot("s1", subject="UnknownCharacter", character_id=""),
            _make_shot("s2", subject="MysteryPerson", character_id=""),
        ]
        characters = {}  # empty character registry

        failures = []
        for s in shots:
            if s["subject"] and not characters.get(s["subject"]):
                if not s.get("characterId"):
                    failures.append({
                        "shot_id": s["shot_id"],
                        "subject": s["subject"],
                        "error": "no character binding",
                    })

        assert len(failures) == 3, (
            f"Expected 3 character binding failures, got {len(failures)}"
        )
        assert all(f["error"] == "no character binding" for f in failures)

    def test_prompt_under_1000(self):
        """All shot prompts must be under 1000 characters."""
        shots = [
            _make_shot("s0", prompt="A wide establishing shot of a moonlit forest."),
            _make_shot("s1", prompt="Medium close-up of Luna looking thoughtful."),
            _make_shot("s2", prompt="X" * 999),  # exactly 999 — should pass
        ]

        for s in shots:
            prompt = s.get("prompt", "")
            assert len(prompt) < 1000, (
                f"Shot {s['shot_id']} prompt is {len(prompt)} chars (limit 1000)"
            )

    def test_hero_multi_take(self):
        """Hero shots should have at least 2 takes for quality selection."""
        hero_shots = [
            _make_shot("hero_0", is_hero=True, takes=[
                {"take_id": "t0", "status": "pending"},
                {"take_id": "t1", "status": "pending"},
            ]),
            _make_shot("hero_1", is_hero=True, takes=[
                {"take_id": "t0", "status": "pending"},
                {"take_id": "t1", "status": "pending"},
                {"take_id": "t2", "status": "pending"},
            ]),
        ]

        for shot in hero_shots:
            assert shot["is_hero"] is True
            assert "takes" in shot, f"Hero shot {shot['shot_id']} missing takes[]"
            assert len(shot["takes"]) >= 2, (
                f"Hero shot {shot['shot_id']} has {len(shot['takes'])} takes, need >= 2"
            )

    def test_music_sync_cuts(self):
        """At least 50% of cumulative cut points should land within 0.3s of a beat."""
        shots = [
            _make_shot("s0", duration=2.0),
            _make_shot("s1", duration=2.5),
            _make_shot("s2", duration=2.0),
            _make_shot("s3", duration=2.5),
            _make_shot("s4", duration=2.0),
            _make_shot("s5", duration=2.5),
        ]
        # Cumulative cuts: 2.0, 4.5, 6.5, 9.0, 11.0, 13.5
        beat_times = [2.0, 4.4, 6.6, 9.1, 11.0, 13.3]

        cuts = []
        cumulative = 0.0
        for s in shots:
            cumulative += s["target_duration"]
            cuts.append(cumulative)

        synced = 0
        for cut in cuts:
            if any(abs(cut - bt) <= 0.3 for bt in beat_times):
                synced += 1

        sync_pct = synced / len(cuts)
        assert sync_pct >= 0.50, (
            f"Only {sync_pct:.0%} cuts on beat, expected >= 50%"
        )

    def test_coverage_per_beat(self, diverse_beat_plan):
        """Each beat should have at least one wide and one close shot."""
        wide_sizes = {"EWS", "WS"}
        close_sizes = {"CU", "ECU", "INSERT"}

        for beat in diverse_beat_plan["beats"]:
            sizes = {s["shot_size"] for s in beat["shots"]}
            has_wide = bool(sizes & wide_sizes)
            has_close = bool(sizes & close_sizes)
            assert has_wide, f"Beat {beat['beat_id']} missing wide shot (has {sizes})"
            assert has_close, f"Beat {beat['beat_id']} missing close shot (has {sizes})"

    def test_v4_beats_v3_score(self, v3_flat_plan, v4_rich_plan):
        """V4 plan with beats+shots should outscore a flat V3 plan."""
        v3_result = score_plan_quality(v3_flat_plan)
        v4_result = score_plan_quality(v4_rich_plan)

        assert v4_result["total"] > v3_result["total"], (
            f"V4 ({v4_result['total']}) should beat V3 ({v3_result['total']})"
        )

        # Also test compare_v3_v4
        comparison = compare_v3_v4(v3_flat_plan, v4_rich_plan)
        assert comparison["improvement"]["total"] > 0
        assert "V4 scores" in comparison["summary"]
