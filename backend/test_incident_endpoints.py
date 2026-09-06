"""test_incident_endpoints.py — pure-logic tests for the read-side helpers
used by the Task 1.10d endpoints.

Per CLAUDE.md test file pattern: mirror the pure-logic functions from
`backend/main.py` locally rather than importing the module directly.
`backend/main.py` has heavyweight GCP / Vertex AI / httpx / google-api
dependencies not installed in local pytest.

When updating the helpers in main.py, ALSO update the mirror here. The
mirror IS the test contract — drift surfaces in production behavior,
and these tests are the regression boundary.

Test coverage (Task 1.10d):
  - _validate_incident_ownership() — shared by /incident-status and
    /close-incident-polling
  - _validate_close_polling() — composes ownership + terminal-state check
  - _serialize_incident_doc_for_api() — datetime → ISO + recursive
    walk over nested dicts and lists
  - _manual_confirm_offered() — Tasks 3.3+3.4 trigger for the dispatch
    console "Did you press Send?" recovery banner

NOT exercised here (covered at live-test time on personal-dev):
  - The four endpoints' Firestore I/O wrappers (read, update)
  - /everbridge-groups / /everbridge-contacts wrap thin Everbridge
    SDK calls already covered by test_everbridge.py response parsers
"""
import datetime

import pytest

# incidents.py is pure stdlib — safe to import directly. Pinning the
# constant here means the mirror tests fail fast if a future commit
# changes the threshold without updating the test contract.
from incidents import MANUAL_CONFIRM_OFFER_DELAY_S


# ---------------------------------------------------------------------------
# Mirrored helpers — must be kept in sync with backend/main.py
# ---------------------------------------------------------------------------

def _validate_incident_ownership(
    doc: dict | None,
    user_email: str,
) -> tuple[int, str] | None:
    """Mirror of backend/main.py::_validate_incident_ownership()."""
    if not doc or doc.get("dispatcher_email") != user_email:
        return (404, "Incident not found or not yours")
    return None


def _validate_close_polling(
    doc: dict | None,
    user_email: str,
) -> tuple[int, str] | None:
    """Mirror of backend/main.py::_validate_close_polling()."""
    err = _validate_incident_ownership(doc, user_email)
    if err:
        return err
    status = doc.get("status", "") if doc else ""
    if isinstance(status, str) and status.startswith("stopped_"):
        return (409, f"Incident already in terminal state {status!r}")
    return None


def _serialize_incident_doc_for_api(value):
    """Mirror of backend/main.py::_serialize_incident_doc_for_api()."""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _serialize_incident_doc_for_api(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_incident_doc_for_api(v) for v in value]
    return value


def _manual_confirm_offered(
    doc: dict | None,
    now: datetime.datetime | None = None,
) -> bool:
    """Mirror of backend/main.py::_manual_confirm_offered().

    The mirror does NOT delegate to the real now_utc() — every test pins
    `now` explicitly so timing-sensitive behavior is deterministic.
    """
    if not doc:
        return False
    if doc.get("status") not in ("polling", "pre_discovery"):
        return False
    if doc.get("notification_id") is not None:
        return False
    if doc.get("template_id") is None:
        return False
    created_at = doc.get("created_at")
    if not isinstance(created_at, datetime.datetime):
        return False
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elapsed_s = (now - created_at).total_seconds()
    return elapsed_s >= MANUAL_CONFIRM_OFFER_DELAY_S


# ---------------------------------------------------------------------------
# Helpers — keep test arrange short
# ---------------------------------------------------------------------------

def _doc(**overrides) -> dict:
    base = {
        "event_id":         "2026-04-25_mpd_calaveras_1430",
        "dispatcher_email": "dispatcher@example.com",
        "notification_id":  "NOTIF_789",
        "status":           "polling",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _validate_incident_ownership() — shared 404 check
# ---------------------------------------------------------------------------

class TestValidateIncidentOwnership:
    def test_owner_returns_none(self):
        assert _validate_incident_ownership(
            _doc(), "dispatcher@example.com",
        ) is None

    def test_doc_missing_returns_404(self):
        assert _validate_incident_ownership(None, "dispatcher@example.com") == (
            404, "Incident not found or not yours",
        )

    def test_doc_empty_dict_returns_404(self):
        # Defensive — `not doc` covers None AND empty dict.
        assert _validate_incident_ownership({}, "dispatcher@example.com") == (
            404, "Incident not found or not yours",
        )

    def test_wrong_owner_returns_404(self):
        # Different authorized dispatcher querying someone else's incident.
        assert _validate_incident_ownership(
            _doc(dispatcher_email="other@example.com"),
            "dispatcher@example.com",
        ) == (404, "Incident not found or not yours")

    def test_missing_and_wrong_owner_responses_identical(self):
        # Defense-in-depth — same regression test as test_confirm_draft_sent.
        # Pinned here too because /incident-status and /close-incident-polling
        # are the two read endpoints that could leak ownership existence.
        not_found  = _validate_incident_ownership(None, "x@example.com")
        wrong_user = _validate_incident_ownership(
            _doc(dispatcher_email="other@example.com"),
            "x@example.com",
        )
        assert not_found == wrong_user

    def test_empty_email_does_not_match_real_doc(self):
        # Defensive against misconfigured auth dependency returning ''.
        assert _validate_incident_ownership(
            _doc(dispatcher_email="dispatcher@example.com"),
            "",
        ) == (404, "Incident not found or not yours")

    def test_terminal_status_does_NOT_block_ownership_check(self):
        # /incident-status is a READ — owner can read their stopped doc to
        # see the final tally, who responded, etc. Only /close-incident-polling
        # rejects terminal states (separate validator below).
        assert _validate_incident_ownership(
            _doc(status="stopped_idle"),
            "dispatcher@example.com",
        ) is None


# ---------------------------------------------------------------------------
# _validate_close_polling() — ownership + terminal-state
# ---------------------------------------------------------------------------

class TestValidateClosePolling:
    def test_owner_polling_returns_none(self):
        assert _validate_close_polling(
            _doc(status="polling"),
            "dispatcher@example.com",
        ) is None

    def test_owner_pre_discovery_returns_none(self):
        # Task 1.11 introduces 'pre_discovery' — closing a pre-discovery
        # safe-mode incident before auto-discovery succeeds is valid (the
        # dispatcher decided not to send after all).
        assert _validate_close_polling(
            _doc(status="pre_discovery"),
            "dispatcher@example.com",
        ) is None

    def test_doc_missing_returns_404_via_ownership_check(self):
        assert _validate_close_polling(None, "dispatcher@example.com") == (
            404, "Incident not found or not yours",
        )

    def test_wrong_owner_returns_404_not_409(self):
        # Even if the doc IS in a terminal state, wrong-owner gets 404 (not
        # 409). Don't disclose terminal-state info to non-owners.
        assert _validate_close_polling(
            _doc(dispatcher_email="other@example.com", status="stopped_idle"),
            "dispatcher@example.com",
        ) == (404, "Incident not found or not yours")

    @pytest.mark.parametrize("terminal_status", [
        "stopped_everbridge_closed",
        "stopped_idle",
        "stopped_hard_cap",
        "stopped_error",
        "stopped_draft_unsent",
        "stopped_manual",
    ])
    def test_each_known_terminal_status_returns_409(self, terminal_status):
        result = _validate_close_polling(
            _doc(status=terminal_status),
            "dispatcher@example.com",
        )
        assert result is not None
        assert result[0] == 409
        assert "terminal state" in result[1]
        # Status string included in detail so the dispatcher's UI can show
        # which terminal state the incident's in.
        assert terminal_status in result[1]

    def test_unknown_status_does_not_match_stopped_prefix(self):
        # Defensive — unknown status that's NOT prefixed `stopped_` is
        # treated as still-running (not 409). This is the safe direction:
        # if a future PR introduces a new in-progress status (say,
        # 'reconciling'), close-polling should still work on it.
        assert _validate_close_polling(
            _doc(status="reconciling"),
            "dispatcher@example.com",
        ) is None

    def test_missing_status_field_treated_as_not_terminal(self):
        # Defensive — missing status defaults to '' which doesn't match
        # the `stopped_` prefix → ownership passes → 200.
        doc = _doc()
        doc.pop("status", None)
        assert _validate_close_polling(doc, "dispatcher@example.com") is None

    def test_non_string_status_does_not_crash(self):
        # Defensive — a Firestore schema bug that wrote a non-string status
        # shouldn't crash the validator. The isinstance(str) guard covers it.
        assert _validate_close_polling(
            _doc(status=42),
            "dispatcher@example.com",
        ) is None


# ---------------------------------------------------------------------------
# _serialize_incident_doc_for_api() — datetime → ISO + recursion
# ---------------------------------------------------------------------------

class TestSerializeIncidentDocForApi:
    def test_datetime_converted_to_iso(self):
        # tz-aware UTC datetime — typical Firestore created_at
        ts = datetime.datetime(2026, 4, 25, 14, 30, tzinfo=datetime.timezone.utc)
        out = _serialize_incident_doc_for_api(ts)
        assert out == "2026-04-25T14:30:00+00:00"

    def test_naive_datetime_also_handled(self):
        # Defensive — should never happen post-incidents.py (now_utc()
        # always produces tz-aware), but pin behavior so a future bug
        # doesn't silently truncate timestamps.
        ts = datetime.datetime(2026, 4, 25, 14, 30)
        out = _serialize_incident_doc_for_api(ts)
        assert out == "2026-04-25T14:30:00"

    def test_strings_pass_through(self):
        assert _serialize_incident_doc_for_api("hello") == "hello"

    def test_ints_floats_bools_none_pass_through(self):
        assert _serialize_incident_doc_for_api(42) == 42
        assert _serialize_incident_doc_for_api(3.14) == 3.14
        assert _serialize_incident_doc_for_api(True) is True
        assert _serialize_incident_doc_for_api(None) is None

    def test_list_of_strings_passes_through(self):
        out = _serialize_incident_doc_for_api(["c1", "g:42"])
        assert out == ["c1", "g:42"]

    def test_dict_of_primitives_passes_through(self):
        out = _serialize_incident_doc_for_api({
            "event_id": "x", "status": "polling", "responders": [],
        })
        assert out == {
            "event_id": "x", "status": "polling", "responders": [],
        }

    def test_nested_dict_with_datetime_recursed(self):
        # The Firestore incident doc has datetimes at the top level
        # (created_at, last_poll_at, etc.) — but tests defend against a
        # future schema change that nests them.
        ts = datetime.datetime(2026, 4, 25, 14, 30, tzinfo=datetime.timezone.utc)
        out = _serialize_incident_doc_for_api({
            "event_id": "x",
            "metadata": {"created_at": ts, "version": 1},
        })
        assert out == {
            "event_id": "x",
            "metadata": {"created_at": "2026-04-25T14:30:00+00:00", "version": 1},
        }

    def test_list_of_dicts_recursed(self):
        # responders[] is a list of dicts in the incident doc shape.
        ts = datetime.datetime(2026, 4, 25, 14, 30, tzinfo=datetime.timezone.utc)
        out = _serialize_incident_doc_for_api([
            {"name": "Burns", "ack_at": ts},
            {"name": "Black"},
        ])
        assert out == [
            {"name": "Burns", "ack_at": "2026-04-25T14:30:00+00:00"},
            {"name": "Black"},
        ]

    def test_full_incident_doc_shape_round_trip(self):
        # Matches the new_incident_doc shape from incidents.py — pin
        # that the whole real-world doc serializes to a JSON-safe dict.
        import json
        ts = datetime.datetime(2026, 4, 25, 14, 30, tzinfo=datetime.timezone.utc)
        doc = {
            "event_id":              "2026-04-25_mpd_calaveras_1430",
            "event_name":            "2026-04-25 MPD CALAVERAS 1430",
            "event_name_human":      "2026-04-25 MPD CALAVERAS",
            "dispatcher_email":      "dispatcher@example.com",
            "everbridge_event_id":   "EVT_456",
            "notification_id":       "NOTIF_789",
            "template_id":           None,
            "slack_channel_id":      "C123",
            "slack_channel_name":    "2026-04-25_mpd_calaveras",
            "active_incidents_ts":   "1234.5678",
            "everbridge_mode":       "full",
            "slack_mode":            "full",
            "action":                "send_live",
            "selected_target_ids":   ["g:111", "c:222"],
            "responders":            [],
            "last_non_empty_responders": [],
            "status":                "polling",
            "stop_reason":           None,
            "created_at":            ts,
            "last_poll_at":          None,
            "last_responder_at":     None,
            "first_error_at":        None,
            "expire_at":             None,
        }
        out = _serialize_incident_doc_for_api(doc)
        # JSON-encodable end-to-end (would raise TypeError if any datetime leaks)
        json.dumps(out)
        assert out["created_at"] == "2026-04-25T14:30:00+00:00"
        assert out["event_id"] == "2026-04-25_mpd_calaveras_1430"
        assert out["selected_target_ids"] == ["g:111", "c:222"]

    def test_does_not_mutate_input(self):
        # The handler may want to use the original doc for other purposes
        # (e.g. logging) AFTER serialization. Pin that the helper is
        # functional — no in-place mutation of the input dict.
        ts = datetime.datetime(2026, 4, 25, 14, 30, tzinfo=datetime.timezone.utc)
        original = {"created_at": ts, "name": "x"}
        _serialize_incident_doc_for_api(original)
        assert original["created_at"] is ts   # unchanged
        assert original["name"] == "x"


# ---------------------------------------------------------------------------
# _manual_confirm_offered() — Tasks 3.3+3.4 banner trigger
# ---------------------------------------------------------------------------

def _safe_draft_doc(**overrides) -> dict:
    """Builder for a typical safe-mode draft incident doc — the ONLY shape
    where the manual-confirm banner can ever fire. Tests mutate from this
    base via overrides."""
    base = {
        "event_id":         "2026-04-25_mpd_calaveras_1430",
        "dispatcher_email": "dispatcher@example.com",
        "status":           "polling",
        "notification_id":  None,
        "template_id":      "TPL_456",
        "created_at":       datetime.datetime(
            2026, 4, 25, 14, 30, tzinfo=datetime.timezone.utc,
        ),
    }
    base.update(overrides)
    return base


# Pin the threshold the mirror test imports from incidents.py — drift
# would silently change the timing assertions below.
_BASE_TS = datetime.datetime(2026, 4, 25, 14, 30, tzinfo=datetime.timezone.utc)
_NOW_AT_DELAY = _BASE_TS + datetime.timedelta(seconds=MANUAL_CONFIRM_OFFER_DELAY_S)


class TestManualConfirmOffered:
    def test_safe_mode_draft_after_delay_returns_true(self):
        # Happy path — safe-mode draft has been in pre-discovery for the
        # threshold; banner SHOULD appear.
        assert _manual_confirm_offered(
            _safe_draft_doc(),
            now=_NOW_AT_DELAY + datetime.timedelta(seconds=1),
        ) is True

    def test_live_send_path_returns_false(self):
        # Live send: notification_id is set at send time, template_id is None.
        # Banner makes no sense — there's no manual UI step to recover from.
        assert _manual_confirm_offered(
            _safe_draft_doc(notification_id="NOTIF_789", template_id=None),
            now=_NOW_AT_DELAY + datetime.timedelta(minutes=10),
        ) is False

    def test_post_discovery_returns_false(self):
        # Auto-discovery succeeded — notification_id has been linked to the
        # safe-mode draft. Banner self-dismisses.
        assert _manual_confirm_offered(
            _safe_draft_doc(notification_id="NOTIF_789"),
            now=_NOW_AT_DELAY + datetime.timedelta(minutes=10),
        ) is False

    def test_within_delay_window_returns_false(self):
        # 1 minute elapsed — below the 2-minute threshold. Banner stays
        # hidden so the happy path (Send + manual UI within 5s) doesn't
        # see a transient flash of the recovery prompt.
        assert _manual_confirm_offered(
            _safe_draft_doc(),
            now=_BASE_TS + datetime.timedelta(seconds=60),
        ) is False

    def test_exactly_at_threshold_returns_true(self):
        # Boundary — `>=` semantics, not strict `>`.
        assert _manual_confirm_offered(
            _safe_draft_doc(),
            now=_NOW_AT_DELAY,
        ) is True

    def test_terminal_status_returns_false(self):
        # Once the incident is stopped (whether by 30-min timeout, manual
        # close, or auto-discovery + Everbridge completion), the banner is
        # replaced by terminal-state UI. Belt-and-suspenders against the
        # poll-timer trying to render a banner over a closed incident.
        for terminal in (
            "stopped_draft_unsent",
            "stopped_manual",
            "stopped_idle",
            "stopped_hard_cap",
            "stopped_error",
            "stopped_everbridge_closed",
        ):
            assert _manual_confirm_offered(
                _safe_draft_doc(status=terminal),
                now=_NOW_AT_DELAY + datetime.timedelta(minutes=10),
            ) is False, f"banner offered for terminal status {terminal!r}"

    def test_pre_discovery_status_also_triggers(self):
        # _validate_confirm_draft_sent accepts both 'polling' and
        # 'pre_discovery' (the latter is reserved for a future status
        # transition the poll handler may emit). The banner trigger
        # mirrors that tolerance — over-showing a UX banner is much safer
        # than missing it.
        assert _manual_confirm_offered(
            _safe_draft_doc(status="pre_discovery"),
            now=_NOW_AT_DELAY + datetime.timedelta(seconds=1),
        ) is True

    def test_doc_none_returns_false(self):
        # Defensive — _validate_incident_ownership returns 404 before the
        # endpoint reaches this helper, so doc is never None in practice.
        # But the helper must not crash if a future caller forgets to validate.
        assert _manual_confirm_offered(None, now=_NOW_AT_DELAY) is False

    def test_missing_created_at_returns_false(self):
        # Defensive — Firestore doc shape is fixed by new_incident_doc(),
        # but a future schema change (or a partial test fixture) shouldn't
        # crash the live endpoint.
        doc = _safe_draft_doc()
        del doc["created_at"]
        assert _manual_confirm_offered(doc, now=_NOW_AT_DELAY) is False

    def test_non_datetime_created_at_returns_false(self):
        # Defensive — a Firestore deserialization edge case (or a stale
        # test fixture) writing a string instead of a datetime should not
        # crash with TypeError on the subtraction.
        doc = _safe_draft_doc(created_at="2026-04-25T14:30:00+00:00")
        assert _manual_confirm_offered(doc, now=_NOW_AT_DELAY) is False


# ---------------------------------------------------------------------------
# D4H Phase 2 PR 5 Task 5.6 — _build_dispatch_status_response()
# PR 7 amendment: d4h_event_log is now exposed (was excluded in PR 5 design).
# ---------------------------------------------------------------------------
# Mirror of the production helper in main.py that the /dispatch-status/{event_id}
# endpoint uses to convert a Firestore incident doc into the 7-field JSON
# response. Pure-logic — pulls 7 keys with appropriate defaults.
#
# Design contract (updated PR 7): returns
#   {eb_status, slack_status, d4h_status, eb_error, slack_error, d4h_error,
#    d4h_event_log}
# Frontend polls every 500ms during the dispatch window (PR 7); new
# d4h_event_log entries are appended to the textarea via a cursor.

def _build_dispatch_status_response(doc: dict | None) -> dict:
    """Mirror of backend/main.py::_build_dispatch_status_response()."""
    d = doc or {}
    return {
        "eb_status":     d.get("eb_status"),
        "slack_status":  d.get("slack_status"),
        "d4h_status":    d.get("d4h_status"),
        "eb_error":      d.get("eb_error"),
        "slack_error":   d.get("slack_error"),
        "d4h_error":     d.get("d4h_error"),
        "d4h_event_log": d.get("d4h_event_log", []),
    }


class TestBuildDispatchStatusResponse:
    STATUS_ERROR_KEYS = {"eb_status", "slack_status", "d4h_status",
                         "eb_error", "slack_error", "d4h_error"}
    ALL_KEYS = STATUS_ERROR_KEYS | {"d4h_event_log"}

    def test_all_response_keys_present(self):
        resp = _build_dispatch_status_response({})
        assert set(resp.keys()) == self.ALL_KEYS

    def test_d4h_success_state(self):
        resp = _build_dispatch_status_response({
            "d4h_status":      "done",
            "d4h_activity_id": 1609745,
            "d4h_error":       None,
        })
        assert resp["d4h_status"] == "done"
        assert resp["d4h_error"]  is None

    def test_d4h_failed_state_with_sanitized_error(self):
        resp = _build_dispatch_status_response({
            "d4h_status": "failed",
            "d4h_error":  "D4HServerError: HTTP 503 service unavailable",
        })
        assert resp["d4h_status"] == "failed"
        assert "D4HServerError" in resp["d4h_error"]

    def test_missing_status_fields_default_to_none(self):
        # eb_status / slack_status are deferred (always None today).
        resp = _build_dispatch_status_response({"d4h_status": "done"})
        assert resp["eb_status"]    is None
        assert resp["slack_status"] is None
        assert resp["eb_error"]     is None
        assert resp["slack_error"]  is None

    def test_none_doc_status_error_fields_are_none(self):
        """Status/error fields are None when doc is None; d4h_event_log is []."""
        resp = _build_dispatch_status_response(None)
        for k in self.STATUS_ERROR_KEYS:
            assert resp[k] is None
        assert resp["d4h_event_log"] == []

    def test_d4h_activity_id_NOT_exposed(self):
        """Activity ID is internal — not part of the /dispatch-status response."""
        resp = _build_dispatch_status_response({"d4h_activity_id": 1609745})
        assert "d4h_activity_id" not in resp

    def test_d4h_event_log_is_exposed(self):
        """PR 7: event log IS exposed so the frontend poll loop can drain entries."""
        resp = _build_dispatch_status_response({"d4h_event_log": ["x", "y"]})
        assert "d4h_event_log" in resp
        assert resp["d4h_event_log"] == ["x", "y"]

    def test_d4h_event_log_default_empty_array(self):
        """When the Firestore field is absent (e.g., older doc), returns []."""
        resp = _build_dispatch_status_response({})
        assert resp["d4h_event_log"] == []

    def test_d4h_event_log_returns_array_contents(self):
        """Array passthrough — ordering and content preserved."""
        entries = [
            "2026-05-18 09:00 - D4H incident created (activity_id=1617853)",
            "2026-05-18 09:01 - D4H bulk-ABSENT: 50 marked, 0 failed",
        ]
        resp = _build_dispatch_status_response({"d4h_event_log": entries})
        assert resp["d4h_event_log"] == entries
