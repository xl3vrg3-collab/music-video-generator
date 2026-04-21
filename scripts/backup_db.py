r"""
Back up output/lumn.db to a timestamped file. Keeps the most recent N
backups; older ones are deleted.

Uses sqlite3's online backup API so it's safe to run while the server
is live (no file locking issues, no torn pages).

Usage:
    python scripts/backup_db.py                      # default: keep 14
    python scripts/backup_db.py --keep 30
    python scripts/backup_db.py --dest D:\backups    # custom destination

Schedule it on Windows:
    schtasks /Create /SC DAILY /TN "LUMN DB Backup" /TR ^
      "C:\Users\Mathe\AppData\Local\Programs\Python\Python311\python.exe ^
       C:\Users\Mathe\lumn\scripts\backup_db.py" /ST 03:00
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import sqlite3
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SRC = os.path.join(_REPO, "output", "lumn.db")
DEFAULT_DEST = os.path.join(_REPO, "output", "backups")


def backup(src: str, dest_dir: str) -> str:
    if not os.path.isfile(src):
        print(f"error: source DB not found at {src}")
        sys.exit(1)
    os.makedirs(dest_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(dest_dir, f"lumn_{stamp}.db")
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(out)
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()
    return out


def rotate(dest_dir: str, keep: int) -> int:
    files = sorted(glob.glob(os.path.join(dest_dir, "lumn_*.db")))
    deleted = 0
    while len(files) > keep:
        old = files.pop(0)
        try:
            os.remove(old)
            deleted += 1
        except OSError as e:
            print(f"warn: failed to delete {old}: {e}")
    return deleted


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", default=DEFAULT_SRC)
    p.add_argument("--dest", default=DEFAULT_DEST)
    p.add_argument("--keep", type=int, default=14)
    args = p.parse_args()

    out = backup(args.src, args.dest)
    size_kb = os.path.getsize(out) // 1024
    deleted = rotate(args.dest, args.keep)
    print(f"backed up: {out} ({size_kb} KB)")
    if deleted:
        print(f"rotated:   {deleted} old backup(s) deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
