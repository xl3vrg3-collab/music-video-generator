"""
LUMN Studio — async job queue.

Bounded thread pool with per-job timeout, single retry on transient errors,
and persistent state in the `jobs` SQLite table.

The HTTP request handler enqueues a job and returns a `job_id` immediately.
The browser polls (or subscribes to SSE) for status. The worker thread runs
the actual fal.ai / Sonnet calls — so a 60-second Kling stall doesn't pin
an HTTP request thread, doesn't trigger the client's read timeout, and
doesn't block other users.

Usage from a request handler:

    job_id = enqueue("v6_anchor", user_id=42,
                     payload={"prompt": "...", ...},
                     run_fn=_run_anchor_job)
    return self._send_json({"ok": True, "job_id": job_id, "status": "queued"})

The worker reads the job from the DB to get the payload, runs `run_fn`,
and stores the result via `lib.db.update_job(...)`. The `run_fn` is given
the job_id and the payload; it should return a result dict on success or
raise on failure.
"""

from __future__ import annotations

import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable, Optional

import lib.db as db

# Bounded so a thundering herd can't OOM the box.
MAX_WORKERS = 4
JOB_TIMEOUT_SECONDS = 180  # hard ceiling per job
RETRY_DELAY_SECONDS = 2

_executor: Optional[ThreadPoolExecutor] = None
_lock = threading.Lock()
_runners: dict[str, Callable[[str, dict], dict]] = {}


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=MAX_WORKERS,
                    thread_name_prefix="lumn-worker",
                )
    return _executor


def register_runner(kind: str, fn: Callable[[str, dict], dict]) -> None:
    """Register a runner for a given job kind. The runner is called with
    (job_id, payload_dict) and returns a result dict. Raise on failure."""
    _runners[kind] = fn


def progress(job_id: str, stage: str, pct: int) -> None:
    """Helper for runners to report progress mid-flight."""
    db.update_job(job_id, stage=stage, progress=max(0, min(100, int(pct))))


def _refund_if_reserved(job_id: str, kind: str, payload: dict, reason: str) -> None:
    """Centralized refund for pre-reserved credits. Safe to call once per
    terminal failure — idempotent at the caller level (only called from
    _run_with_guard on terminal failure paths, never from runners)."""
    try:
        if payload.get("reserved") and int(payload.get("user_id", 0) or 0) > 0:
            db.refund_credits(
                int(payload["user_id"]),
                int(payload.get("cost_cents", 0) or 0),
                f"refund_{kind}",
                {"reason": reason, "job_id": job_id},
            )
    except Exception:
        pass


def enqueue(kind: str, user_id: int, payload: dict) -> str:
    """Create a job row, schedule it for execution, return the job_id."""
    if kind not in _runners:
        raise ValueError(f"no runner registered for kind={kind}")
    job_id = secrets.token_hex(12)
    db.create_job(job_id, user_id=user_id, kind=kind, input_obj=payload)
    fut: Future = _get_executor().submit(_run_with_guard, job_id, kind, payload)
    # Detach — we don't await the future. Errors land in the DB row.
    fut.add_done_callback(lambda f: _on_done(job_id, f))
    return job_id


def _on_done(job_id: str, fut: Future) -> None:
    # The worker itself records success/failure; this is just for unhandled
    # exceptions that escaped the guard.
    exc = fut.exception()
    if exc is not None:
        try:
            db.update_job(job_id, status="failed",
                          error=f"unhandled: {exc!r}")
        except Exception:
            pass


def _run_with_guard(job_id: str, kind: str, payload: dict) -> None:
    """Execute a runner with timeout via a watchdog thread.

    Python has no native cooperative cancellation, so we can't actually
    kill a stuck thread. What we DO is mark the job as failed so the user
    sees a clean error, then leave the orphaned thread to die when the
    underlying socket finally times out. Setting JOB_TIMEOUT_SECONDS lower
    than fal.ai's own timeout keeps this from accumulating forever.
    """
    runner = _runners[kind]
    db.update_job(job_id, status="running", stage="starting", progress=5)

    done = threading.Event()
    result_box: dict[str, Any] = {}

    def _exec():
        try:
            result_box["ok"] = runner(job_id, payload)
        except Exception as e:
            result_box["err"] = e
            result_box["tb"] = traceback.format_exc()
        finally:
            done.set()

    t = threading.Thread(target=_exec, name=f"lumn-job-{job_id}", daemon=True)
    t.start()
    finished = done.wait(timeout=JOB_TIMEOUT_SECONDS)

    if not finished:
        db.update_job(
            job_id, status="failed", stage="timeout",
            error=f"job exceeded {JOB_TIMEOUT_SECONDS}s timeout",
        )
        # SECURITY (H3/H4): refund any pre-reserved credits on timeout.
        # The orphan worker thread can't refund itself (no cooperative cancel).
        _refund_if_reserved(job_id, kind, payload, "timeout")
        return

    if "err" in result_box:
        # One retry on transient-looking errors.
        err = result_box["err"]
        msg = str(err).lower()
        is_transient = any(s in msg for s in
                           ("timeout", "connection", "503", "502", "504",
                            "rate limit", "temporarily"))
        if is_transient:
            time.sleep(RETRY_DELAY_SECONDS)
            db.update_job(job_id, stage="retrying", progress=10)
            try:
                result = runner(job_id, payload)
                db.update_job(job_id, status="done", stage="done",
                              progress=100, result=result)
                return
            except Exception as e2:
                db.update_job(job_id, status="failed", stage="failed",
                              error=f"retry failed: {e2!r}")
                _refund_if_reserved(job_id, kind, payload, f"retry_failed:{e2}")
                return
        db.update_job(job_id, status="failed", stage="failed",
                      error=f"{err!r}\n{result_box.get('tb','')[:1500]}")
        _refund_if_reserved(job_id, kind, payload, f"failed:{err}")
        return

    db.update_job(job_id, status="done", stage="done",
                  progress=100, result=result_box.get("ok") or {})


def shutdown() -> None:
    global _executor
    with _lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=False)
            _executor = None
