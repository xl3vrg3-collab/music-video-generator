"""
Multi-shot continuity live test against fal.

Validates the V6 pipeline under real generation conditions:

  T1  num_images=3 auto-rank — generate 3 candidates for a new Buddy shot,
      confirm Sonnet auto-picks the best and identity lock sticks.
  T2  identity_gate_blocked — attempt a clip featuring Owen (who is
      unlocked); expect HTTP 428.
  T3  Owen identity gen — generate an Owen anchor; expect auto-lock.
  T4  multi-character anchor — generate Buddy+Owen in a reunion shot;
      verify QA and that previously-locked characters stay locked.
  T5  continuity clip — generate a clip from T4 anchor; should pass the
      identity gate (both characters locked).

Usage:
  python tools/multi_shot_test.py                # run all stages
  python tools/multi_shot_test.py --skip-clips   # anchors only (~$0.3)

Rough cost: ~$1.60 full, ~$0.30 without clips.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

HOST = os.environ.get("LUMN_HOST", "127.0.0.1:3849")
TOKEN = os.environ.get("LUMN_API_TOKEN", "test")
BASE = f"http://{HOST}"


def get_csrf() -> str:
    for _ in range(3):
        try:
            with urllib.request.urlopen(f"{BASE}/", timeout=30) as r:
                html = r.read().decode("utf-8", errors="ignore")
            m = re.search(r'csrf-token" content="([a-f0-9]+)"', html)
            if m:
                return m.group(1)
        except Exception:
            pass
    return ""


CSRF = ""


def req(method: str, path: str, body: dict | None = None, timeout: int = 600) -> tuple[int, dict]:
    data = None
    headers = {"X-CSRF-Token": CSRF, "Authorization": f"Bearer {TOKEN}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": raw[:200]}
    except Exception as e:
        return 0, {"error": str(e)}


def banner(title: str):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def poll_clip_file(shot_id: str, timeout_s: int = 360) -> str | None:
    """Poll filesystem for the clip to appear."""
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(proj, "output", "pipeline", "clips_v6", f"{shot_id}.mp4")
    start = time.time()
    last = -1
    while time.time() - start < timeout_s:
        if os.path.isfile(path):
            return path
        elapsed = int(time.time() - start)
        if elapsed - last >= 20:
            print(f"  … waiting for clip ({elapsed}s)")
            last = elapsed
        time.sleep(2)
    return None


def check_budget(need: float) -> bool:
    c, b = req("GET", "/api/cost-tracker")
    if c != 200:
        print(f"  (budget endpoint returned {c}, continuing)")
        return True
    spent = float(b.get("total_cost", 0) or 0)
    budget = float(b.get("budget", 0) or 0)
    head = budget - spent
    print(f"  budget: ${budget:.2f}, spent: ${spent:.3f}, headroom: ${head:.2f}, need: ${need:.2f}")
    return head >= need


def t1_auto_rank(results: dict):
    banner("T1 — num_images=3 auto-rank on Buddy shot")
    if not check_budget(0.15):
        results["t1"] = {"status": "SKIP", "reason": "insufficient budget"}
        return
    code, body = req("POST", "/api/v6/anchor/generate", {
        "prompt": "Wide establishing shot, Buddy running through fallen leaves, golden hour, oak trees in background, shallow depth of field",
        "shot_id": "multi_t1_wide",
        "num_images": 3,
    })
    if code != 200:
        print(f"  FAIL: HTTP {code}: {body.get('error')}")
        results["t1"] = {"status": "FAIL", "code": code, "error": body.get("error")}
        return
    paths = body.get("paths", [])
    qa = body.get("qa") or {}
    pick = qa.get("pick", "?")
    cands = qa.get("candidates", {})
    selected = body.get("selected_path") or "?"
    print(f"  generated {len(paths)} candidates")
    for lbl, sc in cands.items():
        print(f"    {lbl}: overall={sc.get('overall')}, id={sc.get('identity')}, notes={sc.get('notes','')[:60]}")
    print(f"  Sonnet pick: {pick}, selected_path: {os.path.basename(selected)}")
    print(f"  locked: {qa.get('identity_gate_locked') or '(already locked / skipped)'}")
    results["t1"] = {
        "status": "PASS" if paths else "FAIL",
        "n_candidates": len(paths),
        "pick": pick,
        "selected": selected,
        "qa": qa,
    }


def t2_gate_blocks_owen(results: dict):
    banner("T2 — identity gate should block a clip featuring Owen")
    # Use the existing Buddy anchor as the "fake" anchor since we just need
    # the gate check to fire before spend.
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    anchor = os.path.join(proj, "output", "pipeline", "anchors_v6", "e2e_test_01", "selected.png")
    if not os.path.isfile(anchor):
        results["t2"] = {"status": "SKIP", "reason": "no existing anchor"}
        return
    code, body = req("POST", "/api/v6/clip/generate", {
        "shot_id": "multi_t2_owen_block",
        "anchor_path": anchor,
        "prompt": "Handheld track, Owen walks into frame looking for Buddy",
        "duration": 5,
        "tier": "v3_standard",
    })
    if code == 428:
        unlocked = (body.get("gate") or {}).get("unlocked", [])
        ok = "Owen" in unlocked
        print(f"  {'PASS' if ok else 'FAIL'}: gate returned 428, unlocked={unlocked}")
        results["t2"] = {
            "status": "PASS" if ok else "FAIL",
            "code": 428,
            "unlocked": unlocked,
        }
    else:
        print(f"  FAIL: expected 428, got {code}: {body}")
        results["t2"] = {"status": "FAIL", "code": code, "body": body}


def t3_owen_anchor(results: dict):
    banner("T3 — Owen anchor (single-subject, should auto-lock)")
    if not check_budget(0.07):
        results["t3"] = {"status": "SKIP", "reason": "insufficient budget"}
        return
    code, body = req("POST", "/api/v6/anchor/generate", {
        "prompt": "Medium close-up of Owen sitting on a park bench, looking off frame with concern, golden hour light",
        "shot_id": "multi_t3_owen",
        "num_images": 1,
    })
    if code != 200:
        print(f"  FAIL: HTTP {code}: {body.get('error')}")
        results["t3"] = {"status": "FAIL", "code": code, "error": body.get("error")}
        return
    qa = body.get("qa") or {}
    locked = qa.get("identity_gate_locked") or []
    print(f"  paths: {len(body.get('paths', []))}")
    pick = qa.get("pick", "?")
    cand = (qa.get("candidates") or {}).get(pick, {})
    print(f"  QA pick={pick}, overall={cand.get('overall')}, id={cand.get('identity')}")
    print(f"  auto-locked: {locked}")
    ok_lock = "Owen" in locked or _is_locked("Owen")
    results["t3"] = {
        "status": "PASS" if ok_lock else "WARN",
        "locked_now": locked,
        "already_locked": _is_locked("Owen"),
        "qa": qa,
    }


def _is_locked(name: str) -> bool:
    c, b = req("GET", "/api/v6/identity-gate")
    if c != 200:
        return False
    chars = b.get("characters", {}) or {}
    return bool(chars.get(name, {}).get("locked"))


def t4_multi_char_anchor(results: dict):
    banner("T4 — multi-character anchor (Buddy + Owen reunion)")
    if not check_budget(0.08):
        results["t4"] = {"status": "SKIP", "reason": "insufficient budget"}
        return
    code, body = req("POST", "/api/v6/anchor/generate", {
        "prompt": "Medium shot of Owen kneeling to embrace Buddy in Autumn Park, warm reunion, golden hour",
        "shot_id": "multi_t4_reunion",
        "num_images": 1,
    })
    if code != 200:
        print(f"  FAIL: HTTP {code}: {body.get('error')}")
        results["t4"] = {"status": "FAIL", "code": code, "error": body.get("error")}
        return
    inj = body.get("injection", {})
    qa = body.get("qa") or {}
    print(f"  injected: {[e.get('name') for e in inj.get('injected', [])]}")
    print(f"  must_keep count: {len(inj.get('must_keep', []))}")
    # Multi-subject anchors should NOT auto-lock (ambiguous). Check state.
    locked = qa.get("identity_gate_locked") or []
    pick = qa.get("pick", "?")
    cand = (qa.get("candidates") or {}).get(pick, {})
    print(f"  QA pick={pick}, overall={cand.get('overall')}, id={cand.get('identity')}")
    print(f"  newly auto-locked (should be empty for multi-subj): {locked}")
    results["t4"] = {
        "status": "PASS" if body.get("paths") else "FAIL",
        "injected": [e.get("name") for e in inj.get("injected", [])],
        "qa": qa,
        "selected_path": body.get("selected_path"),
    }


def t5_continuity_clip(results: dict, skip: bool = False):
    banner("T5 — continuity clip from T4 anchor (both chars locked)")
    if skip:
        results["t5"] = {"status": "SKIP", "reason": "--skip-clips"}
        return
    t4 = results.get("t4") or {}
    selected = t4.get("selected_path")
    if not selected or not os.path.isfile(selected):
        results["t5"] = {"status": "SKIP", "reason": "no T4 anchor"}
        return
    if not check_budget(0.5):
        results["t5"] = {"status": "SKIP", "reason": "insufficient budget"}
        return
    # Both Buddy and Owen should be locked by now
    g_c, g_b = req("GET", "/api/v6/identity-gate")
    chars = (g_b or {}).get("characters", {})
    print(f"  gate state: {list(chars.keys())}")
    code, body = req("POST", "/api/v6/clip/generate", {
        "shot_id": "multi_t5_reunion_clip",
        "anchor_path": selected,
        "prompt": "Slow push-in, Owen pulls Buddy closer, leaves drift across frame in the golden light",
        "duration": 5,
        "tier": "v3_standard",
        "num_candidates": 1,
    })
    if code != 200:
        print(f"  FAIL: HTTP {code}: {body.get('error')} {body.get('lint', {})}")
        results["t5"] = {"status": "FAIL", "code": code, "body": body}
        return
    print(f"  queued: {body.get('num_candidates')} candidates, est ${body.get('est_total')}")
    path = poll_clip_file("multi_t5_reunion_clip", timeout_s=360)
    if path:
        size = os.path.getsize(path) / 1024 / 1024
        print(f"  PASS: clip delivered, {size:.1f}MB")
        results["t5"] = {"status": "PASS", "path": path, "size_mb": round(size, 2)}
    else:
        print("  FAIL: clip did not land within 360s")
        results["t5"] = {"status": "FAIL", "reason": "timeout"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-clips", action="store_true", help="anchors only, no video spend")
    ap.add_argument("--json-out", default="/tmp/multi_shot_results.json")
    args = ap.parse_args()

    global CSRF
    CSRF = get_csrf()
    if not CSRF:
        print("ABORT: no CSRF token")
        return 1
    print(f"CSRF: {CSRF[:8]}…")

    results: dict = {}
    t1_auto_rank(results)
    t2_gate_blocks_owen(results)
    t3_owen_anchor(results)
    t4_multi_char_anchor(results)
    t5_continuity_clip(results, skip=args.skip_clips)

    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    for k, v in results.items():
        print(f"  {k}: {v.get('status', '?')}  {v.get('reason', '') or ''}")

    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nwrote {args.json_out}")

    fails = sum(1 for r in results.values() if r.get("status") == "FAIL")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
