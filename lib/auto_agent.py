"""
LUMN AutoAgent — Self-Improving Generation System

A meta-agent that autonomously improves LUMN's generation quality by:
1. Running eval batches (generate test frames, measure quality)
2. Analyzing failure traces (moderation blocks, identity drift, low quality)
3. Editing the prompt harness (prompt_templates.py strategies)
4. Testing improvements against baseline
5. Keeping what works, reverting what doesn't

Inspired by AutoAgent (kevinrgu/autoagent):
- Meta/task agent split (meta = optimizer, task = generator)
- Trace-based learning (understanding WHY something improved)
- Self-reflection to prevent overfitting
"""

import json
import os
import time
import copy
import hashlib
import threading
from datetime import datetime


# ─────────────────────── Configuration ───────────────────────

AUTOAGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "autoagent")
os.makedirs(AUTOAGENT_DIR, exist_ok=True)

RUNS_DIR = os.path.join(AUTOAGENT_DIR, "runs")
os.makedirs(RUNS_DIR, exist_ok=True)

HARNESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_templates.py")

# The "harness" we're optimizing — these are the tunable parameters
DEFAULT_HARNESS = {
    "version": 1,
    "quality_suffix": "Hyper-realistic, photorealistic, 8K UHD, cinematic lighting, sharp focus, professional cinematography.",
    "identity_strength": {
        "close-up": "CRITICAL: PRESERVE EXACT LIKENESS from @Character reference — identical face shape, identical features, identical proportions, identical skin tone. Do NOT alter or stylize the face.",
        "medium": "PRESERVE EXACT LIKENESS from @Character reference — same face, same build, same features.",
        "full": "PRESERVE EXACT LIKENESS from @Character reference.",
        "wide": "",
        "establishing": "",
    },
    "framing_prefix": {
        "close-up": "Extreme close-up shot. Detailed skin texture, visible pores, catch-light in eyes, shallow depth of field, 85mm portrait lens bokeh.",
        "medium": "Medium shot, waist-up framing, balanced composition, 50mm lens.",
        "full": "Full body shot, head-to-toe framing, character centered, 35mm lens, full figure visible.",
        "wide": "Wide shot, 24mm lens, expansive environment fills frame, character small in frame, cinematic composition, atmosphere, depth.",
        "establishing": "Establishing shot, 16mm ultra-wide, sweeping vista, grand scale, environmental storytelling, dramatic atmosphere, no characters in foreground.",
    },
    "costume_suffix": {
        "close-up": "",
        "medium": "Outfit matches @Costume reference exactly.",
        "full": "Show the COMPLETE outfit clearly — every detail from @Costume reference.",
        "wide": "",
        "establishing": "",
    },
    "environment_suffix": {
        "close-up": "",
        "medium": "",
        "full": "",
        "wide": "Match the EXACT environment from @Setting reference — same architecture, materials, lighting, atmosphere.",
        "establishing": "Match the EXACT location from @Setting reference — same environment, same lighting, same mood.",
    },
    "video_motion_hints": {
        "close-up": "Subtle facial movement, breathing, natural micro-expressions. Maintain exact likeness throughout.",
        "medium": "Natural, fluid motion. Cinematic quality.",
        "full": "Natural, fluid motion. Cinematic quality.",
        "wide": "Atmospheric motion, environmental animation, cinematic sweep.",
        "establishing": "Atmospheric motion, environmental animation, cinematic sweep.",
    },
    "negative_keywords": "blurry, distorted, deformed, low quality, cartoon, anime, sketch, painting, watermark, text overlay",
    "sheet_quality_suffix": "8K ultra-high definition, photorealistic, studio lighting, sharp focus, professional reference sheet.",
    "max_prompt_length": 1000,
}


# ─────────────────────── Eval Metrics ───────────────────────

class EvalResult:
    """Result of evaluating a single generation."""
    def __init__(self, scene_index, shot_type, success, error=None,
                 moderation_blocked=False, generation_time=0, prompt_used=""):
        self.scene_index = scene_index
        self.shot_type = shot_type
        self.success = success
        self.error = error
        self.moderation_blocked = moderation_blocked
        self.generation_time = generation_time
        self.prompt_used = prompt_used
        self.timestamp = datetime.utcnow().isoformat()

    def to_dict(self):
        return {
            "scene_index": self.scene_index,
            "shot_type": self.shot_type,
            "success": self.success,
            "error": self.error,
            "moderation_blocked": self.moderation_blocked,
            "generation_time": self.generation_time,
            "prompt_used": self.prompt_used[:500],
            "timestamp": self.timestamp,
        }


class EvalBatch:
    """Results from running a batch of evaluations."""
    def __init__(self, harness_version, harness_hash):
        self.harness_version = harness_version
        self.harness_hash = harness_hash
        self.results = []
        self.started_at = datetime.utcnow().isoformat()
        self.finished_at = None

    @property
    def success_rate(self):
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.success) / len(self.results)

    @property
    def moderation_rate(self):
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.moderation_blocked) / len(self.results)

    @property
    def avg_generation_time(self):
        times = [r.generation_time for r in self.results if r.success and r.generation_time > 0]
        return sum(times) / len(times) if times else 0

    @property
    def failure_traces(self):
        return [r for r in self.results if not r.success]

    def to_dict(self):
        return {
            "harness_version": self.harness_version,
            "harness_hash": self.harness_hash,
            "success_rate": round(self.success_rate, 4),
            "moderation_rate": round(self.moderation_rate, 4),
            "avg_generation_time": round(self.avg_generation_time, 1),
            "total": len(self.results),
            "passed": sum(1 for r in self.results if r.success),
            "failed": sum(1 for r in self.results if not r.success),
            "moderation_blocked": sum(1 for r in self.results if r.moderation_blocked),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "results": [r.to_dict() for r in self.results],
        }


# ─────────────────────── Harness Manager ───────────────────────

class HarnessManager:
    """Manages the generation harness — the tunable parameters being optimized."""

    def __init__(self):
        self.current = self._load_or_default()
        self.history = []
        self.best = copy.deepcopy(self.current)
        self.best_score = 0.0

    def _harness_path(self):
        return os.path.join(AUTOAGENT_DIR, "current_harness.json")

    def _history_path(self):
        return os.path.join(AUTOAGENT_DIR, "harness_history.json")

    def _load_or_default(self):
        path = self._harness_path()
        if os.path.isfile(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except:
                pass
        return copy.deepcopy(DEFAULT_HARNESS)

    def save(self):
        with open(self._harness_path(), "w") as f:
            json.dump(self.current, f, indent=2)

    def hash(self):
        """Hash the current harness for comparison."""
        s = json.dumps(self.current, sort_keys=True)
        return hashlib.md5(s.encode()).hexdigest()[:12]

    def snapshot(self, score, notes=""):
        """Save a snapshot of current harness with its score."""
        entry = {
            "version": self.current.get("version", 0),
            "hash": self.hash(),
            "score": score,
            "notes": notes,
            "timestamp": datetime.utcnow().isoformat(),
            "harness": copy.deepcopy(self.current),
        }
        self.history.append(entry)

        # Update best if this is the highest score
        if score > self.best_score:
            self.best_score = score
            self.best = copy.deepcopy(self.current)
            entry["is_best"] = True

        # Save history
        with open(self._history_path(), "w") as f:
            json.dump(self.history[-50:], f, indent=2)  # Keep last 50

        return entry

    def revert_to_best(self):
        """Revert to the best-scoring harness."""
        self.current = copy.deepcopy(self.best)
        self.save()
        return self.best_score

    def apply_edit(self, key_path, new_value):
        """Apply an edit to the harness.
        key_path: dot-separated path like 'identity_strength.close-up'
        """
        keys = key_path.split(".")
        target = self.current
        for k in keys[:-1]:
            if isinstance(target, dict) and k in target:
                target = target[k]
            else:
                return False

        final_key = keys[-1]
        if isinstance(target, dict):
            old_value = target.get(final_key)
            target[final_key] = new_value
            self.current["version"] = self.current.get("version", 0) + 1
            self.save()
            return True
        return False


# ─────────────────────── Failure Analyzer ───────────────────────

class FailureAnalyzer:
    """Analyzes failure traces to identify patterns and suggest harness edits."""

    MODERATION_KEYWORDS = [
        "content_moderation", "moderation", "safety", "policy",
        "flagged", "nsfw", "inappropriate", "prohibited",
        "violates", "not allowed", "rejected",
    ]

    QUALITY_KEYWORDS = [
        "distorted", "blurry", "deformed", "artifact",
        "low quality", "unrealistic", "cartoon",
    ]

    @staticmethod
    def classify_failure(error_str):
        """Classify a failure into a category."""
        if not error_str:
            return "unknown"
        lower = error_str.lower()

        if any(kw in lower for kw in FailureAnalyzer.MODERATION_KEYWORDS):
            return "moderation"
        if "timeout" in lower or "timed out" in lower:
            return "timeout"
        if "rate" in lower and "limit" in lower:
            return "rate_limit"
        if "429" in lower:
            return "rate_limit"
        if any(kw in lower for kw in ["500", "502", "503", "server error"]):
            return "server_error"
        if "connection" in lower:
            return "connection"
        if "no image" in lower or "no video" in lower:
            return "generation_failed"
        return "unknown"

    @staticmethod
    def analyze_batch(batch: EvalBatch):
        """Analyze a batch of results and return actionable insights."""
        insights = {
            "failure_categories": {},
            "shot_type_scores": {},
            "suggestions": [],
            "moderation_prompts": [],
        }

        # Categorize failures
        for r in batch.results:
            # Track per-shot-type success
            st = r.shot_type or "unknown"
            if st not in insights["shot_type_scores"]:
                insights["shot_type_scores"][st] = {"total": 0, "passed": 0}
            insights["shot_type_scores"][st]["total"] += 1
            if r.success:
                insights["shot_type_scores"][st]["passed"] += 1

            if not r.success:
                category = FailureAnalyzer.classify_failure(r.error)
                insights["failure_categories"][category] = \
                    insights["failure_categories"].get(category, 0) + 1

                if category == "moderation":
                    insights["moderation_prompts"].append(r.prompt_used[:300])

        # Generate suggestions based on patterns
        cats = insights["failure_categories"]

        if cats.get("moderation", 0) > 0:
            mod_rate = cats["moderation"] / max(len(batch.results), 1)
            if mod_rate > 0.3:
                insights["suggestions"].append({
                    "type": "reduce_moderation",
                    "priority": "high",
                    "action": "Add safety-conscious language to prompts, avoid explicit terms",
                    "detail": f"{cats['moderation']} moderation blocks ({mod_rate:.0%} of generations)",
                })
            insights["suggestions"].append({
                "type": "soften_prompts",
                "priority": "medium",
                "action": "Review moderation-blocked prompts and soften violent/explicit language",
                "detail": f"Blocked prompts: {len(insights['moderation_prompts'])}",
            })

        if cats.get("timeout", 0) > 0:
            insights["suggestions"].append({
                "type": "reduce_complexity",
                "priority": "medium",
                "action": "Simplify prompts that timeout — shorter, less complex descriptions",
                "detail": f"{cats['timeout']} timeouts",
            })

        # Shot type analysis
        for st, scores in insights["shot_type_scores"].items():
            rate = scores["passed"] / max(scores["total"], 1)
            if rate < 0.5 and scores["total"] >= 2:
                insights["suggestions"].append({
                    "type": f"improve_{st}",
                    "priority": "high",
                    "action": f"Improve {st} shot prompts — only {rate:.0%} success rate",
                    "detail": f"{scores['passed']}/{scores['total']} passed",
                })

        return insights


# ─────────────────────── Meta-Agent ───────────────────────

class MetaAgent:
    """The meta-agent that optimizes the generation harness.

    This is the core of AutoAgent — it reads failure traces,
    proposes harness edits, tests them, and keeps improvements.
    """

    def __init__(self, harness_manager=None):
        self.harness = harness_manager or HarnessManager()
        self.analyzer = FailureAnalyzer()
        self.run_log = []
        self.iteration = 0

    def propose_edits(self, insights: dict) -> list:
        """Based on failure analysis, propose specific harness edits.

        Returns list of {key_path, new_value, reason} dicts.
        """
        edits = []

        for suggestion in insights.get("suggestions", []):
            stype = suggestion["type"]

            if stype == "reduce_moderation":
                # Add explicit safety language to quality suffix
                current_quality = self.harness.current.get("quality_suffix", "")
                if "tasteful" not in current_quality.lower():
                    edits.append({
                        "key_path": "quality_suffix",
                        "new_value": current_quality + " Tasteful, artistic, professional production quality.",
                        "reason": "High moderation block rate — adding safety-conscious quality language",
                    })

            elif stype == "soften_prompts":
                # Add to negative keywords
                current_neg = self.harness.current.get("negative_keywords", "")
                additions = "nsfw, explicit, gore, violence, blood"
                if "nsfw" not in current_neg:
                    edits.append({
                        "key_path": "negative_keywords",
                        "new_value": current_neg + ", " + additions,
                        "reason": "Adding explicit negative keywords to reduce moderation blocks",
                    })

            elif stype == "reduce_complexity":
                # Shorten max prompt length
                current_max = self.harness.current.get("max_prompt_length", 1000)
                if current_max > 700:
                    edits.append({
                        "key_path": "max_prompt_length",
                        "new_value": max(600, current_max - 100),
                        "reason": "Reducing prompt length to prevent timeouts",
                    })

            elif stype.startswith("improve_"):
                shot_type = stype.replace("improve_", "")
                # Enhance the framing prefix for this shot type
                current_framing = self.harness.current.get("framing_prefix", {}).get(shot_type, "")
                if current_framing and "professional" not in current_framing.lower():
                    edits.append({
                        "key_path": f"framing_prefix.{shot_type}",
                        "new_value": current_framing + " Professional cinema production, award-winning cinematography.",
                        "reason": f"Low success rate on {shot_type} shots — strengthening prompt quality",
                    })

        return edits

    def apply_and_test(self, edits: list, eval_fn) -> dict:
        """Apply proposed edits, run eval, keep if improved.

        Args:
            edits: list of {key_path, new_value, reason}
            eval_fn: callable() -> EvalBatch

        Returns: {improved, old_score, new_score, edits_applied, edits_reverted}
        """
        self.iteration += 1

        # Save current state as baseline
        baseline_hash = self.harness.hash()
        baseline = copy.deepcopy(self.harness.current)

        result = {
            "iteration": self.iteration,
            "edits_proposed": len(edits),
            "edits_applied": [],
            "edits_reverted": [],
            "old_score": self.harness.best_score,
            "new_score": 0,
            "improved": False,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if not edits:
            result["notes"] = "No edits proposed"
            self.run_log.append(result)
            return result

        # Apply edits
        for edit in edits:
            success = self.harness.apply_edit(edit["key_path"], edit["new_value"])
            if success:
                result["edits_applied"].append(edit)
            else:
                result["edits_reverted"].append({**edit, "reason": "Failed to apply"})

        if not result["edits_applied"]:
            result["notes"] = "No edits could be applied"
            self.run_log.append(result)
            return result

        # Run eval with new harness
        try:
            batch = eval_fn()
            new_score = batch.success_rate
            result["new_score"] = new_score
            result["eval_results"] = batch.to_dict()

            # Self-reflection: would this improvement generalize?
            reflection = self._self_reflect(edits, batch)
            result["reflection"] = reflection

            if new_score > self.harness.best_score and reflection["generalizable"]:
                # Improvement! Keep it.
                self.harness.snapshot(new_score, f"Iteration {self.iteration}: {new_score:.1%} success")
                result["improved"] = True
                result["notes"] = f"Improved from {self.harness.best_score:.1%} to {new_score:.1%}"
            else:
                # No improvement or overfitting — revert
                self.harness.current = baseline
                self.harness.save()
                result["improved"] = False
                if not reflection["generalizable"]:
                    result["notes"] = f"Reverted: improvement looks like overfitting ({reflection['reason']})"
                else:
                    result["notes"] = f"Reverted: {new_score:.1%} <= {self.harness.best_score:.1%}"
                result["edits_reverted"] = result["edits_applied"]
                result["edits_applied"] = []

        except Exception as e:
            # Eval failed — revert
            self.harness.current = baseline
            self.harness.save()
            result["notes"] = f"Eval failed: {str(e)[:200]}"
            result["edits_reverted"] = result["edits_applied"]
            result["edits_applied"] = []

        self.run_log.append(result)
        return result

    def _self_reflect(self, edits, batch):
        """Self-reflection: if this exact task disappeared, would this still be worthwhile?

        Prevents overfitting to specific test cases.
        """
        reflection = {"generalizable": True, "reason": ""}

        # Check: did we just add task-specific keywords?
        for edit in edits:
            new_val = str(edit.get("new_value", ""))
            # Overfitting signals
            if len(new_val) > 500:
                reflection["generalizable"] = False
                reflection["reason"] = "Edit too long — likely overfit to specific case"
                break
            if new_val.count(",") > 20:
                reflection["generalizable"] = False
                reflection["reason"] = "Too many comma-separated items — keyword stuffing"
                break

        # Check: is improvement suspiciously large? (>30% jump usually means gaming)
        if batch.success_rate - self.harness.best_score > 0.3:
            if len(batch.results) < 5:
                reflection["generalizable"] = False
                reflection["reason"] = "Large improvement on small sample — needs more evals"

        return reflection


# ─────────────────────── Optimization Runner ───────────────────────

class OptimizationRun:
    """Manages a full optimization run — multiple iterations of the meta-agent loop."""

    def __init__(self, run_id=None):
        self.run_id = run_id or f"run_{int(time.time())}"
        self.run_dir = os.path.join(RUNS_DIR, self.run_id)
        os.makedirs(self.run_dir, exist_ok=True)

        self.harness = HarnessManager()
        self.meta = MetaAgent(self.harness)
        self.status = "idle"  # idle, running, paused, completed, error
        self.iterations_completed = 0
        self.max_iterations = 20
        self.start_time = None
        self.end_time = None
        self.results = []
        self._thread = None
        self._stop_flag = False

    def get_status(self):
        return {
            "run_id": self.run_id,
            "status": self.status,
            "iterations_completed": self.iterations_completed,
            "max_iterations": self.max_iterations,
            "best_score": self.harness.best_score,
            "current_version": self.harness.current.get("version", 0),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "results_summary": [
                {
                    "iteration": r.get("iteration"),
                    "improved": r.get("improved"),
                    "old_score": r.get("old_score"),
                    "new_score": r.get("new_score"),
                    "notes": r.get("notes", "")[:100],
                }
                for r in self.results[-20:]
            ],
        }

    def start(self, eval_fn, max_iterations=20):
        """Start the optimization loop in a background thread."""
        if self.status == "running":
            return {"error": "Already running"}

        self.max_iterations = max_iterations
        self._stop_flag = False
        self.status = "running"
        self.start_time = datetime.utcnow().isoformat()

        def _run_loop():
            try:
                # Initial baseline eval
                print(f"[AutoAgent] Starting run {self.run_id} — {max_iterations} iterations")
                baseline_batch = eval_fn()
                baseline_score = baseline_batch.success_rate
                self.harness.best_score = baseline_score
                self.harness.snapshot(baseline_score, "Baseline")
                print(f"[AutoAgent] Baseline score: {baseline_score:.1%}")

                for i in range(max_iterations):
                    if self._stop_flag:
                        self.status = "paused"
                        break

                    print(f"[AutoAgent] Iteration {i+1}/{max_iterations}")

                    # 1. Run eval with current harness
                    batch = eval_fn()

                    # 2. Analyze failures
                    insights = self.meta.analyzer.analyze_batch(batch)

                    # 3. Propose edits
                    edits = self.meta.propose_edits(insights)

                    if not edits:
                        print(f"[AutoAgent] No edits to propose — harness may be optimal")
                        # Try random exploration
                        edits = self._explore_random()

                    # 4. Apply, test, keep/revert
                    result = self.meta.apply_and_test(edits, eval_fn)
                    self.results.append(result)
                    self.iterations_completed = i + 1

                    # Save run state
                    self._save_state()

                    improved = "IMPROVED" if result["improved"] else "reverted"
                    print(f"[AutoAgent] Iteration {i+1}: {improved} — score: {result.get('new_score', 0):.1%}")

                    # Early stopping if we hit 100%
                    if self.harness.best_score >= 0.99:
                        print(f"[AutoAgent] Perfect score reached — stopping")
                        break

                self.status = "completed"
                self.end_time = datetime.utcnow().isoformat()
                print(f"[AutoAgent] Run complete. Best score: {self.harness.best_score:.1%}")

            except Exception as e:
                self.status = "error"
                self.end_time = datetime.utcnow().isoformat()
                print(f"[AutoAgent] Run error: {e}")
                import traceback
                traceback.print_exc()

            self._save_state()

        self._thread = threading.Thread(target=_run_loop, daemon=True)
        self._thread.start()

        return {"ok": True, "run_id": self.run_id}

    def stop(self):
        self._stop_flag = True
        return {"ok": True, "message": "Stopping after current iteration"}

    def _explore_random(self):
        """Random exploration when no failure-driven edits are available."""
        import random

        explorations = [
            {
                "key_path": "quality_suffix",
                "new_value": random.choice([
                    "Hyper-realistic, photorealistic, 8K UHD, cinematic lighting, sharp focus, professional cinematography.",
                    "Ultra-detailed, photorealistic, cinematic color grading, dramatic lighting, ARRI Alexa quality.",
                    "Photorealistic, film grain, anamorphic lens, cinematic composition, Kodak Vision3 color science.",
                    "8K resolution, photorealistic skin detail, volumetric lighting, depth of field, cinema-grade.",
                    "Award-winning cinematography, photorealistic, natural lighting, sharp optics, professional color science.",
                ]),
                "reason": "Exploring alternative quality suffix language",
            },
        ]

        return [random.choice(explorations)]

    def _save_state(self):
        state = {
            "run_id": self.run_id,
            "status": self.status,
            "iterations": self.iterations_completed,
            "best_score": self.harness.best_score,
            "results": self.results,
            "harness": self.harness.current,
        }
        path = os.path.join(self.run_dir, "state.json")
        with open(path, "w") as f:
            json.dump(state, f, indent=2)


# ─────────────────────── Global Instance ───────────────────────

_current_run = None

def get_or_create_run():
    global _current_run
    if _current_run is None:
        _current_run = OptimizationRun()
    return _current_run

def get_current_run():
    return _current_run
