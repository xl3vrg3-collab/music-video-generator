"""Build a QA gallery of all rendered TB shots.

For each scene with a rendered clip, extracts the mid-frame via ffmpeg and
produces:
  - output/pipeline/qa/<shot_key>_<id>_mid.jpg  (ffmpeg mid-frame)
  - output/pipeline/qa/contact_sheet.html       (gallery with anchor + mid + inline player)

Re-runnable. Only extracts frames that are missing or older than the clip.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PROJECT_ROOT = os.path.join(ROOT, "output", "projects", "default", "prompt_os")
SCENES_JSON  = os.path.join(PROJECT_ROOT, "scenes.json")
ANCHORS_DIR  = os.path.join(ROOT, "output", "pipeline", "anchors_v6")
CLIPS_DIR    = os.path.join(ROOT, "output", "pipeline", "clips_v6")
QA_DIR       = os.path.join(ROOT, "output", "pipeline", "qa")


def _scene_key(name: str) -> str:
    return (name or "").split(" ", 1)[0].strip()


def _sort_key(name: str) -> tuple:
    k = _scene_key(name)
    if "." in k:
        a, b = k.split(".", 1)
        try:
            return (int(a), int(b))
        except ValueError:
            return (99, 99)
    return (99, 99)


def _extract_mid(clip_path: str, out_path: str, duration: int) -> bool:
    if (os.path.isfile(out_path)
            and os.path.getmtime(out_path) >= os.path.getmtime(clip_path)):
        return True
    mid = max(0.1, duration / 2.0)
    cmd = [
        "ffmpeg", "-y", "-ss", f"{mid:.2f}", "-i", clip_path,
        "-frames:v", "1", "-q:v", "3", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0


def _rel(p: str) -> str:
    return os.path.relpath(p, QA_DIR).replace("\\", "/")


def main():
    os.makedirs(QA_DIR, exist_ok=True)
    with open(SCENES_JSON, "r", encoding="utf-8") as f:
        scenes = json.load(f)
    scenes.sort(key=lambda s: _sort_key(s.get("name", "")))

    rows = []
    for s in scenes:
        sid = s["id"]
        name = s["name"]
        duration = int(s.get("duration") or 6)
        anchor = os.path.join(ANCHORS_DIR, sid, "selected.png")
        clip   = os.path.join(CLIPS_DIR, sid, "selected.mp4")
        key = _scene_key(name).replace(".", "_")
        mid_out = os.path.join(QA_DIR, f"{key}_{sid}_mid.jpg")

        has_anchor = os.path.isfile(anchor)
        has_clip   = os.path.isfile(clip)
        mid_ok = False
        if has_clip:
            mid_ok = _extract_mid(clip, mid_out, duration)

        rows.append({
            "name": name, "id": sid, "duration": duration,
            "anchor": _rel(anchor) if has_anchor else None,
            "clip":   _rel(clip)   if has_clip   else None,
            "mid":    _rel(mid_out) if mid_ok    else None,
            "has_anchor": has_anchor, "has_clip": has_clip,
        })

    # HTML gallery
    html = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>TB Lifestream Static — QA Gallery</title>",
        "<style>",
        "body{font-family:-apple-system,Segoe UI,sans-serif;margin:20px;background:#0a0a0c;color:#ddd;}",
        "h1{margin:0 0 14px;}",
        "table{border-collapse:collapse;width:100%;}",
        "td{padding:8px;border-bottom:1px solid #222;vertical-align:top;}",
        ".name{font-size:14px;font-weight:600;color:#f0f0f0;}",
        ".meta{font-size:11px;color:#888;margin-top:4px;}",
        "img{max-width:420px;max-height:240px;display:block;border-radius:4px;}",
        "video{max-width:480px;border-radius:4px;}",
        ".missing{opacity:.4;font-style:italic;color:#888;}",
        ".no-anchor{background:rgba(255,60,60,.08);}",
        "</style></head><body>",
        f"<h1>TB Lifestream Static — QA Gallery</h1>",
        f"<p style='color:#888;font-size:12px;'>{sum(1 for r in rows if r['has_clip'])} of {len(rows)} shots rendered</p>",
        "<table>",
    ]
    for r in rows:
        row_cls = "" if r["has_clip"] else "no-anchor"
        html.append(f"<tr class='{row_cls}'>")
        html.append(f"<td><div class='name'>{r['name']}</div>"
                    f"<div class='meta'>id={r['id']}<br>dur={r['duration']}s</div></td>")
        if r["anchor"]:
            html.append(f"<td><img src='{r['anchor']}' alt='anchor'><div class='meta'>anchor</div></td>")
        else:
            html.append("<td class='missing'>no anchor</td>")
        if r["mid"]:
            html.append(f"<td><img src='{r['mid']}' alt='mid'><div class='meta'>mid frame</div></td>")
        else:
            html.append("<td class='missing'>no mid</td>")
        if r["clip"]:
            html.append(f"<td><video src='{r['clip']}' controls muted loop preload='none'></video></td>")
        else:
            html.append("<td class='missing'>no clip</td>")
        html.append("</tr>")
    html.append("</table></body></html>")

    out_html = os.path.join(QA_DIR, "contact_sheet.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write("".join(html))

    print(f"wrote {out_html}")
    print(f"  {sum(1 for r in rows if r['has_clip'])}/{len(rows)} shots with clips")
    print(f"  {sum(1 for r in rows if r['has_anchor'])}/{len(rows)} shots with anchors")


if __name__ == "__main__":
    main()
