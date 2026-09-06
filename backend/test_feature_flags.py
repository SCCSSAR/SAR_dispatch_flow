"""test_feature_flags.py — tests for the two-flag rollout model.

Per CLAUDE.md test file pattern: mirror the pure-logic functions + constants
from `backend/main.py` locally rather than importing `main.py` (which has
heavyweight GCP/Vertex AI/httpx/google-api dependencies not installed in
local pytest environments).

When updating the gate logic in main.py, ALSO update the mirror here. The
mirror IS the test contract — if main.py drifts away from this mirror,
that drift will surface in production behavior, and these tests are the
regression boundary.

Specifically tested (Task 1.4):
  - _feature_enabled() returns True only when both flags are set to recognized values
  - Startup combo validation rejects EVERBRIDGE_MODE=safe + SLACK_MODE=full
  - /version response shape (verified via the local helper that builds the dict —
    the FastAPI route itself is not exercised here; the route just returns the
    output of the helper)
"""
import os
import pytest


# ---------------------------------------------------------------------------
# Mirrored gate logic — must be kept in sync with backend/main.py
# ---------------------------------------------------------------------------

def _read_modes(env: dict) -> tuple[str, str]:
    """Mirror of `_EVERBRIDGE_MODE = os.environ.get("EVERBRIDGE_MODE", "").lower()`
    and the matching SLACK_MODE line. Takes an env dict directly so each test
    can pass a different state cleanly."""
    return (
        env.get("EVERBRIDGE_MODE", "").lower(),
        env.get("SLACK_MODE",      "").lower(),
    )


def _feature_enabled(everbridge_mode: str, slack_mode: str) -> bool:
    """Mirror of backend/main.py::_feature_enabled().

    True iff both flags are set to a recognized value. When either flag is
    unset (typical on SCCSSAR-dev during Phase 1-3), this returns False and
    all new endpoints 503.
    """
    return (
        everbridge_mode in ("safe", "full")
        and slack_mode in ("shadow", "full")
    )


def _validate_startup_combo(everbridge_mode: str, slack_mode: str) -> None:
    """Mirror of the startup combo-rejection block in backend/main.py.

    Raises RuntimeError when the operationally-incoherent combination is set.
    Real responders auto-invited to a Slack channel for an incident still in
    draft state is operationally incoherent. See design Section 4.
    """
    if everbridge_mode == "safe" and slack_mode == "full":
        raise RuntimeError(
            "EVERBRIDGE_MODE=safe with SLACK_MODE=full is not allowed — "
            "Slack real invites cannot precede Everbridge real send."
        )


def _build_version_response(version: str, everbridge_mode: str, slack_mode: str) -> dict:
    """Mirror of the dict returned by backend/main.py::version().

    Build it the same way the route does so the test asserts against the
    exact shape the frontend consumes.
    """
    return {
        "version": version,
        "features": {
            "everbridge_slack": _feature_enabled(everbridge_mode, slack_mode),
        },
        "flags": {
            "everbridge_mode": everbridge_mode or "off",
            "slack_mode":      slack_mode      or "off",
        },
    }


# ---------------------------------------------------------------------------
# _feature_enabled() — gate for the new endpoints
# ---------------------------------------------------------------------------

class TestFeatureEnabled:
    def test_both_unset_returns_false(self):
        assert _feature_enabled("", "") is False

    def test_only_everbridge_set_returns_false(self):
        assert _feature_enabled("safe", "") is False

    def test_only_slack_set_returns_false(self):
        assert _feature_enabled("", "shadow") is False

    def test_safe_shadow_returns_true(self):
        assert _feature_enabled("safe", "shadow") is True

    def test_full_shadow_returns_true(self):
        assert _feature_enabled("full", "shadow") is True

    def test_full_full_returns_true(self):
        assert _feature_enabled("full", "full") is True

    def test_safe_full_returns_true_for_gate_check(self):
        # _feature_enabled() itself returns True for safe+full — but the
        # startup combo validation (separate function below) rejects this
        # combination before any code reaches _feature_enabled. The two
        # checks are intentionally separate.
        assert _feature_enabled("safe", "full") is True

    def test_unrecognized_values_return_false(self):
        assert _feature_enabled("foo", "bar") is False

    def test_lowercase_only_safe_for_recognized_values(self):
        # The mirror takes pre-lowered strings, but _read_modes does the
        # lowering. Test the full pipeline matches main.py's behavior.
        ev, sl = _read_modes({"EVERBRIDGE_MODE": "SAFE", "SLACK_MODE": "SHADOW"})
        assert _feature_enabled(ev, sl) is True


# ---------------------------------------------------------------------------
# Startup combo validation — safe + full is operationally incoherent
# ---------------------------------------------------------------------------

class TestStartupComboValidation:
    def test_safe_full_combo_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="not allowed"):
            _validate_startup_combo("safe", "full")

    def test_safe_shadow_does_not_raise(self):
        # Sanity check — must be silent for the safe sequence start
        _validate_startup_combo("safe", "shadow")

    def test_full_shadow_does_not_raise(self):
        _validate_startup_combo("full", "shadow")

    def test_full_full_does_not_raise(self):
        _validate_startup_combo("full", "full")

    def test_both_unset_does_not_raise(self):
        # SCCSSAR-dev during Phase 1-3 — env vars absent. Module must not
        # raise on startup.
        _validate_startup_combo("", "")


# ---------------------------------------------------------------------------
# /version response shape — used by frontend footer + feature gate
# ---------------------------------------------------------------------------

class TestVersionResponseShape:
    def test_unset_flags_render_off(self):
        body = _build_version_response("1.5z / abc1234", "", "")
        assert body["version"] == "1.5z / abc1234"
        assert body["features"]["everbridge_slack"] is False
        assert body["flags"]["everbridge_mode"] == "off"
        assert body["flags"]["slack_mode"]      == "off"

    def test_set_flags_render_literal_value(self):
        body = _build_version_response("1.5z / abc1234", "full", "shadow")
        assert body["features"]["everbridge_slack"] is True
        assert body["flags"]["everbridge_mode"] == "full"
        assert body["flags"]["slack_mode"]      == "shadow"

    def test_safe_shadow_renders_correctly(self):
        body = _build_version_response("1.5z / abc1234", "safe", "shadow")
        assert body["features"]["everbridge_slack"] is True
        assert body["flags"]["everbridge_mode"] == "safe"
        assert body["flags"]["slack_mode"]      == "shadow"

    def test_response_keys_match_frontend_expectations(self):
        # Frontend reads data.version, data.features.everbridge_slack,
        # data.flags.everbridge_mode, data.flags.slack_mode. Pin the keys
        # here so a future PR that drops/renames any of them fails this test.
        body = _build_version_response("v", "safe", "shadow")
        assert set(body.keys()) == {"version", "features", "flags"}
        assert "everbridge_slack" in body["features"]
        assert {"everbridge_mode", "slack_mode"} <= set(body["flags"].keys())
