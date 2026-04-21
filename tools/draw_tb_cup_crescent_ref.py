"""Draw a deterministic cup-crescent TB identity reference.

Outputs a 1024x1024 PNG showing a chibi bear head silhouette with a
white/silver crescent moon on the forehead in CUP orientation — both horns
pointing straight UP (vertical, parallel), concave opening faces the sky,
convex curve rests against the brow.

Used as the TB reference photo so Gemini's stylize+sheet pass inherits the
correct orientation (visual prior defeats text instructions).
"""
from PIL import Image, ImageDraw, ImageFilter
import os

SIZE = 1024
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "output", "projects", "default", "prompt_os",
                   "tb_tips_up_reference.png")

bg_color = (28, 32, 44, 255)
fur_color = (22, 28, 48, 255)
fur_highlight = (40, 50, 80, 255)
eye_color = (240, 60, 50, 255)
eye_glow = (255, 140, 100, 255)
emblem_color = (235, 240, 248, 255)
emblem_glow = (210, 225, 245, 255)

img = Image.new("RGBA", (SIZE, SIZE), bg_color)

# Soft radial gradient background
grad = Image.new("RGBA", (SIZE, SIZE), bg_color)
gd = ImageDraw.Draw(grad)
for r in range(SIZE // 2, 0, -20):
    alpha = int(30 * (r / (SIZE / 2)))
    gd.ellipse([SIZE // 2 - r, SIZE // 2 - r, SIZE // 2 + r, SIZE // 2 + r],
               fill=(80, 90, 130, alpha))
img = Image.alpha_composite(img, grad)

d = ImageDraw.Draw(img)

# Chibi bear head silhouette (large round head, round ears)
cx, cy = SIZE // 2, SIZE // 2 + 40
head_r = 360
# Ears (two small circles on top of head)
ear_r = 110
d.ellipse([cx - head_r + 20, cy - head_r - 40, cx - head_r + 20 + ear_r * 2,
           cy - head_r - 40 + ear_r * 2], fill=fur_color)
d.ellipse([cx + head_r - 20 - ear_r * 2, cy - head_r - 40, cx + head_r - 20,
           cy - head_r - 40 + ear_r * 2], fill=fur_color)
# Inner ear tint
inner_ear_r = 55
d.ellipse([cx - head_r + 75, cy - head_r + 15,
           cx - head_r + 75 + inner_ear_r * 2,
           cy - head_r + 15 + inner_ear_r * 2], fill=fur_highlight)
d.ellipse([cx + head_r - 75 - inner_ear_r * 2, cy - head_r + 15,
           cx + head_r - 75, cy - head_r + 15 + inner_ear_r * 2],
          fill=fur_highlight)

# Head
d.ellipse([cx - head_r, cy - head_r, cx + head_r, cy + head_r], fill=fur_color)

# Eyes (red glow)
eye_r = 60
eye_y = cy + 30
eye_dx = 140
for side in (-1, 1):
    ex = cx + side * eye_dx
    # Glow
    for gr, ga in [(90, 40), (75, 70), (60, 120)]:
        d.ellipse([ex - gr, eye_y - gr, ex + gr, eye_y + gr],
                  fill=(*eye_glow[:3], ga))
    d.ellipse([ex - eye_r, eye_y - eye_r, ex + eye_r, eye_y + eye_r],
              fill=eye_color)
    # Pupil highlight
    d.ellipse([ex - 15, eye_y - 35, ex + 5, eye_y - 15],
              fill=(255, 230, 220, 255))

# Muzzle
muz_w, muz_h = 180, 120
muz_y = cy + 170
d.ellipse([cx - muz_w, muz_y - muz_h // 2, cx + muz_w, muz_y + muz_h // 2],
          fill=fur_highlight)
# Nose
nose_r = 30
d.ellipse([cx - nose_r, muz_y - 50, cx + nose_r, muz_y - 50 + nose_r * 2],
          fill=(12, 14, 20, 255))

# FOREHEAD CRESCENT — cup orientation, tips STRAIGHT UP
# Outer circle center slightly below forehead, inner circle offset upward to
# create an upward-opening crescent with both horns pointing vertically.
em_cx = cx
em_cy = cy - 180  # forehead position
outer_r = 140
inner_r = 125
inner_offset = 60  # shift inner circle UP (smaller y) to open the crescent upward

mask = Image.new("L", (SIZE, SIZE), 0)
md = ImageDraw.Draw(mask)
# Outer disc
md.ellipse([em_cx - outer_r, em_cy - outer_r, em_cx + outer_r, em_cy + outer_r],
           fill=255)
# Subtract inner disc (offset upward)
inner_cy = em_cy - inner_offset
md.ellipse([em_cx - inner_r, inner_cy - inner_r,
            em_cx + inner_r, inner_cy + inner_r], fill=0)

# Create glow layer (blurred copy of mask, scaled)
glow_mask = mask.filter(ImageFilter.GaussianBlur(radius=18))
glow_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
glow_layer.putalpha(glow_mask)
# Tint glow
glow_colored = Image.new("RGBA", (SIZE, SIZE), emblem_glow)
glow_colored.putalpha(glow_mask)
img = Image.alpha_composite(img, glow_colored)

# Solid emblem
emblem_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
el = ImageDraw.Draw(emblem_layer)
el.ellipse([em_cx - outer_r, em_cy - outer_r, em_cx + outer_r, em_cy + outer_r],
           fill=emblem_color)
el.ellipse([em_cx - inner_r, inner_cy - inner_r,
            em_cx + inner_r, inner_cy + inner_r], fill=(0, 0, 0, 0))
# Apply mask to keep only the crescent region
emblem_layer.putalpha(mask)
img = Image.alpha_composite(img, emblem_layer)

# Label
d2 = ImageDraw.Draw(img)
try:
    from PIL import ImageFont
    font = ImageFont.truetype("arial.ttf", 28)
except Exception:
    font = ImageFont.load_default()
label = "TB — tips STRAIGHT UP (cup orientation)"
d2.text((20, SIZE - 50), label, fill=(220, 230, 240, 200), font=font)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
img.convert("RGB").save(OUT, "PNG", optimize=True)
print(f"[OK] wrote {OUT}")
print(f"[OK] size: {os.path.getsize(OUT)} bytes")
