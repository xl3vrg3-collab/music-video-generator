"""fal.ai real-account balance lookup.

The fal.ai billing endpoint requires an ADMIN key (regular keys get 403).
Users create admin keys at https://fal.ai/dashboard/keys and drop into
.env as FAL_ADMIN_KEY. If absent we fall back to FAL_API_KEY (may 403).
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

FAL_BILLING_URL = "https://api.fal.ai/v1/account/billing?expand=credits"

_cache: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 10.0  # seconds


def _admin_key() -> Optional[str]:
    return os.environ.get("FAL_ADMIN_KEY") or os.environ.get("FAL_API_KEY") or None


def get_fal_balance(force: bool = False) -> dict:
    """Return {ok, current_balance, currency, dollars, username?, error?}.

    Cached for _CACHE_TTL seconds to avoid hammering fal on UI polls.
    On auth/network error returns ok=False with an error string so the UI
    can render a dash rather than crash.
    """
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        return dict(_cache["data"])

    key = _admin_key()
    if not key:
        data = {"ok": False, "error": "no_key", "current_balance": None, "currency": None, "dollars": None}
        _cache.update(ts=now, data=data)
        return dict(data)

    req = urllib.request.Request(
        FAL_BILLING_URL,
        headers={"Authorization": f"Key {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        err = f"http_{e.code}"
        if e.code == 403:
            err = "not_admin_key"
        data = {"ok": False, "error": err, "detail": body[:300], "current_balance": None, "currency": None, "dollars": None}
        _cache.update(ts=now, data=data)
        return dict(data)
    except Exception as e:
        data = {"ok": False, "error": f"network:{e.__class__.__name__}", "current_balance": None, "currency": None, "dollars": None}
        _cache.update(ts=now, data=data)
        return dict(data)

    credits = payload.get("credits") or {}
    bal = credits.get("current_balance")
    cur = credits.get("currency") or "USD"
    # fal returns current_balance as a dollars float (per fal docs — not cents).
    dollars = None
    if isinstance(bal, (int, float)):
        dollars = float(bal)
    data = {
        "ok": True,
        "current_balance": bal,
        "currency": cur,
        "dollars": dollars,
        "username": payload.get("username"),
    }
    _cache.update(ts=now, data=data)
    return dict(data)


def sync_user_credits_to_fal(user_id: int) -> Optional[int]:
    """Set LUMN user.credits_cents to the current fal.ai balance (cents).

    Returns the new balance in cents on success, None when fal balance
    is not readable (missing admin key, 403, network error). The caller
    should use this as a best-effort sync; on failure, existing DB credits
    are preserved.
    """
    bal = get_fal_balance()
    if not bal.get("ok"):
        return None
    dollars = bal.get("dollars")
    if not isinstance(dollars, (int, float)):
        return None
    cents = int(round(float(dollars) * 100))
    try:
        from lib.db import set_credits
        return set_credits(user_id, cents, reason="fal_sync")
    except Exception:
        return None


if __name__ == "__main__":
    print(json.dumps(get_fal_balance(force=True), indent=2))
