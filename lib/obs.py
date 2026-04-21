"""
LUMN Studio — lightweight observability.

In-process request metrics + structured logs. Zero external dependencies.
Goal: give us the bare minimum of production visibility (error rate,
latency, hit count per endpoint) without spinning up Prometheus/Grafana.

Exported functions:
  log_request(method, path, status, latency_ms, user_id)
  snapshot() -> dict          # for GET /api/metrics

Metric shape:
  {
    "uptime_s": 1234.5,
    "total_requests": 4091,
    "by_endpoint": {
      "POST /api/v6/anchor/generate": {
        "count": 12,
        "errors": 1,
        "p50_ms": 4123,
        "p95_ms": 8210,
        "last_ts": 1734567890.12
      }
    }
  }

Latency is tracked with a bounded ring-buffer per endpoint (last 200
samples) so the memory footprint stays flat regardless of traffic.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import defaultdict, deque
from typing import Any

_START_TS = time.time()
_LOCK = threading.Lock()
_TOTAL_REQUESTS = 0
_SAMPLES: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
_COUNTS: dict[str, int] = defaultdict(int)
_ERRORS: dict[str, int] = defaultdict(int)
_LAST_TS: dict[str, float] = defaultdict(float)


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

def structured_log(level: str, msg: str, **fields: Any) -> None:
    """Emit a JSON line to stdout. Safe for multi-threaded use.

    Keep this cheap — no timestamps need millisecond precision, no
    thread IDs needed unless debugging; add as kwargs only.
    """
    record = {"t": int(time.time()), "lvl": level, "msg": msg, **fields}
    try:
        line = json.dumps(record, default=str)
    except Exception:
        line = f'{{"t":{int(time.time())},"lvl":"{level}","msg":"log_err"}}'
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Request metrics
# ---------------------------------------------------------------------------

def log_request(method: str, path: str, status: int, latency_ms: float,
                user_id: int | None = None) -> None:
    """Record a single request. Collapse all paths with path params to a
    canonical key so we don't cardinality-explode on per-shot endpoints.
    """
    global _TOTAL_REQUESTS
    key = _canonical_key(method, path)
    with _LOCK:
        _TOTAL_REQUESTS += 1
        _SAMPLES[key].append(float(latency_ms))
        _COUNTS[key] += 1
        if status >= 500 or status == 0:
            _ERRORS[key] += 1
        _LAST_TS[key] = time.time()
    # Also emit a structured log line for anything slow or errored.
    if status >= 500 or latency_ms > 5000:
        structured_log("warn", "slow_or_error_request",
                       method=method, path=path, status=status,
                       latency_ms=int(latency_ms), user_id=user_id)


def _canonical_key(method: str, path: str) -> str:
    """Collapse path params. We only track a handful of known patterns so
    cardinality stays bounded."""
    p = path.split("?", 1)[0]
    # Collapse /u_123/ and /{shot_id}/
    parts = p.split("/")
    canon: list[str] = []
    for part in parts:
        if part.startswith("u_") and part[2:].isdigit():
            canon.append("u_*")
        elif len(part) >= 20 and all(c in "abcdef0123456789-" for c in part):
            canon.append(":hash")
        elif part.isdigit() and len(part) > 2:
            canon.append(":id")
        else:
            canon.append(part)
    return f"{method} {'/'.join(canon)}"


def _percentile(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    k = int(round((len(s) - 1) * p))
    return s[k]


def snapshot() -> dict:
    """Return the current metrics snapshot. Safe to serialize as JSON."""
    with _LOCK:
        out = {
            "uptime_s": round(time.time() - _START_TS, 1),
            "total_requests": _TOTAL_REQUESTS,
            "by_endpoint": {},
        }
        for key, samples in _SAMPLES.items():
            s = list(samples)
            out["by_endpoint"][key] = {
                "count": _COUNTS[key],
                "errors": _ERRORS[key],
                "p50_ms": round(_percentile(s, 0.5), 1),
                "p95_ms": round(_percentile(s, 0.95), 1),
                "max_ms": round(max(s), 1) if s else 0.0,
                "last_ts": _LAST_TS[key],
            }
        return out


def reset() -> None:
    """Reset all metrics. Admin-only."""
    global _TOTAL_REQUESTS
    with _LOCK:
        _TOTAL_REQUESTS = 0
        _SAMPLES.clear()
        _COUNTS.clear()
        _ERRORS.clear()
        _LAST_TS.clear()
