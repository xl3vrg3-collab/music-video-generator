"""Extract the IDENTITY MARK callout panel from the TB character sheet.

Sheet layout:
  - 3x3 grid of body views (1792 wide x ~1800 tall -> each ~597x~600)
  - Bottom row: FACE CLOSEUP (left half) + IDENTITY MARK (right half), ~600 tall
  - Total sheet: 1792 x 2400

Writes a clean square crop of the IDENTITY MARK panel to replace the stale
tips-right callout the auditor has been comparing against.
"""
from PIL import Image
import os

SRC = r"C:/Users/Mathe/lumn/output/projects/default/prompt_os/char_previews/6d31f281-4cc_full_1776697777.png"
DST = r"C:/Users/Mathe/lumn/output/prompt_os/previews/characters/6d31f281-4cc_callout_tips_up.png"

im = Image.open(SRC)
W, H = im.size
print(f"[SRC] {SRC} size={W}x{H}")

# Sheet has 3 grid rows of body views + 1 taller bottom row.
# Bottom row starts at ~3/4 height; IDENTITY MARK occupies right half of that row.
# Shift top down past the HEAD TILTED / BOWED row's label strip.
top = int(H * 0.78)    # ~1872 for 2400 (below preceding labels)
bottom = H            # 2400
left = int(W * 0.50)   # 896
right = W              # 1792

panel = im.crop((left, top, right, bottom))
PW, PH = panel.size
# Trim bottom 14% to remove "IDENTITY MARK - exact shape, color, placement" label
panel = panel.crop((0, 0, PW, int(PH * 0.86)))

os.makedirs(os.path.dirname(DST), exist_ok=True)
panel.save(DST, "PNG", optimize=True)
print(f"[OK] wrote {DST} size={panel.size}")
