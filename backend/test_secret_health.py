# backend/test_secret_health.py
"""test_secret_health.py — pure-logic tests for secret_health.py helpers.

Per backend/test_d4h.py and backend/test_everbridge.py pattern: mirror constants +
pure-logic functions locally rather than importing secret_health.py directly.
google-cloud-secret-manager is not installed in the local pytest env; importing
secret_health would fail at collection time.

When updating helpers in secret_health.py, ALSO update the mirror here. The
mirror IS the test contract — drift surfaces in production behavior.
"""
import datetime


# --------------------------------------------------------------------------
# Mirrored constants/helpers — kept in sync with backend/secret_health.py
# --------------------------------------------------------------------------

# --- Mirror ---
_WARN_DAYS = 30

# --- Mirror ---
_MONITORED_SECRETS: dict[str, int] = {
    "d4h-access-token": 365,
    # "everbridge-credentials": 180,  # uncomment when EB rotation is operationalized
    # "slack-bot-token": 365,          # uncomment when Slack rotation is operationalized
}


# --- Mirror ---
def _compute_expiry(labels: dict) -> dict:
    """Mirror of backend/secret_health.py::_compute_expiry."""
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


# --- Mirror ---
def _orchestrate(project_id: str, fetch_labels_fn) -> dict[str, dict]:
    """Mirror of backend/secret_health.py::get_all_token_health, parameterized
    on the label-fetching function (production passes _fetch_labels; tests pass
    a stub). The orchestration shape itself — iterate registry, fetch, compute —
    is what's pinned by these tests.
    """
    return {
        secret_name: _compute_expiry(fetch_labels_fn(secret_name, project_id))
        for secret_name in _MONITORED_SECRETS
    }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _labels(rotated_offset_days: int, period_days: int) -> dict:
    """Build label dict with rotated = today - offset, period_days as given."""
    rotated = (datetime.date.today() - datetime.timedelta(days=rotated_offset_days)).isoformat()
    return {"rotated": rotated, "period_days": str(period_days)}


# --------------------------------------------------------------------------
# Tests — _compute_expiry
# --------------------------------------------------------------------------

class TestComputeExpiry:
    def test_normal_case_computes_days_remaining(self):
        result = _compute_expiry(_labels(rotated_offset_days=300, period_days=365))
        assert result["days_remaining"] == 65  # 365 - 300
        assert result["period_days"] == 365
        assert result["warn"] is False  # 65 > 30

    def test_warn_true_when_within_30_days(self):
        result = _compute_expiry(_labels(rotated_offset_days=340, period_days=365))
        assert result["days_remaining"] == 25
        assert result["warn"] is True

    def test_warn_boundary_at_exactly_30_days(self):
        result = _compute_expiry(_labels(rotated_offset_days=335, period_days=365))
        assert result["days_remaining"] == 30
        assert result["warn"] is True  # boundary inclusive

    def test_warn_false_at_31_days(self):
        result = _compute_expiry(_labels(rotated_offset_days=334, period_days=365))
        assert result["days_remaining"] == 31
        assert result["warn"] is False

    def test_negative_when_expired(self):
        result = _compute_expiry(_labels(rotated_offset_days=400, period_days=365))
        assert result["days_remaining"] < 0
        assert result["warn"] is True  # expired -> always warn

    def test_missing_rotated_label_returns_none(self):
        result = _compute_expiry({"period_days": "365"})
        assert result["days_remaining"] is None
        assert result["period_days"] is None
        assert result["rotated"] is None
        assert result["warn"] is False

    def test_missing_period_days_label_returns_none(self):
        result = _compute_expiry({"rotated": "2026-01-01"})
        assert result["days_remaining"] is None
        assert result["warn"] is False

    def test_empty_labels_dict_returns_none(self):
        result = _compute_expiry({})
        assert result["days_remaining"] is None
        assert result["warn"] is False


# --------------------------------------------------------------------------
# Tests — orchestrator (registry iteration shape)
# --------------------------------------------------------------------------

class TestOrchestrate:
    def test_returns_entry_for_every_registered_secret(self):
        """Result keys must match _MONITORED_SECRETS keys exactly."""
        result = _orchestrate("test-project", lambda name, pid: {})
        assert set(result.keys()) == set(_MONITORED_SECRETS.keys())

    def test_passes_secret_name_and_project_to_fetcher(self):
        """Orchestrator must call fetcher with (secret_name, project_id)."""
        calls = []

        def recording_fetcher(name, pid):
            calls.append((name, pid))
            return {}

        _orchestrate("test-project", recording_fetcher)
        assert ("d4h-access-token", "test-project") in calls

    def test_d4h_token_with_real_labels_computed_correctly(self):
        """Integration check: registry + fetcher + compute end-to-end."""
        labels = _labels(rotated_offset_days=10, period_days=365)
        result = _orchestrate("test-project", lambda name, pid: labels)
        d4h = result["d4h-access-token"]
        assert d4h["days_remaining"] == 355
        assert d4h["warn"] is False

    def test_d4h_token_missing_labels_yields_none(self):
        result = _orchestrate("test-project", lambda name, pid: {})
        assert result["d4h-access-token"]["days_remaining"] is None
        assert result["d4h-access-token"]["warn"] is False
