"""
Director Brain — Creative DNA System

Learns your unique filmmaking style by analyzing:
- Which shots you approve vs reject
- What prompts produce your best results
- Your preferred camera angles per scene type
- Your color grade choices
- Your pacing patterns
- Your character framing preferences

Over time, builds a "style vector" that auto-recommends settings for new scenes.
"""

import json
import os
import time
from datetime import datetime
from collections import Counter

BRAIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "director_brain")
os.makedirs(BRAIN_DIR, exist_ok=True)


class DirectorBrain:
    """Learns and recommends creative decisions based on user history."""

    def __init__(self):
        self.ratings = self._load("ratings.json", [])
        self.style_vector = self._load("style_vector.json", {})
        self.shot_history = self._load("shot_history.json", [])

    def _load(self, filename, default):
        path = os.path.join(BRAIN_DIR, filename)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                pass
        return default

    def _save(self, filename, data):
        path = os.path.join(BRAIN_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # ─── Rating System ───

    def rate_scene(self, scene_index: int, rating: int, scene_data: dict):
        """Rate a generated scene 1-5. Feeds the learning system.

        Args:
            scene_index: Which scene
            rating: 1-5 stars
            scene_data: Full scene dict (shot_type, prompt, camera, refs, etc.)
        """
        entry = {
            "scene_index": scene_index,
            "rating": max(1, min(5, rating)),
            "timestamp": datetime.utcnow().isoformat(),
            "shot_type": scene_data.get("shot_type", "medium"),
            "camera": scene_data.get("camera", ""),
            "camera_movement": scene_data.get("camera_movement", ""),
            "color_grade": scene_data.get("color_grade", "none"),
            "duration": scene_data.get("duration", 4),
            "engine": scene_data.get("engine", ""),
            "prompt_length": len(scene_data.get("shot_prompt", scene_data.get("prompt", ""))),
            "has_char_ref": bool(scene_data.get("characters")),
            "has_env_ref": bool(scene_data.get("environments")),
            "approval_status": scene_data.get("approval_status", "draft"),
        }

        self.ratings.append(entry)
        self._save("ratings.json", self.ratings[-500:])  # Keep last 500

        # Also record in shot history for pattern analysis
        self.shot_history.append(entry)
        self._save("shot_history.json", self.shot_history[-1000:])

        # Recalculate style vector
        self._update_style_vector()

        # Feed learned preferences to generation harness for AutoAgent
        self.feed_to_harness()

        return entry

    def _update_style_vector(self):
        """Recalculate the style vector from rating history."""
        if len(self.ratings) < 3:
            return  # Need minimum data

        # Separate good (4-5) and bad (1-2) ratings
        good = [r for r in self.ratings if r["rating"] >= 4]
        bad = [r for r in self.ratings if r["rating"] <= 2]

        sv = {}

        # Preferred shot types (what you rate highly)
        if good:
            shot_counts = Counter(r["shot_type"] for r in good)
            total = sum(shot_counts.values())
            sv["preferred_shot_types"] = {k: round(v/total, 2) for k, v in shot_counts.most_common(5)}

        # Avoided shot types (what you rate poorly)
        if bad:
            bad_shots = Counter(r["shot_type"] for r in bad)
            sv["avoided_shot_types"] = list(bad_shots.keys())

        # Preferred camera angles
        good_cameras = [r["camera"] for r in good if r.get("camera")]
        if good_cameras:
            sv["preferred_cameras"] = Counter(good_cameras).most_common(3)

        # Preferred camera movements
        good_moves = [r["camera_movement"] for r in good if r.get("camera_movement")]
        if good_moves:
            sv["preferred_movements"] = Counter(good_moves).most_common(3)

        # Preferred color grades
        good_grades = [r["color_grade"] for r in good if r.get("color_grade") and r["color_grade"] != "none"]
        if good_grades:
            sv["preferred_grades"] = Counter(good_grades).most_common(3)

        # Average preferred duration
        good_durations = [r["duration"] for r in good if r.get("duration")]
        if good_durations:
            sv["avg_preferred_duration"] = round(sum(good_durations) / len(good_durations), 1)

        # Engine preference
        good_engines = [r["engine"] for r in good if r.get("engine")]
        if good_engines:
            sv["preferred_engine"] = Counter(good_engines).most_common(1)[0][0]

        # Prompt length preference
        good_lengths = [r["prompt_length"] for r in good if r.get("prompt_length")]
        if good_lengths:
            sv["avg_prompt_length"] = round(sum(good_lengths) / len(good_lengths))

        # Reference usage preference
        char_ref_rate = sum(1 for r in good if r.get("has_char_ref")) / max(len(good), 1)
        env_ref_rate = sum(1 for r in good if r.get("has_env_ref")) / max(len(good), 1)
        sv["char_ref_usage"] = round(char_ref_rate, 2)
        sv["env_ref_usage"] = round(env_ref_rate, 2)

        # Overall stats
        all_ratings = [r["rating"] for r in self.ratings]
        sv["total_ratings"] = len(self.ratings)
        sv["avg_rating"] = round(sum(all_ratings) / len(all_ratings), 2)
        sv["last_updated"] = datetime.utcnow().isoformat()

        self.style_vector = sv
        self._save("style_vector.json", sv)

    def feed_to_harness(self):
        """Push learned preferences into the generation harness for AutoAgent."""
        if len(self.ratings) < 5:
            return  # Need minimum data

        sv = self.style_vector
        if not sv:
            return

        harness_path = os.path.join(BRAIN_DIR, "learned_harness.json")

        learned = {
            "quality_suffix_additions": [],
            "preferred_shot_distribution": sv.get("preferred_shot_types", {}),
            "preferred_duration": sv.get("avg_preferred_duration"),
            "preferred_engine": sv.get("preferred_engine"),
            "avoid_shot_types": sv.get("avoided_shot_types", []),
            "updated_at": datetime.utcnow().isoformat(),
            "based_on_ratings": len(self.ratings),
        }

        # Extract patterns from 5-star scenes
        five_star = [r for r in self.ratings if r["rating"] == 5]
        if five_star:
            # What do all 5-star scenes have in common?
            common_shots = Counter(r["shot_type"] for r in five_star)
            if common_shots:
                learned["best_shot_type"] = common_shots.most_common(1)[0][0]

            common_cameras = [r["camera"] for r in five_star if r.get("camera")]
            if common_cameras:
                learned["best_camera"] = Counter(common_cameras).most_common(1)[0][0]

            avg_dur = sum(r["duration"] for r in five_star if r.get("duration")) / max(len(five_star), 1)
            if avg_dur > 0:
                learned["best_duration"] = round(avg_dur, 1)

        # Extract patterns from 1-2 star scenes (what to avoid)
        bad = [r for r in self.ratings if r["rating"] <= 2]
        if bad:
            bad_shots = [r["shot_type"] for r in bad]
            learned["avoid_shot_types"] = list(set(bad_shots))

        with open(harness_path, "w", encoding="utf-8") as f:
            json.dump(learned, f, indent=2, ensure_ascii=False)

        return learned

    # ─── Recommendations ───

    def recommend_for_scene(self, scene_data: dict) -> dict:
        """Recommend settings for a scene based on learned preferences."""
        sv = self.style_vector
        if not sv or sv.get("total_ratings", 0) < 3:
            return {"available": False, "reason": "Rate at least 3 scenes to unlock recommendations"}

        recs = {"available": True, "recommendations": []}

        shot_type = scene_data.get("shot_type", "medium")

        # Shot type recommendation
        preferred = sv.get("preferred_shot_types", {})
        if preferred:
            best_type = max(preferred, key=preferred.get)
            if best_type != shot_type:
                recs["recommendations"].append({
                    "field": "shot_type",
                    "current": shot_type,
                    "suggested": best_type,
                    "reason": f"You rate {best_type} shots highest ({preferred[best_type]:.0%} of your top-rated scenes)",
                    "confidence": preferred[best_type],
                })

        # Camera recommendation
        if sv.get("preferred_cameras"):
            best_cam = sv["preferred_cameras"][0][0]
            current_cam = scene_data.get("camera", "")
            if best_cam and best_cam != current_cam:
                recs["recommendations"].append({
                    "field": "camera",
                    "current": current_cam,
                    "suggested": best_cam,
                    "reason": f"Your top-rated scenes use '{best_cam}' camera angle",
                    "confidence": 0.6,
                })

        # Color grade recommendation
        if sv.get("preferred_grades"):
            best_grade = sv["preferred_grades"][0][0]
            current_grade = scene_data.get("color_grade", "none")
            if best_grade != current_grade:
                recs["recommendations"].append({
                    "field": "color_grade",
                    "current": current_grade,
                    "suggested": best_grade,
                    "reason": f"You prefer '{best_grade}' color grading",
                    "confidence": 0.5,
                })

        # Duration recommendation
        if sv.get("avg_preferred_duration"):
            current_dur = scene_data.get("duration", 4)
            pref_dur = sv["avg_preferred_duration"]
            if abs(current_dur - pref_dur) > 1.5:
                recs["recommendations"].append({
                    "field": "duration",
                    "current": current_dur,
                    "suggested": pref_dur,
                    "reason": f"Your best scenes average {pref_dur}s duration",
                    "confidence": 0.4,
                })

        # Engine recommendation
        if sv.get("preferred_engine"):
            current_engine = scene_data.get("engine", "")
            pref_engine = sv["preferred_engine"]
            if pref_engine and pref_engine != current_engine:
                recs["recommendations"].append({
                    "field": "engine",
                    "current": current_engine,
                    "suggested": pref_engine,
                    "reason": f"Your highest-rated scenes use {pref_engine}",
                    "confidence": 0.5,
                })

        recs["style_summary"] = self.get_style_summary()
        return recs

    def get_style_summary(self) -> str:
        """Human-readable summary of learned style."""
        sv = self.style_vector
        if not sv or sv.get("total_ratings", 0) < 3:
            return "Not enough data yet — rate some scenes to build your profile."

        parts = []

        if sv.get("preferred_shot_types"):
            top = list(sv["preferred_shot_types"].keys())[:2]
            parts.append(f"Prefers {' and '.join(top)} shots")

        if sv.get("preferred_grades"):
            parts.append(f"Favors {sv['preferred_grades'][0][0]} color grading")

        if sv.get("avg_preferred_duration"):
            parts.append(f"Sweet spot: {sv['avg_preferred_duration']}s scenes")

        if sv.get("preferred_engine"):
            parts.append(f"Best results with {sv['preferred_engine']}")

        parts.append(f"Based on {sv.get('total_ratings', 0)} rated scenes (avg {sv.get('avg_rating', 0):.1f}/5)")

        return ". ".join(parts) + "."

    # ─── Prompt Archaeology ───

    def analyze_success_factors(self, scene_data: dict) -> dict:
        """Reverse-engineer why a scene was successful.
        Compare its properties against the style vector."""
        sv = self.style_vector
        if not sv:
            return {"factors": [], "message": "No style data yet"}

        factors = []

        # Check if shot type matches preference
        shot_type = scene_data.get("shot_type", "medium")
        preferred = sv.get("preferred_shot_types", {})
        if shot_type in preferred:
            factors.append({
                "factor": "Shot Type",
                "value": shot_type,
                "impact": "positive",
                "reason": f"This is one of your preferred shot types ({preferred[shot_type]:.0%} of top-rated)",
            })

        # Check duration
        duration = scene_data.get("duration", 4)
        pref_dur = sv.get("avg_preferred_duration", 4)
        if abs(duration - pref_dur) < 1:
            factors.append({
                "factor": "Duration",
                "value": f"{duration}s",
                "impact": "positive",
                "reason": f"Close to your sweet spot of {pref_dur}s",
            })

        # Check references
        if scene_data.get("characters") and sv.get("char_ref_usage", 0) > 0.7:
            factors.append({
                "factor": "Character Reference",
                "value": "Present",
                "impact": "positive",
                "reason": f"You use character refs in {sv['char_ref_usage']:.0%} of top-rated scenes",
            })

        # Check color grade
        grade = scene_data.get("color_grade", "none")
        pref_grades = sv.get("preferred_grades", [])
        if pref_grades and grade == pref_grades[0][0]:
            factors.append({
                "factor": "Color Grade",
                "value": grade,
                "impact": "positive",
                "reason": "This is your most-used color grade in top scenes",
            })

        return {
            "factors": factors,
            "factor_count": len(factors),
            "match_score": len(factors) / max(4, 1),  # 0-1 how much it matches your style
        }


# Global instance
_brain = None

def get_brain():
    global _brain
    if _brain is None:
        _brain = DirectorBrain()
    return _brain
