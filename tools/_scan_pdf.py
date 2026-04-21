import re
with open("LUMN Manifesto.pdf", "rb") as f:
    data = f.read()
patterns = [b"Mathe", b"mathe", b"xl3vrg3", b"gmail", b"/Users/", b"file://",
            b"Author", b"Creator", b"Producer", b"Title", b"\\Users",
            b"Trillion", b"trillion", b"bear", b"Bear", b"u_14", b"u_",
            b"localhost", b"3849", b"botfarm", b"Solana", b".db",
            b"lumn.db", b"session", b"cookie", b"password"]
for p in patterns:
    hits = [m.start() for m in re.finditer(re.escape(p), data)][:5]
    if hits:
        print(f"{p!r:25s} -> {len(hits)} hit(s), first at {hits[0]}")
        s, e = max(0, hits[0] - 20), hits[0] + 80
        print(f"   ctx: {data[s:e]!r}")
    else:
        print(f"{p!r:25s} -> (none)")
