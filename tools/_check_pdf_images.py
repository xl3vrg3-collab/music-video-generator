"""List the image objects embedded in LUMN Manifesto.pdf so we can tell
which welcome PNGs ended up in the file."""
import re
with open("LUMN Manifesto.pdf", "rb") as f:
    data = f.read()

# Each Image XObject has a /Subtype /Image ... /Width N /Height M line.
pattern = re.compile(rb'/Subtype\s*/Image[^>]*?/Width\s*(\d+)[^>]*?/Height\s*(\d+)', re.DOTALL)
hits = [(m.start(), int(m.group(1)), int(m.group(2))) for m in pattern.finditer(data)]
print(f"Found {len(hits)} embedded image(s):")
for off, w, h in hits:
    print(f"  offset {off:>10}  {w}x{h}")
