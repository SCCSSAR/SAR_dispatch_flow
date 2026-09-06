"""
auth.py — Google ID token verification + dispatcher allowlist enforcement.

Every protected route uses require_authorized_dispatcher as a FastAPI dependency.
Verification order:
  1. Parse Bearer token from Authorization header
  2. Verify signature, aud, iss, exp via google-auth
  3. Check email_verified == True
  4. Check email is in the hardcoded allowlist (loaded from AUTHORIZED_EMAILS env var)

No request reaches business logic without passing all four gates.
"""

import logging
import os
from functools import lru_cache

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowlist — loaded once at startup from the AUTHORIZED_EMAILS env var.
# Format: comma-separated email addresses.
# Set via Secret Manager → Cloud Run env var at deploy time.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_allowlist() -> frozenset[str]:
    raw = os.environ.get("AUTHORIZED_EMAILS", "")
    if not raw:
        logger.error("AUTHORIZED_EMAILS env var is empty — no users will be authorized")
        return frozenset()
    emails = frozenset(e.strip().lower() for e in raw.split(",") if e.strip())
    logger.info("Loaded dispatcher allowlist: %d authorized accounts", len(emails))
    return emails


# ---------------------------------------------------------------------------
# Firebase / Google Sign-In audience.
# Must match the OAuth2 client ID used by the frontend.
# Set via GOOGLE_CLIENT_ID env var at deploy time.
# ---------------------------------------------------------------------------

def _get_expected_audience() -> str:
    audience = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not audience:
        raise RuntimeError("GOOGLE_CLIENT_ID env var is not set")
    return audience


# ---------------------------------------------------------------------------
# FastAPI dependency — inject into any route that must be protected.
# Returns the verified token payload so routes can log the sub (stable user ID).
# ---------------------------------------------------------------------------

security = HTTPBearer()


async def require_authorized_dispatcher(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Verify the Google ID token and enforce the dispatcher allowlist.
    Raises HTTPException on any failure — never leaks the reason to the caller
    beyond the HTTP status code (prevents oracle attacks).
    """
    token_str = credentials.credentials

    # Gate 1: Verify signature, aud, iss, exp
    try:
        token = id_token.verify_oauth2_token(
            token_str,
            google_requests.Request(),
            audience=_get_expected_audience(),
        )
    except Exception:
        # Log at WARNING — this fires on every expired/invalid token, not just attacks
        logger.warning(
            "Token verification failed | ip=%s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Gate 1.5: OIDC nonce binding (PR-F).
    # GSI's data-nonce on the sign-in button forwards the nonce as a `nonce`
    # claim inside the signed JWT.  The live page session sends the same
    # value in X-OIDC-Nonce on every authed request.  A mismatch means the
    # bearer token came from a different sign-in than the current session
    # — i.e. a replay attempt.  Defense-in-depth on top of TLS.
    expected_nonce = request.headers.get("X-OIDC-Nonce", "")
    token_nonce = token.get("nonce", "")
    if not expected_nonce or not token_nonce or expected_nonce != token_nonce:
        logger.warning("OIDC nonce mismatch | sub=%s", token.get("sub", "?"))
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Gate 2: email_verified must be True
    if not token.get("email_verified", False):
        logger.warning("Unverified email rejected | sub=%s", token.get("sub", "?"))
        raise HTTPException(status_code=403, detail="Forbidden")

    # Gate 3: Email must be in the allowlist
    email = token.get("email", "").lower()
    if email not in _load_allowlist():
        # Log at WARNING with the sub (stable ID), not the email, to avoid PII in logs
        logger.warning("Non-allowlisted account rejected | sub=%s", token.get("sub", "?"))
        raise HTTPException(status_code=403, detail="Forbidden")

    logger.info("Dispatcher authenticated | sub=%s", token.get("sub", "?"))
    return token
