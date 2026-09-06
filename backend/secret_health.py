"""Centralized token/credential expiry monitoring.

Reads Secret Manager labels (set by bin/rotate-secret.sh) for each monitored secret
and computes days until expiry. Add new integrations to _MONITORED_SECRETS only.
"""
from __future__ import annotations

import datetime

from google.cloud import secretmanager as _secretmanager

_WARN_DAYS = 30  # surface banner when this many days or fewer remain

# Registry: secret_name -> expected default period_days (informational only;
# actual period_days is read from the Secret Manager label at call time).
# Add new integrations as one-liners when their rotation lifecycle is operationalized.
_MONITORED_SECRETS: dict[str, int] = {
    "d4h-access-token": 365,
    # "everbridge-credentials": 180,  # uncomment when EB rotation is operationalized
    # "slack-bot-token": 365,          # uncomment when Slack rotation is operationalized
}


def _fetch_labels(secret_name: str, project_id: str) -> dict:
    """Fetch Secret Manager labels for a secret. Returns {} on any error.

    Requires roles/secretmanager.viewer on the project (granted in Terraform).
    """
    client = _secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_name}"
    try:
        secret = client.get_secret(request={"name": name})
        return dict(secret.labels)
    except Exception:
        return {}


def _compute_expiry(labels: dict) -> dict:
    """Compute expiry fields from rotation labels."""
    rotated_str = labels.get("rotated")
    period_days_str = labels.get("period_days")

    if not rotated_str or not period_days_str:
        return {"days_remaining": None, "period_days": None, "rotated": None, "warn": False}

    rotated_date = datetime.date.fromisoformat(rotated_str)
    period_days = int(period_days_str)
    expiry_date = rotated_date + datetime.timedelta(days=period_days)
    days_remaining = (expiry_date - datetime.date.today()).days

    return {
        "days_remaining": days_remaining,
        "period_days": period_days,
        "rotated": rotated_str,
        "warn": days_remaining <= _WARN_DAYS,
    }


def get_all_token_health(project_id: str) -> dict[str, dict]:
    """Return expiry info for every secret in _MONITORED_SECRETS.

    Returns:
        {secret_name: {"days_remaining": int|None, "period_days": int|None,
                       "rotated": str|None, "warn": bool}}
    days_remaining is None when labels are absent (never rotated via bin/rotate-secret.sh).
    warn is True when days_remaining <= _WARN_DAYS (including expired/negative).
    """
    return {
        secret_name: _compute_expiry(_fetch_labels(secret_name, project_id))
        for secret_name in _MONITORED_SECRETS
    }
