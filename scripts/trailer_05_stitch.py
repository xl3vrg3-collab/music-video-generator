"""Bear trailer — Step 5: stitch with climax v3 (real moon + L→R wipe).

Climax v3 (post-user feedback on fake moon + fake color/size):
  Shot D is the base THROUGHOUT the climax. Phase 1: Shot D is blacked out
  except a radial hole around the REAL moon on the bear's forehead (the
  moon's true color, at its true size and position — no PIL fakery). Phase
  2: a slow smoothright xfade sweeps left-to-right, revealing the full Shot
  D + LUMN wordmark together. Each LUMN letter lights up naturally as the
  wipe front passes under it. Phase 3: hold full LUMN + bear. Phase 4:
  fade to black. Phase 5: COMING SOON card.

Structure (~16.0s):
  [A     4.5s]  push-in on white bear
  [B     4.5s]  orbit reveal of cinema rig
  [CLIMAX 5.0s] fade-in moon-only → slow L→R wipe → bear+LUMN → fade out
  [END   2.0s]  COMING SOON card

Assets built:
  trailer_lumn_wordmark.png  — full LUMN (Inter Tight Black)
  trailer_moon_mask.png      — radial white hole at real moon, black elsewhere
  trailer_coming_soon.png    — end card
  trailer_shot_d_first.png   — first frame (used to find real moon position)

Output: public/trailer.mp4
"""
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))
A = os.path.join(PUBLIC, "trailer_shot_a.mp4")
B = os.path.join(PUBLIC, "trailer_shot_b.mp4")
D = os.path.join(PUBLIC, "trailer_shot_d.mp4")
D_FIRST = os.path.join(PUBLIC, "trailer_shot_d_first.png")

WORDMARK = os.path.join(PUBLIC, "trailer_lumn_wordmark.png")
MOON_MASK = os.path.join(PUBLIC, "trailer_moon_mask.png")
COMING_SOON = os.path.join(PUBLIC, "trailer_coming_soon.png")
OUT = os.path.join(PUBLIC, "trailer.mp4")

INTER_TIGHT = os.path.join(PUBLIC, "fonts", "InterTight-Bold.ttf")

W, H = 1928, 1072
FPS = 24


def _amber_gradient(canvas_w, canvas_h):
    top = (255, 240, 200)
    mid = (242, 182, 102)
    bot = (222, 158, 78)
    grad = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grad)
    for yy in range(canvas_h):
        t = yy / max(1, canvas_h - 1)
        if t < 0.55:
            u = t / 0.55
            r = int(top[0] * (1 - u) + mid[0] * u)
            g = int(top[1] * (1 - u) + mid[1] * u)
            b = int(top[2] * (1 - u) + mid[2] * u)
        else:
            u = (t - 0.55) / 0.45
            r = int(mid[0] * (1 - u) + bot[0] * u)
            g = int(mid[1] * (1 - u) + bot[1] * u)
            b = int(mid[2] * (1 - u) + bot[2] * u)
        gdraw.line([(0, yy), (canvas_w, yy)], fill=(r, g, b, 255))
    return grad


def build_wordmark():
    font_size = 260
    font = ImageFont.truetype(INTER_TIGHT, font_size)
    font.set_variation_by_name("Black")
    text = "LUMN"

    tmp = Image.new("RGBA", (1, 1))
    tdraw = ImageDraw.Draw(tmp)
    bbox = tdraw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pad = 100
    canvas_w = text_w + pad * 2
    canvas_h = text_h + pad * 2

    grad = _amber_gradient(canvas_w, canvas_h)

    mask = Image.new("L", (canvas_w, canvas_h), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=255)

    layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    layer.paste(grad, (0, 0), mask)

    glow_big = layer.filter(ImageFilter.GaussianBlur(radius=70))
    ga = glow_big.split()[3].point(lambda p: int(p * 0.45))
    glow_big.putalpha(ga)
    glow_tight = layer.filter(ImageFilter.GaussianBlur(radius=18))
    gt = glow_tight.split()[3].point(lambda p: int(p * 0.55))
    glow_tight.putalpha(gt)

    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    x0 = (W - canvas_w) // 2
    y0 = int(H * 0.21) - canvas_h // 2
    frame.paste(glow_big, (x0, y0), glow_big)
    frame.paste(glow_tight, (x0, y0), glow_tight)
    frame.paste(layer, (x0, y0), layer)
    frame.save(WORDMARK)
    print(f"[WORDMARK] saved {WORDMARK} ({canvas_w}x{canvas_h}) at frame ({x0},{y0})")


def extract_shot_d_first():
    if os.path.isfile(D_FIRST):
        return
    cmd = ["ffmpeg", "-y", "-i", D, "-vframes", "1", D_FIRST]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"[D_FIRST] extracted {D_FIRST}")


def find_moon_center():
    img = Image.open(D_FIRST).convert("RGB")
    arr = np.array(img)
    R = arr[..., 0].astype(np.int32)
    G = arr[..., 1].astype(np.int32)
    B = arr[..., 2].astype(np.int32)
    warmth = (R + G) - 2 * B
    bright = (R + G + B) / 3
    score = warmth * (bright > 100)
    score[int(H * 0.7):] = 0
    score[: int(H * 0.10)] = 0
    flat = score.flatten()
    idx = int(np.argmax(flat))
    my, mx = idx // W, idx % W
    print(f"[MOON] center x={mx} y={my}  ({mx/W:.3f}W, {my/H:.3f}H)")
    return mx, my


def build_moon_mask(moon_x, moon_y):
    """Grayscale mask: feathered white hole at real moon, black elsewhere.

    When multiplied with Shot D, this leaves the real moon visible (with
    its true color, at its true size) and darkens everything else to black.
    """
    hole_r = 115
    feather = 130
    y_idx, x_idx = np.ogrid[:H, :W]
    dist = np.sqrt((x_idx - moon_x) ** 2 + (y_idx - moon_y) ** 2)
    mask = np.clip(1.0 - (dist - hole_r) / feather, 0.0, 1.0)
    arr = (mask * 255).astype(np.uint8)
    rgb = np.stack([arr, arr, arr], axis=-1)
    img = Image.fromarray(rgb, mode="RGB")
    img.save(MOON_MASK)
    print(
        f"[MOON_MASK] saved {MOON_MASK} "
        f"(center=({moon_x},{moon_y}) hole_r={hole_r} feather={feather})"
    )


def build_coming_soon():
    font_size = 62
    font = ImageFont.truetype(INTER_TIGHT, font_size)
    font.set_variation_by_name("Bold")
    text = "COMING  SOON"

    tmp = Image.new("RGBA", (1, 1))
    tdraw = ImageDraw.Draw(tmp)
    bbox = tdraw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pad = 50
    canvas_w = text_w + pad * 2
    canvas_h = text_h + pad * 2

    layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    ldraw.text(
        (pad - bbox[0], pad - bbox[1]),
        text,
        font=font,
        fill=(250, 236, 208, 255),
    )

    glow = layer.filter(ImageFilter.GaussianBlur(radius=18))
    ga = glow.split()[3].point(lambda p: int(p * 0.55))
    glow.putalpha(ga)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    x = (W - canvas_w) // 2
    y = int(H * 0.50) - canvas_h // 2
    img.paste(glow, (x, y), glow)
    img.paste(layer, (x, y), layer)
    img.save(COMING_SOON)
    print(f"[COMING_SOON] saved {COMING_SOON}")


def build_trailer():
    A_DUR = 4.5
    B_DUR = 4.5
    CLIMAX_DUR = 5.0
    END_DUR = 2.0
    total = A_DUR + B_DUR + CLIMAX_DUR + END_DUR

    climax_fade_in = 0.5
    climax_fade_out = 0.5
    wipe_offset = 1.3
    wipe_dur = 2.5

    cs_in = 0.45
    cs_fade_out_start = 0.45
    cs_fade_dur = 0.4
    final_fade = 0.55

    filt = (
        f"[0:v]trim=0:{A_DUR},setpts=PTS-STARTPTS,format=yuv420p[a];"
        f"[1:v]trim=0:{B_DUR},setpts=PTS-STARTPTS,format=yuv420p[b];"
        f"[2:v]trim=0:{CLIMAX_DUR},setpts=PTS-STARTPTS,format=rgb24,split=2[d_a_src][d_b_src];"
        f"[4:v]format=rgb24[mmask];"
        f"[d_a_src][mmask]blend=all_mode=multiply,"
        f"fade=in:st=0:d={climax_fade_in}:color=black,"
        f"fps={FPS},settb=AVTB[d_moon];"
        f"[d_b_src][5:v]overlay=0:0,"
        f"fade=in:st=0:d={climax_fade_in}:color=black,"
        f"fps={FPS},settb=AVTB[d_full];"
        f"[d_moon][d_full]xfade=transition=smoothright:duration={wipe_dur}:offset={wipe_offset}[climax_raw];"
        f"[climax_raw]trim=0:{CLIMAX_DUR},setpts=PTS-STARTPTS,"
        f"fade=out:st={CLIMAX_DUR - climax_fade_out}:d={climax_fade_out}:color=black,"
        f"format=yuv420p[climax];"
        f"[3:v]trim=0:{END_DUR},setpts=PTS-STARTPTS[blk_end];"
        f"[6:v]format=rgba,"
        f"fade=in:st={cs_in}:d={cs_fade_dur}:alpha=1,"
        f"fade=out:st={END_DUR - final_fade - cs_fade_out_start}:d={cs_fade_out_start}:alpha=1[cs];"
        f"[blk_end][cs]overlay=0:0[blk_end_cs_raw];"
        f"[blk_end_cs_raw]fade=out:st={END_DUR - final_fade}:d={final_fade}:color=black,format=yuv420p[blk_end_cs];"
        f"[a][b][climax][blk_end_cs]concat=n=4:v=1:a=0[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", A,                                                                          # 0
        "-i", B,                                                                          # 1
        "-i", D,                                                                          # 2
        "-f", "lavfi", "-t", f"{END_DUR + 0.1}", "-i", f"color=c=black:s={W}x{H}:r={FPS}",  # 3
        "-loop", "1", "-t", f"{CLIMAX_DUR + 0.1}", "-i", MOON_MASK,                        # 4
        "-loop", "1", "-t", f"{CLIMAX_DUR + 0.1}", "-i", WORDMARK,                         # 5
        "-loop", "1", "-t", f"{END_DUR + 0.1}", "-i", COMING_SOON,                         # 6
        "-filter_complex", filt,
        "-map", "[v]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "slow",
        "-crf", "17",
        "-r", str(FPS),
        "-movflags", "+faststart",
        OUT,
    ]

    print(f"[STITCH] running ffmpeg... total={total:.1f}s")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("[STITCH] FFMPEG FAILED:")
        print(res.stderr[-3500:])
        sys.exit(1)
    mb = os.path.getsize(OUT) / (1024 * 1024)
    print(f"[STITCH] saved -> {OUT} ({mb:.1f}MB, {total:.1f}s)")


def main():
    for f in (A, B, D):
        if not os.path.isfile(f):
            print(f"[STITCH] missing: {f}")
            return
    if not os.path.isfile(INTER_TIGHT):
        print(f"[STITCH] missing font: {INTER_TIGHT}")
        return
    extract_shot_d_first()
    moon_x, moon_y = find_moon_center()
    build_wordmark()
    build_moon_mask(moon_x, moon_y)
    build_coming_soon()
    build_trailer()


if __name__ == "__main__":
    main()
