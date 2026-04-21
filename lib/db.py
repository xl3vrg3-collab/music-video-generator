"""
LUMN Studio — SQLite persistence layer.

Provides the minimum multi-tenant primitives we need to move off of
single-global JSON files without yanking the entire server into an ORM:

  - users       : id, email, password_hash (scrypt), credits_cents, role, created_at
  - sessions    : id (random 32 bytes hex), user_id, expires_at
  - ledger      : per-user spend log (kind, cost_cents, meta_json, ts)
  - projects    : id, user_id, name, created_at
  - rate_limits : sliding window per-user (user_id, kind, ts) for hourly caps

The DB file lives at `output/lumn.db` by default. All functions are thread
safe via a per-call connection (SQLite handles short connections cheaply
and we don't need long-lived transactions for this workload).

Designed to be imported by server.py lazily — zero hard deps beyond stdlib.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from typing import Any, Optional

DB_PATH = os.environ.get(
    "LUMN_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "output", "lumn.db"),
)

SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  email           TEXT UNIQUE NOT NULL,
  password_hash   TEXT NOT NULL,
  credits_cents   INTEGER NOT NULL DEFAULT 0,
  role            TEXT NOT NULL DEFAULT 'user',
  created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id          TEXT PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at  INTEGER NOT NULL,
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS ledger (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,
  cost_cents  INTEGER NOT NULL,
  meta_json   TEXT,
  ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user_ts ON ledger(user_id, ts);

CREATE TABLE IF NOT EXISTS projects (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);

CREATE TABLE IF NOT EXISTS rate_limits (
  user_id  INTEGER NOT NULL,
  kind     TEXT NOT NULL,
  ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rl_user_kind_ts ON rate_limits(user_id, kind, ts);

CREATE TABLE IF NOT EXISTS feedback (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER,
  email       TEXT,
  category    TEXT NOT NULL,
  message     TEXT NOT NULL,
  context     TEXT,
  user_agent  TEXT,
  url         TEXT,
  ts          INTEGER NOT NULL,
  resolved    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback(ts DESC);

CREATE TABLE IF NOT EXISTS shot_ratings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL,
  shot_id     TEXT NOT NULL,
  asset_path  TEXT,
  prompt      TEXT,
  rating      INTEGER NOT NULL,
  reason      TEXT,
  meta_json   TEXT,
  ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ratings_user_ts ON shot_ratings(user_id, ts DESC);

CREATE TABLE IF NOT EXISTS jobs (
  id           TEXT PRIMARY KEY,
  user_id      INTEGER NOT NULL,
  kind         TEXT NOT NULL,
  status       TEXT NOT NULL,
  stage        TEXT,
  progress     INTEGER NOT NULL DEFAULT 0,
  input_json   TEXT,
  result_json  TEXT,
  error        TEXT,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, updated_at);
"""


def init_db() -> None:
    """Create tables on first use. Safe to call repeatedly."""
    with _conn() as c:
        c.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Password hashing (scrypt — stdlib, no extra deps)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, key_hex = stored.split("$", 2)
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expect = bytes.fromhex(key_hex)
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got, expect)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(email: str, password: str, credits_cents: int = 500, role: str = "user") -> int:
    """Create a user. Returns new user_id. Raises ValueError on duplicate email."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("invalid email")
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    init_db()
    with _conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO users (email, password_hash, credits_cents, role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (email, hash_password(password), credits_cents, role, int(time.time())),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            raise ValueError("email already registered")


def get_user_by_email(email: str) -> Optional[dict]:
    init_db()
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email=?",
                        ((email or "").strip().lower(),)).fetchone()
        return dict(row) if row else None


def get_user(user_id: int) -> Optional[dict]:
    init_db()
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def authenticate(email: str, password: str) -> Optional[dict]:
    u = get_user_by_email(email)
    if not u:
        return None
    if not verify_password(password, u["password_hash"]):
        return None
    return u


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(user_id: int, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    init_db()
    sid = secrets.token_hex(32)
    now = int(time.time())
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions (id, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (sid, user_id, now + ttl_seconds, now),
        )
    return sid


def get_session_user(sid: str) -> Optional[dict]:
    if not sid:
        return None
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.id = ? AND s.expires_at > ?",
            (sid, int(time.time())),
        ).fetchone()
        return dict(row) if row else None


def destroy_session(sid: str) -> None:
    if not sid:
        return
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))


def purge_expired_sessions() -> int:
    init_db()
    with _conn() as c:
        cur = c.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Ledger + credits
# ---------------------------------------------------------------------------

def charge_user(user_id: int, cost_cents: int, kind: str, meta: Optional[dict] = None) -> bool:
    """Atomically debit the user's credits and append a ledger entry.

    Returns True on success, False if insufficient credits.
    """
    init_db()
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT credits_cents FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                c.execute("ROLLBACK")
                return False
            if int(row["credits_cents"]) < cost_cents:
                c.execute("ROLLBACK")
                return False
            c.execute(
                "UPDATE users SET credits_cents = credits_cents - ? WHERE id=?",
                (cost_cents, user_id),
            )
            c.execute(
                "INSERT INTO ledger (user_id, kind, cost_cents, meta_json, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, kind, cost_cents, json.dumps(meta or {}), int(time.time())),
            )
            c.execute("COMMIT")
            return True
        except Exception:
            c.execute("ROLLBACK")
            raise


def refund_credits(user_id: int, cents: int, kind: str = "refund",
                   meta: Optional[dict] = None) -> int:
    """Refund credits atomically with a ledger entry. Returns new balance.

    Used when a pre-reserved credit (charge_user at enqueue) needs to be
    returned because the worker failed. Logged as negative cost_cents so
    spend aggregations don't double-count.
    """
    init_db()
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("UPDATE users SET credits_cents = credits_cents + ? WHERE id=?",
                      (cents, user_id))
            c.execute(
                "INSERT INTO ledger (user_id, kind, cost_cents, meta_json, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, kind, -cents, json.dumps(meta or {"refund": True}), int(time.time())),
            )
            row = c.execute("SELECT credits_cents FROM users WHERE id=?", (user_id,)).fetchone()
            c.execute("COMMIT")
            return int(row["credits_cents"]) if row else 0
        except Exception:
            c.execute("ROLLBACK")
            raise


def grant_credits(user_id: int, cents: int, reason: str = "grant") -> int:
    """Add credits. Returns new balance."""
    init_db()
    with _conn() as c:
        c.execute("UPDATE users SET credits_cents = credits_cents + ? WHERE id=?",
                  (cents, user_id))
        c.execute(
            "INSERT INTO ledger (user_id, kind, cost_cents, meta_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, reason, -cents, json.dumps({"grant": True}), int(time.time())),
        )
        row = c.execute("SELECT credits_cents FROM users WHERE id=?", (user_id,)).fetchone()
        return int(row["credits_cents"]) if row else 0


def set_credits(user_id: int, cents: int, reason: str = "sync") -> int:
    """Overwrite a user's credits to `cents`. Logs the delta to the ledger so
    running spend aggregates stay consistent. Used by the fal-balance sync —
    LUMN per-user credits track the shared fal.ai account balance for now.
    Returns the new balance.
    """
    init_db()
    cents = max(0, int(cents))
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT credits_cents FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                c.execute("ROLLBACK")
                return 0
            prev = int(row["credits_cents"])
            delta = cents - prev
            c.execute("UPDATE users SET credits_cents=? WHERE id=?", (cents, user_id))
            c.execute(
                "INSERT INTO ledger (user_id, kind, cost_cents, meta_json, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, reason, -delta,
                 json.dumps({"sync": True, "prev": prev, "next": cents}),
                 int(time.time())),
            )
            c.execute("COMMIT")
            return cents
        except Exception:
            c.execute("ROLLBACK")
            raise


def user_ledger(user_id: int, limit: int = 100) -> list[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM ledger WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def user_spend_since(user_id: int, seconds_ago: int) -> int:
    """Sum of positive cost_cents in the last N seconds (excludes grants)."""
    init_db()
    since = int(time.time()) - seconds_ago
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_cents), 0) AS s FROM ledger "
            "WHERE user_id=? AND ts >= ? AND cost_cents > 0",
            (user_id, since),
        ).fetchone()
        return int(row["s"] or 0)


# ---------------------------------------------------------------------------
# Rate limits (sliding window)
# ---------------------------------------------------------------------------

def rate_limit_check(user_id: int, kind: str, max_per_hour: int) -> tuple[bool, int]:
    """Sliding-window rate limiter. Records the attempt if allowed.

    Returns (allowed, count_in_window).
    """
    init_db()
    now = int(time.time())
    cutoff = now - 3600
    with _conn() as c:
        c.execute("DELETE FROM rate_limits WHERE ts < ?", (cutoff,))
        cnt = c.execute(
            "SELECT COUNT(*) AS n FROM rate_limits WHERE user_id=? AND kind=? AND ts >= ?",
            (user_id, kind, cutoff),
        ).fetchone()
        n = int(cnt["n"] or 0)
        if n >= max_per_hour:
            return False, n
        c.execute(
            "INSERT INTO rate_limits (user_id, kind, ts) VALUES (?, ?, ?)",
            (user_id, kind, now),
        )
        return True, n + 1


def rate_limit_check_ip(ip: str, kind: str, max_per_hour: int) -> tuple[bool, int]:
    """IP-keyed rate limit for pre-auth endpoints (e.g. signup). Reuses the
    rate_limits table with a negative synthetic user_id derived from the IP
    so it can never collide with real user ids."""
    import zlib
    synthetic = -(zlib.crc32((ip or "unknown").encode("utf-8")) & 0x7FFFFFFF) - 1
    return rate_limit_check(synthetic, kind, max_per_hour)


def global_spend_since(seconds_ago: int) -> int:
    """Sum of all positive ledger charges (across every user) in the last
    N seconds. Used as a global circuit breaker against cost runaway."""
    init_db()
    since = int(time.time()) - seconds_ago
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_cents), 0) AS s FROM ledger "
            "WHERE ts >= ? AND cost_cents > 0",
            (since,),
        ).fetchone()
        return int(row["s"] or 0)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def create_project(user_id: int, name: str) -> int:
    init_db()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO projects (user_id, name, created_at) VALUES (?, ?, ?)",
            (user_id, name, int(time.time())),
        )
        return int(cur.lastrowid)


def list_projects(user_id: int) -> list[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Per-user filesystem namespacing helper
# ---------------------------------------------------------------------------

def user_output_root(user_id: int, base: str) -> str:
    """Return a user-scoped output directory under `base`. Creates it.

    Example:
      user_output_root(42, 'output/pipeline/anchors_v6')
      -> 'output/pipeline/anchors_v6/u_42'
    """
    p = os.path.join(base, f"u_{int(user_id)}")
    os.makedirs(p, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Feedback (in-app bug reports)
# ---------------------------------------------------------------------------

def insert_feedback(user_id: Optional[int], email: Optional[str],
                    category: str, message: str,
                    context: Optional[str] = None,
                    user_agent: Optional[str] = None,
                    url: Optional[str] = None) -> int:
    init_db()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO feedback (user_id, email, category, message, context, "
            "user_agent, url, ts) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, email, category[:40], message[:8000],
             (context or "")[:8000], (user_agent or "")[:300],
             (url or "")[:500], int(time.time())),
        )
        return int(cur.lastrowid)


def list_feedback(limit: int = 100, unresolved_only: bool = False) -> list[dict]:
    init_db()
    q = "SELECT * FROM feedback "
    if unresolved_only:
        q += "WHERE resolved = 0 "
    q += "ORDER BY ts DESC LIMIT ?"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, (limit,)).fetchall()]


def resolve_feedback(feedback_id: int) -> None:
    init_db()
    with _conn() as c:
        c.execute("UPDATE feedback SET resolved=1 WHERE id=?", (feedback_id,))


# ---------------------------------------------------------------------------
# Shot ratings (TI learning loop)
# ---------------------------------------------------------------------------

def insert_shot_rating(user_id: int, shot_id: str, rating: int,
                       asset_path: Optional[str] = None,
                       prompt: Optional[str] = None,
                       reason: Optional[str] = None,
                       meta: Optional[dict] = None) -> int:
    init_db()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO shot_ratings (user_id, shot_id, asset_path, prompt, "
            "rating, reason, meta_json, ts) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, shot_id[:200], (asset_path or "")[:1000],
             (prompt or "")[:4000], int(rating), (reason or "")[:1000],
             json.dumps(meta or {}), int(time.time())),
        )
        return int(cur.lastrowid)


def list_shot_ratings(user_id: Optional[int] = None, limit: int = 200) -> list[dict]:
    init_db()
    with _conn() as c:
        if user_id is None:
            rows = c.execute(
                "SELECT * FROM shot_ratings ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM shot_ratings WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Async jobs (worker queue)
# ---------------------------------------------------------------------------

def create_job(job_id: str, user_id: int, kind: str, input_obj: dict) -> None:
    init_db()
    now = int(time.time())
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, user_id, kind, status, stage, progress, "
            "input_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, user_id, kind, "queued", "queued", 0,
             json.dumps(input_obj), now, now),
        )


def update_job(job_id: str, *, status: Optional[str] = None,
               stage: Optional[str] = None, progress: Optional[int] = None,
               result: Optional[dict] = None, error: Optional[str] = None) -> None:
    init_db()
    sets, vals = [], []
    if status is not None:
        sets.append("status=?"); vals.append(status)
    if stage is not None:
        sets.append("stage=?"); vals.append(stage)
    if progress is not None:
        sets.append("progress=?"); vals.append(int(progress))
    if result is not None:
        sets.append("result_json=?"); vals.append(json.dumps(result))
    if error is not None:
        sets.append("error=?"); vals.append(error[:2000])
    sets.append("updated_at=?"); vals.append(int(time.time()))
    vals.append(job_id)
    with _conn() as c:
        c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", vals)


def get_job(job_id: str) -> Optional[dict]:
    init_db()
    with _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------

def list_users(limit: int = 200) -> list[dict]:
    init_db()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, email, credits_cents, role, created_at FROM users "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()]


def spend_summary(seconds_ago: int = 86400) -> dict:
    """Total spend + per-user spend in the last N seconds. Excludes grants."""
    init_db()
    since = int(time.time()) - seconds_ago
    with _conn() as c:
        total = int(c.execute(
            "SELECT COALESCE(SUM(cost_cents),0) FROM ledger WHERE ts>=? AND cost_cents>0",
            (since,),
        ).fetchone()[0] or 0)
        rows = c.execute(
            "SELECT u.email, SUM(l.cost_cents) AS spent, COUNT(*) AS n "
            "FROM ledger l JOIN users u ON u.id=l.user_id "
            "WHERE l.ts>=? AND l.cost_cents>0 GROUP BY u.id ORDER BY spent DESC LIMIT 50",
            (since,),
        ).fetchall()
        return {
            "total_cents": total,
            "by_user": [{"email": r["email"], "cents": int(r["spent"]), "count": int(r["n"])}
                        for r in rows],
            "window_seconds": seconds_ago,
        }


if __name__ == "__main__":
    # Smoke test
    init_db()
    print(f"DB ready at {DB_PATH}")
    print("Tables: users, sessions, ledger, projects, rate_limits, "
          "feedback, shot_ratings, jobs")
