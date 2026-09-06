"""
rate_limit.py — Firestore-backed rate limiter for the OCR endpoint.

Uses Firestore transactions for correctness across multiple Cloud Run instances.
A simple in-process counter would silently fail when requests hit different containers.

Limits (per user):
  - 5 calls / minute
  - 20 calls / hour
  - 50 calls / day

Global limit (all users combined):
  - 200 calls / day  (hard ceiling protecting project budget)

On limit exceeded: raises HTTPException 429 with Retry-After header.
On global cap exceeded: raises HTTPException 503 (hides the reason from caller).
"""

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Callable, TypeVar

from fastapi import HTTPException
from google.api_core.exceptions import InvalidArgument
from google.cloud import firestore

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Cold-start transaction-expired guard (issue #522)
#
# Firestore transactions have a 60s lifetime from when db.transaction() is
# created. On a cold-deployed container the very first request constructs
# the Firestore.Client(), performs ADC lookup + gRPC handshake, opens the
# transaction, reads, writes — all serially. If that sequence exceeds 60s
# the commit fails with InvalidArgument("transaction has expired or is no
# longer valid"). main.py's lifespan hook eagerly warms the client at
# container startup; this retry is a defense-in-depth in case the warmup
# is bypassed (local pytest, future code paths) or transiently fails.
# ---------------------------------------------------------------------------

_EXPIRED_TXN_MARKER = "transaction has expired"


def _run_txn_with_retry(
    db: firestore.Client,
    txn_fn: Callable[[firestore.Transaction, firestore.DocumentReference], T],
    ref: firestore.DocumentReference,
    *,
    label: str,
) -> T:
    """Run a transactional function, retrying once on cold-start expiry.

    Only retries on InvalidArgument whose message contains the marker string
    — other InvalidArgument errors (malformed field paths, invalid filters)
    propagate so real bugs surface.
    """
    try:
        return txn_fn(db.transaction(), ref)
    except InvalidArgument as e:
        if _EXPIRED_TXN_MARKER not in str(e):
            raise
        logger.warning(
            "rate_limit %s: transaction expired on first attempt — retrying once",
            label,
        )
        return txn_fn(db.transaction(), ref)

# ---------------------------------------------------------------------------
# Limits — can be overridden via environment variables set at deploy time
# ---------------------------------------------------------------------------

def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        return default

LIMIT_PER_MINUTE = _int_env("OCR_RATE_LIMIT_PER_MINUTE", 5)
LIMIT_PER_HOUR   = _int_env("OCR_RATE_LIMIT_PER_HOUR", 20)
LIMIT_PER_DAY    = _int_env("OCR_RATE_LIMIT_PER_DAY", 50)
GLOBAL_DAILY_CAP = _int_env("OCR_DAILY_GLOBAL_CAP", 200)

# ---------------------------------------------------------------------------
# Firestore client — instantiated once per container
# ---------------------------------------------------------------------------

_db: firestore.Client | None = None

def _get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def _email_hash(email: str) -> str:
    """SHA-256 hash of email — used as Firestore doc ID and in logs (no raw PII)."""
    return hashlib.sha256(email.lower().encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Global daily cap check
# ---------------------------------------------------------------------------

def _check_global_cap(db: firestore.Client) -> None:
    """
    Atomically increment the global daily counter and raise 503 if cap exceeded.
    Uses a Firestore transaction so multiple concurrent containers see the same value.
    """
    global_ref = db.collection("ocr_usage_limits").document("global")
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    @firestore.transactional
    def _txn(transaction, ref):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() or {}

        # Reset counter if day has rolled over
        if data.get("day") != today_str:
            data = {"day": today_str, "total": 0}

        if data["total"] >= GLOBAL_DAILY_CAP:
            return False  # cap exceeded

        data["total"] += 1
        transaction.set(ref, data)
        return True

    allowed = _run_txn_with_retry(db, _txn, global_ref, label="global_cap")

    if not allowed:
        logger.error("Global daily OCR cap reached (%d)", GLOBAL_DAILY_CAP)
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable",
        )


# ---------------------------------------------------------------------------
# Per-user rate limit check
# ---------------------------------------------------------------------------

def _check_user_limits(db: firestore.Client, email: str) -> None:
    """
    Atomically check and increment per-user rate limit counters.
    Raises 429 with Retry-After header if any window is exceeded.
    """
    user_hash = _email_hash(email)
    user_ref = db.collection("ocr_rate_limits").document(user_hash)
    now = datetime.now(timezone.utc)

    minute_key = now.strftime("%Y-%m-%dT%H:%M")
    hour_key   = now.strftime("%Y-%m-%dT%H")
    day_key    = now.strftime("%Y-%m-%d")

    @firestore.transactional
    def _txn(transaction, ref):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() or {}

        minute_count = data.get(f"m_{minute_key}", 0)
        hour_count   = data.get(f"h_{hour_key}", 0)
        day_count    = data.get(f"d_{day_key}", 0)

        if minute_count >= LIMIT_PER_MINUTE:
            return "minute"
        if hour_count >= LIMIT_PER_HOUR:
            return "hour"
        if day_count >= LIMIT_PER_DAY:
            return "day"

        # Increment all windows atomically
        transaction.set(ref, {
            f"m_{minute_key}": minute_count + 1,
            f"h_{hour_key}":   hour_count + 1,
            f"d_{day_key}":    day_count + 1,
        }, merge=True)
        return None  # allowed

    exceeded = _run_txn_with_retry(db, _txn, user_ref, label="user_limits")

    if exceeded:
        retry_after = {"minute": 60, "hour": 3600, "day": 86400}[exceeded]
        logger.warning(
            "Rate limit exceeded: window=%s user_hash=%s", exceeded, user_hash
        )
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )


# ---------------------------------------------------------------------------
# Public interface — called by the OCR route before invoking Vertex AI
# ---------------------------------------------------------------------------

async def check_rate_limits(email: str) -> None:
    """
    Run global cap check then per-user rate limit check.
    Global check first — if the project is under attack we don't waste a
    Firestore transaction on per-user bookkeeping.
    """
    db = _get_db()
    _check_global_cap(db)
    _check_user_limits(db, email)
