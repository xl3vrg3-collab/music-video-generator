"""
Show beta feedback collected from /api/feedback.

Usage:
    python scripts/show_feedback.py                # last 50, all categories
    python scripts/show_feedback.py --unresolved   # only unresolved
    python scripts/show_feedback.py --resolve 12   # mark id 12 resolved
    python scripts/show_feedback.py --limit 200
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import lib.db as db  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--unresolved", action="store_true")
    p.add_argument("--resolve", type=int, metavar="ID", help="mark feedback id as resolved")
    args = p.parse_args()

    if args.resolve:
        db.resolve_feedback(args.resolve)
        print(f"resolved feedback #{args.resolve}")
        return 0

    rows = db.list_feedback(limit=args.limit, unresolved_only=args.unresolved)
    if not rows:
        print("(no feedback yet)")
        return 0

    for r in rows:
        when = dt.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M")
        status = "OPEN" if not r["resolved"] else "DONE"
        who = r["email"] or f"user_id={r['user_id']}" or "anon"
        print(f"\n#{r['id']:>4}  [{status}]  {when}  {r['category']:<8}  {who}")
        if r["url"]:
            print(f"      url: {r['url']}")
        msg_lines = (r["message"] or "").strip().splitlines()
        for line in msg_lines:
            print(f"      {line}")
    print(f"\n{len(rows)} feedback row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
