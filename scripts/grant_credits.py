"""
Grant credits to a beta user.

Usage:
    python scripts/grant_credits.py <email> <dollars> [--reason beta_invite]
    python scripts/grant_credits.py --list
    python scripts/grant_credits.py --who <email>

Examples:
    python scripts/grant_credits.py friend@example.com 20
    python scripts/grant_credits.py friend@example.com 20 --reason topup
    python scripts/grant_credits.py --list
    python scripts/grant_credits.py --who friend@example.com

Grants are recorded in the ledger as negative cost_cents so /api/cost-tracker
and global_spend_since() both exclude them from spend totals — only real
fal.ai / Anthropic charges count.
"""

from __future__ import annotations

import argparse
import os
import sys

# Ensure repo root is on sys.path so `import lib.db` works when invoked
# from any cwd.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import lib.db as db  # noqa: E402


def cmd_grant(email: str, dollars: float, reason: str) -> int:
    email = email.strip().lower()
    u = db.get_user_by_email(email)
    if not u:
        print(f"error: no user with email {email!r}")
        print("hint: have them sign up at /signin first, then grant.")
        return 1
    cents = int(round(dollars * 100))
    if cents <= 0:
        print("error: amount must be positive")
        return 1
    before = int(u["credits_cents"])
    new_balance = db.grant_credits(u["id"], cents, reason=reason)
    print(f"ok  user={email}  granted=${dollars:.2f}  "
          f"balance: ${before/100:.2f} -> ${new_balance/100:.2f}")
    return 0


def cmd_list() -> int:
    db.init_db()
    with db._conn() as c:
        rows = c.execute(
            "SELECT id, email, credits_cents, role, created_at "
            "FROM users ORDER BY id"
        ).fetchall()
    if not rows:
        print("(no users yet)")
        return 0
    print(f"{'id':>4}  {'email':<36}  {'balance':>10}  {'role':<7}")
    print("-" * 64)
    for r in rows:
        print(f"{r['id']:>4}  {r['email']:<36}  "
              f"${r['credits_cents']/100:>8.2f}  {r['role']:<7}")
    return 0


def cmd_who(email: str) -> int:
    email = email.strip().lower()
    u = db.get_user_by_email(email)
    if not u:
        print(f"no user with email {email!r}")
        return 1
    print(f"id:       {u['id']}")
    print(f"email:    {u['email']}")
    print(f"role:     {u['role']}")
    print(f"balance:  ${u['credits_cents']/100:.2f}")
    ledger = db.user_ledger(u["id"], limit=20)
    if ledger:
        print("\nrecent ledger (last 20):")
        for row in ledger:
            sign = "+" if row["cost_cents"] < 0 else "-"
            amt = abs(row["cost_cents"]) / 100
            print(f"  {row['ts']}  {sign}${amt:>6.2f}  {row['kind']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("email", nargs="?", help="target user email")
    p.add_argument("dollars", nargs="?", type=float, help="amount in dollars")
    p.add_argument("--reason", default="beta_invite", help="ledger reason tag")
    p.add_argument("--list", action="store_true", help="list all users")
    p.add_argument("--who", metavar="EMAIL", help="show one user + recent ledger")
    args = p.parse_args()

    if args.list:
        return cmd_list()
    if args.who:
        return cmd_who(args.who)
    if not args.email or args.dollars is None:
        p.print_help()
        return 1
    return cmd_grant(args.email, args.dollars, args.reason)


if __name__ == "__main__":
    raise SystemExit(main())
