"""Quick verification: login + call /api/v6/anchors + /api/v6/clips."""
import http.cookiejar
import json
import urllib.request

BASE = "http://127.0.0.1:3849"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

# seed cookies + CSRF
opener.open(BASE + "/").read()
csrf = next((c.value for c in cj if c.name == "lumn_csrf"), "")

# login
body = json.dumps({"email": "mctest@local", "password": "testpass123"}).encode()
req = urllib.request.Request(
    BASE + "/api/auth/login", data=body,
    headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
)
print("login:", opener.open(req).status)

# re-read csrf post-login
csrf = next((c.value for c in cj if c.name == "lumn_csrf"), csrf)

# anchors
req = urllib.request.Request(
    BASE + "/api/v6/anchors",
    headers={"X-CSRF-Token": csrf, "X-Lumn-Project": "default"},
)
data = json.loads(opener.open(req).read())
print(f"anchors returned: {len(data.get('anchors', []))}")
for a in data.get("anchors", []):
    print(f"  shot_id={a['shot_id']}  selected={a.get('selected') or '(none)'}")

# clips
req = urllib.request.Request(
    BASE + "/api/v6/clips",
    headers={"X-CSRF-Token": csrf, "X-Lumn-Project": "default"},
)
data = json.loads(opener.open(req).read())
print(f"clips returned: {len(data.get('clips', []))}")
for c in data.get("clips", []):
    print(f"  shot_id={c['shot_id']}  size={c['size_mb']}MB  url={c['url']}")
