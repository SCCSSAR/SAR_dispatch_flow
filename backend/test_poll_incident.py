"""test_poll_incident.py — pure-logic tests for the Task 1.11 polling helpers.

Per CLAUDE.md test file pattern: mirror the pure-logic functions from
`backend/main.py` locally rather than importing the module directly.
`backend/main.py` has heavyweight GCP / Vertex AI / httpx / google-api /
google-cloud-tasks / slack-sdk dependencies not installed in local pytest.

When updating helpers in main.py, ALSO update the mirror here. The mirror
IS the test contract — drift surfaces in production behavior, and these
tests are the regression boundary.

Test coverage (Task 1.11):
  - _check_oidc_claims() — issuer, email_verified, email match, audience
    match (all 401 on failure for ambiguity); pinned identical-response
    pattern across failure modes
  - _render_terminal_header() — phrase mapping for each known reason
  - _check_stop_conditions() — five stop conditions in priority order;
    Phase 0 Task 8 (notificationStatus terminal); manual close;
    hard cap; idle; mode-dependent error tolerance
  - _apply_responder_diff() — new arrivals only (contact_id identity);
    name shaping; partial-data fallback; defensive empty handling

NOT exercised here (covered at live-test on personal-dev):
  - /poll-incident orchestration sequence (FastAPI TestClient + heavyweight
    imports — deferred to end-of-Phase-1 venv test env follow-up)
  - /delete-template orchestration
  - _enqueue_poll_task / _enqueue_template_delete_task (Cloud Tasks SDK)
  - _verify_oidc_request (google-auth SDK call)
"""
import datetime

import pytest


# ---------------------------------------------------------------------------
# Mirrored helpers — must be kept in sync with backend/main.py
# ---------------------------------------------------------------------------

_GOOGLE_OIDC_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_HARD_CAP_S = 4 * 60 * 60
_IDLE_S     = 60 * 60


def _check_oidc_claims(
    claims: dict | None,
    expected_email: str,
    expected_audience: str,
) -> tuple[int, str] | None:
    """Mirror of backend/main.py::_check_oidc_claims()."""
    # Fail-closed when caller config is missing: an empty expected_email
    # (PROJECT_ID unset) or expected_audience (CLOUD_RUN_SERVICE_URL unset)
    # means the deployment is misconfigured — refuse without comparing
    # against attacker-controlled token fields.
    if not expected_email or not expected_audience:
        return (401, "Unauthorized")
    if not claims:
        return (401, "Unauthorized")
    iss = claims.get("iss", "")
    if iss not in _GOOGLE_OIDC_ISSUERS:
        return (401, "Unauthorized")
    if not claims.get("email_verified"):
        return (401, "Unauthorized")
    if claims.get("email") != expected_email:
        return (401, "Unauthorized")
    if claims.get("aud") != expected_audience:
        return (401, "Unauthorized")
    return None


def _render_terminal_header(stop_reason: str) -> str:
    """Mirror of backend/main.py::_render_terminal_header()."""
    phrases = {
        "everbridge_closed": "Completed",
        "stopped":           "Stopped (manual EB UI)",
        "manual":            "Stopped (manual close)",
        "idle":              "idle (no new responders)",
        "hard_cap":          "4-hour cap reached",
        "error":             "polling errors exceeded tolerance",
        "draft_unsent":      "draft expired without send",
    }
    return f"⏹ Everbridge STOPPED — {phrases.get(stop_reason, stop_reason)}"


def _check_stop_conditions(
    *,
    doc: dict,
    poll_response: dict | None,
    mode_tolerance_s: int,
    now_dt: datetime.datetime,
) -> str | None:
    """Mirror of backend/main.py::_check_stop_conditions()."""
    notif_status = ""
    if poll_response is not None:
        notif_status = (poll_response.get("notif_status") or "")
    if notif_status == "Completed":
        return "everbridge_closed"
    if doc.get("manual_stop_requested"):
        return "manual"
    if notif_status == "Stopped":
        return "stopped"
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime.datetime):
        if (now_dt - created_at).total_seconds() >= _HARD_CAP_S:
            return "hard_cap"
    last_resp = doc.get("last_responder_at") or created_at
    if isinstance(last_resp, datetime.datetime):
        if (now_dt - last_resp).total_seconds() >= _IDLE_S:
            return "idle"
    first_err = doc.get("first_error_at")
    if isinstance(first_err, datetime.datetime):
        if (now_dt - first_err).total_seconds() >= mode_tolerance_s:
            return "error"
    return None


def _shape_responder_name(ack: dict) -> str:
    """Mirror of backend/main.py::_shape_responder_name()."""
    last  = (ack.get("last_name")  or "").strip()
    first = (ack.get("first_name") or "").strip()
    if last and first:
        return f"{last}, {first}"
    if last:
        return last
    if first:
        return first
    return ack.get("contact_id", "?")


def _apply_responder_diff(
    prev_responders: list[dict],
    new_ack_contacts: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Mirror of backend/main.py::_apply_responder_diff()."""
    prev_ids = {r.get("contact_id") for r in prev_responders}
    new_arrivals: list[dict] = []
    next_full_list = list(prev_responders)
    for ack in new_ack_contacts:
        cid = ack.get("contact_id")
        if not cid or cid in prev_ids:
            continue
        entry = {
            "contact_id": cid,
            "name":       _shape_responder_name(ack),
            "groups":     [],
            "emails":     ack.get("emails", []),
        }
        next_full_list.append(entry)
        new_arrivals.append(entry)
    return next_full_list, new_arrivals


# ---------------------------------------------------------------------------
# Helpers — keep tests short
# ---------------------------------------------------------------------------

def _utc(*args, **kwargs) -> datetime.datetime:
    return datetime.datetime(*args, **kwargs, tzinfo=datetime.timezone.utc)


def _doc(**overrides) -> dict:
    base = {
        "event_id":   "2026-04-25_mpd_calaveras_1430",
        "created_at": _utc(2026, 4, 25, 14, 30),   # 14:30 UTC
        "everbridge_mode": "safe",
        "slack_mode":      "shadow",
        "responders": [],
        "last_non_empty_responders": [],
    }
    base.update(overrides)
    return base


def _claims(**overrides) -> dict:
    base = {
        "iss":            "https://accounts.google.com",
        "email":          "everbridge-poll-sa@sar-dispatch-dev.iam.gserviceaccount.com",
        "email_verified": True,
        "aud":            "https://dispatch-console-970461953836.us-central1.run.app",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _check_oidc_claims()
# ---------------------------------------------------------------------------

class TestCheckOidcClaims:
    def _expected_email(self) -> str:
        return "everbridge-poll-sa@sar-dispatch-dev.iam.gserviceaccount.com"

    def _expected_audience(self) -> str:
        return "https://dispatch-console-970461953836.us-central1.run.app"

    def test_happy_path_returns_none(self):
        result = _check_oidc_claims(
            _claims(),
            self._expected_email(),
            self._expected_audience(),
        )
        assert result is None

    def test_alternate_issuer_form_accepted(self):
        # Google's metadata server uses both forms (with and without
        # https:// prefix) — both accepted.
        result = _check_oidc_claims(
            _claims(iss="accounts.google.com"),
            self._expected_email(),
            self._expected_audience(),
        )
        assert result is None

    def test_none_claims_401(self):
        result = _check_oidc_claims(None, self._expected_email(), self._expected_audience())
        assert result == (401, "Unauthorized")

    def test_empty_claims_401(self):
        result = _check_oidc_claims({}, self._expected_email(), self._expected_audience())
        assert result == (401, "Unauthorized")

    def test_wrong_issuer_401(self):
        result = _check_oidc_claims(
            _claims(iss="https://attacker.example.com"),
            self._expected_email(),
            self._expected_audience(),
        )
        assert result == (401, "Unauthorized")

    def test_email_not_verified_401(self):
        result = _check_oidc_claims(
            _claims(email_verified=False),
            self._expected_email(),
            self._expected_audience(),
        )
        assert result == (401, "Unauthorized")

    def test_email_missing_email_verified_401(self):
        # Defensive — claim missing entirely.
        c = _claims()
        c.pop("email_verified")
        result = _check_oidc_claims(c, self._expected_email(), self._expected_audience())
        assert result == (401, "Unauthorized")

    def test_wrong_email_401(self):
        result = _check_oidc_claims(
            _claims(email="someone-else@example.com"),
            self._expected_email(),
            self._expected_audience(),
        )
        assert result == (401, "Unauthorized")

    def test_wrong_audience_401(self):
        result = _check_oidc_claims(
            _claims(aud="https://attacker.run.app"),
            self._expected_email(),
            self._expected_audience(),
        )
        assert result == (401, "Unauthorized")

    def test_all_failure_modes_produce_identical_response(self):
        # Defense in depth — wrong issuer, wrong email, wrong audience,
        # missing claims all produce the same (401, "Unauthorized") so
        # an attacker probing the endpoint can't tell which check tripped.
        # Operator-visible reason is in Cloud Run logs only.
        e_email = self._expected_email()
        e_aud   = self._expected_audience()
        wrongs = [
            None,
            {},
            _claims(iss="https://attacker.example.com"),
            _claims(email_verified=False),
            _claims(email="other@example.com"),
            _claims(aud="https://attacker.run.app"),
        ]
        results = [_check_oidc_claims(c, e_email, e_aud) for c in wrongs]
        # All identical
        assert all(r == (401, "Unauthorized") for r in results)

    def test_empty_expected_email_fails_closed_even_with_matching_token(self):
        # PROJECT_ID env unset → _expected_poll_sa_email() returns "".
        # Even if a token arrives whose claims happen to also contain
        # empty fields (or the verify_oauth2_token SDK call somehow
        # passes without enforcing audience), this layer must reject.
        result = _check_oidc_claims(
            _claims(email="", aud=""),
            expected_email="",
            expected_audience=self._expected_audience(),
        )
        assert result == (401, "Unauthorized")

    def test_empty_expected_audience_fails_closed_even_with_matching_token(self):
        # CLOUD_RUN_SERVICE_URL env unset → _expected_oidc_audience() returns "".
        # Mirror of the above check for the audience field.
        result = _check_oidc_claims(
            _claims(email="", aud=""),
            expected_email=self._expected_email(),
            expected_audience="",
        )
        assert result == (401, "Unauthorized")

    def test_both_expected_values_empty_fails_closed(self):
        # Worst-case misconfiguration — both env vars unset. Must reject
        # every request, including a syntactically-valid empty-claims dict.
        result = _check_oidc_claims(
            _claims(email="", aud=""),
            expected_email="",
            expected_audience="",
        )
        assert result == (401, "Unauthorized")

    def test_empty_config_rejects_before_consulting_token(self):
        # When the deployment is misconfigured, we must not even read the
        # token claims — pass None claims with empty expected values and
        # confirm we still get the standard 401.
        result = _check_oidc_claims(
            None,
            expected_email="",
            expected_audience="",
        )
        assert result == (401, "Unauthorized")


# ---------------------------------------------------------------------------
# _render_terminal_header()
# ---------------------------------------------------------------------------

class TestRenderTerminalHeader:
    @pytest.mark.parametrize("reason,expected_phrase", [
        ("everbridge_closed", "Completed"),
        ("stopped",           "Stopped (manual EB UI)"),
        ("manual",            "Stopped (manual close)"),
        ("idle",              "idle (no new responders)"),
        ("hard_cap",          "4-hour cap reached"),
        ("error",             "polling errors exceeded tolerance"),
        ("draft_unsent",      "draft expired without send"),
    ])
    def test_known_reasons_get_distinct_phrases(self, reason, expected_phrase):
        # Each known stop_reason gets a distinct human phrase. The
        # dispatcher (and search management) reads the tally header to
        # know what happened — distinct phrases means at-a-glance
        # disambiguation.
        out = _render_terminal_header(reason)
        assert out == f"⏹ Everbridge STOPPED — {expected_phrase}"

    def test_each_reason_produces_unique_header(self):
        # No two known reasons collide on the same rendered string.
        reasons = ["everbridge_closed", "stopped", "manual", "idle",
                   "hard_cap", "error", "draft_unsent"]
        rendered = [_render_terminal_header(r) for r in reasons]
        assert len(set(rendered)) == len(rendered)

    def test_unknown_reason_falls_through_safely(self):
        # Defensive — unknown reason renders the raw string. The dispatcher
        # still sees a STOPPED header; the diagnostic appears in Cloud Run
        # logs.
        out = _render_terminal_header("unrecognized_future_reason")
        assert out == "⏹ Everbridge STOPPED — unrecognized_future_reason"


# ---------------------------------------------------------------------------
# _check_stop_conditions()
# ---------------------------------------------------------------------------

class TestCheckStopConditionsTerminalNotificationStatus:
    """Phase 0 Task 8: notificationStatus is the stop signal — NOT `status`."""

    def test_completed_returns_everbridge_closed(self):
        result = _check_stop_conditions(
            doc=_doc(),
            poll_response={"notif_status": "Completed"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),     # only 2 min after creation
        )
        assert result == "everbridge_closed"

    def test_stopped_returns_stopped(self):
        result = _check_stop_conditions(
            doc=_doc(),
            poll_response={"notif_status": "Stopped"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result == "stopped"

    def test_active_does_NOT_stop(self):
        # Phase 0 Task 8 — `Active` is in-progress, NOT terminal.
        result = _check_stop_conditions(
            doc=_doc(),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result is None

    def test_inprogress_does_NOT_stop(self):
        # Phase 0 Task 9 — discovery query returns "Inprogress" right after
        # dispatcher Send. Must NOT terminate.
        result = _check_stop_conditions(
            doc=_doc(),
            poll_response={"notif_status": "Inprogress"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result is None

    def test_status_A_alone_does_NOT_stop(self):
        # Phase 0 Task 8: parent `status: "A"` is "A" for every notification
        # regardless of lifecycle. Must NOT trigger stop. The parser strips
        # this from poll_response so we never see it here, but defense in
        # depth: even if it leaked, it's not in our terminal set.
        result = _check_stop_conditions(
            doc=_doc(),
            poll_response={"notif_status": "A"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result is None

    def test_no_poll_response_skips_first_check(self):
        # poll_response=None happens when the SDK call raised — caller
        # marks first_error_at and we proceed to the other checks.
        result = _check_stop_conditions(
            doc=_doc(),
            poll_response=None,
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        # No other condition met → None
        assert result is None


class TestCheckStopConditionsManualClose:
    def test_manual_stop_requested_returns_manual(self):
        result = _check_stop_conditions(
            doc=_doc(manual_stop_requested=True),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result == "manual"

    def test_manual_takes_precedence_over_idle(self):
        # If both manual and idle would fire, manual wins (it's the
        # explicit dispatcher action).
        result = _check_stop_conditions(
            doc=_doc(manual_stop_requested=True,
                     created_at=_utc(2026, 4, 25, 13, 0)),  # 1.5h ago, idle
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result == "manual"

    def test_completed_takes_precedence_over_manual(self):
        # If EB ran to natural Completion AND manual_stop_requested,
        # everbridge_closed wins — Completed has its own meaning (everyone
        # confirmed / template policy fully fired) distinct from manual close.
        result = _check_stop_conditions(
            doc=_doc(manual_stop_requested=True),
            poll_response={"notif_status": "Completed"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result == "everbridge_closed"

    def test_manual_takes_precedence_over_stopped_notif_status(self):
        # Issue #330: /close-incident-polling sets BOTH manual_stop_requested
        # AND PUTs the EB notification to Stopped. On the next polling tick
        # both signals are present; "manual" must win so the #active-incidents
        # tally reads "Stopped (manual close)" not "Stopped (manual EB UI)".
        result = _check_stop_conditions(
            doc=_doc(manual_stop_requested=True),
            poll_response={"notif_status": "Stopped"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result == "manual"

    def test_stopped_without_flag_still_returns_stopped(self):
        # If EB shows Stopped but our flag is NOT set, someone clicked Stop
        # in the EB UI directly — that's the original "stopped" reason.
        result = _check_stop_conditions(
            doc=_doc(),  # no manual_stop_requested
            poll_response={"notif_status": "Stopped"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result == "stopped"


class TestCheckStopConditionsHardCap:
    def test_at_4hr_returns_hard_cap(self):
        result = _check_stop_conditions(
            doc=_doc(created_at=_utc(2026, 4, 25, 10, 30)),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 30),     # exactly 4h after
        )
        assert result == "hard_cap"

    def test_just_under_4hr_does_NOT_stop(self):
        # Use a recent last_responder_at to avoid idle firing first
        # (idle fires before hard_cap in priority order).
        result = _check_stop_conditions(
            doc=_doc(
                created_at=_utc(2026, 4, 25, 10, 30),
                last_responder_at=_utc(2026, 4, 25, 14, 25),  # 5 min ago
            ),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 29, 59),  # 4h - 1s
        )
        assert result is None

    def test_no_created_at_skips_check(self):
        # Defensive — missing created_at shouldn't crash.
        d = _doc()
        d.pop("created_at")
        # Also remove last_responder_at fallback chain to avoid idle firing
        d["last_responder_at"] = None
        result = _check_stop_conditions(
            doc=d,
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result is None


class TestCheckStopConditionsIdle:
    def test_60_min_no_responder_returns_idle(self):
        # last_responder_at = 60 min ago (or never; fall back to created_at)
        result = _check_stop_conditions(
            doc=_doc(created_at=_utc(2026, 4, 25, 14, 0)),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 15, 0),     # exactly 60 min
        )
        assert result == "idle"

    def test_recent_responder_does_NOT_idle(self):
        # last_responder_at within 60 min — keep polling.
        result = _check_stop_conditions(
            doc=_doc(
                created_at=_utc(2026, 4, 25, 13, 0),         # 2h ago
                last_responder_at=_utc(2026, 4, 25, 14, 25), # 35 min ago — still under 60 min
            ),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 15, 0),
        )
        assert result is None

    def test_just_under_60_min_does_NOT_idle(self):
        result = _check_stop_conditions(
            doc=_doc(created_at=_utc(2026, 4, 25, 14, 0, 1)),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 15, 0),
        )
        assert result is None


class TestCheckStopConditionsError:
    def test_full_mode_tolerance_at_10_min(self):
        # Full mode tolerance is 600s (10 min) — pass it as
        # mode_tolerance_s.  Just-at-tolerance fires.
        result = _check_stop_conditions(
            doc=_doc(first_error_at=_utc(2026, 4, 25, 14, 20)),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=10 * 60,
            now_dt=_utc(2026, 4, 25, 14, 30),
        )
        assert result == "error"

    def test_full_mode_just_under_tolerance_does_NOT_error_stop(self):
        result = _check_stop_conditions(
            doc=_doc(first_error_at=_utc(2026, 4, 25, 14, 20, 1)),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=10 * 60,
            now_dt=_utc(2026, 4, 25, 14, 30),
        )
        assert result is None

    def test_safe_mode_tolerance_at_30_min(self):
        # Safe mode tolerance is 1800s (30 min). At-tolerance fires.
        # But idle fires first if last_responder_at + 60 min has passed —
        # explicitly use a recent last_responder_at to avoid idle.
        result = _check_stop_conditions(
            doc=_doc(
                first_error_at=_utc(2026, 4, 25, 14, 0),
                last_responder_at=_utc(2026, 4, 25, 14, 29),  # 1 min ago
            ),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=30 * 60,
            now_dt=_utc(2026, 4, 25, 14, 30),
        )
        assert result == "error"

    def test_no_first_error_at_skips_check(self):
        # Common case — no errors yet. Don't fire.
        result = _check_stop_conditions(
            doc=_doc(first_error_at=None),
            poll_response={"notif_status": "Active"},
            mode_tolerance_s=600,
            now_dt=_utc(2026, 4, 25, 14, 32),
        )
        assert result is None


class TestCheckStopConditionsPriorityOrder:
    def test_priority_order_pinned(self):
        # When multiple conditions fire simultaneously, the priority order is:
        #   1. notificationStatus terminal
        #   2. manual_stop_requested
        #   3. hard_cap
        #   4. idle
        #   5. error
        # This test pins the order via a "everything's stopped" doc.
        d = _doc(
            manual_stop_requested=True,
            created_at=_utc(2026, 4, 25, 10, 0),         # 4.5h ago — hard_cap
            last_responder_at=_utc(2026, 4, 25, 13, 0),  # 1.5h ago — idle
            first_error_at=_utc(2026, 4, 25, 14, 0),     # 30 min ago — error
        )
        # 1. EB terminal wins:
        result1 = _check_stop_conditions(
            doc=d, poll_response={"notif_status": "Completed"},
            mode_tolerance_s=600, now_dt=_utc(2026, 4, 25, 14, 30),
        )
        assert result1 == "everbridge_closed"
        # 2. With EB Active, manual wins:
        result2 = _check_stop_conditions(
            doc=d, poll_response={"notif_status": "Active"},
            mode_tolerance_s=600, now_dt=_utc(2026, 4, 25, 14, 30),
        )
        assert result2 == "manual"
        # 3. Without manual, hard_cap wins:
        d3 = dict(d); d3["manual_stop_requested"] = False
        result3 = _check_stop_conditions(
            doc=d3, poll_response={"notif_status": "Active"},
            mode_tolerance_s=600, now_dt=_utc(2026, 4, 25, 14, 30),
        )
        assert result3 == "hard_cap"


# ---------------------------------------------------------------------------
# _apply_responder_diff()
# ---------------------------------------------------------------------------

class TestApplyResponderDiff:
    def test_empty_prev_empty_new_returns_empty(self):
        next_full, arrivals = _apply_responder_diff([], [])
        assert next_full == []
        assert arrivals == []

    def test_one_new_arrival(self):
        prev = []
        new_acks = [{"contact_id": "c1", "first_name": "Bill", "last_name": "Burns",
                     "emails": ["bill@example.com"]}]
        next_full, arrivals = _apply_responder_diff(prev, new_acks)
        assert len(next_full) == 1
        assert len(arrivals) == 1
        assert arrivals[0]["contact_id"] == "c1"
        assert arrivals[0]["name"]       == "Burns, Bill"
        assert arrivals[0]["emails"]     == ["bill@example.com"]
        assert arrivals[0]["groups"]     == []   # filled in elsewhere

    def test_no_new_arrivals_when_prev_already_has_responder(self):
        # Same contact_id appearing again across cycles is the steady state
        # — must NOT trigger duplicate Slack action.
        prev = [{"contact_id": "c1", "name": "Burns, Bill", "groups": [], "emails": []}]
        new_acks = [{"contact_id": "c1", "first_name": "Bill", "last_name": "Burns"}]
        next_full, arrivals = _apply_responder_diff(prev, new_acks)
        assert next_full == prev
        assert arrivals == []

    def test_partial_data_falls_back_safely(self):
        # Defensive — ack with only first_name renders cleanly.
        new_acks = [{"contact_id": "c1", "first_name": "Bill"}]
        next_full, arrivals = _apply_responder_diff([], new_acks)
        assert arrivals[0]["name"] == "Bill"

        new_acks = [{"contact_id": "c2", "last_name": "Burns"}]
        next_full, arrivals = _apply_responder_diff([], new_acks)
        assert arrivals[0]["name"] == "Burns"

        # No name at all — fall back to contact_id so the line still renders.
        new_acks = [{"contact_id": "c3"}]
        next_full, arrivals = _apply_responder_diff([], new_acks)
        assert arrivals[0]["name"] == "c3"

    def test_skips_acks_with_no_contact_id(self):
        # Defensive — Everbridge response shape error shouldn't crash.
        new_acks = [{"first_name": "X"}, {"contact_id": "c1", "first_name": "Y"}]
        next_full, arrivals = _apply_responder_diff([], new_acks)
        assert len(arrivals) == 1
        assert arrivals[0]["contact_id"] == "c1"

    def test_mixed_existing_and_new(self):
        prev = [
            {"contact_id": "c1", "name": "Burns, Bill",  "groups": ["K9"], "emails": []},
            {"contact_id": "c2", "name": "Black, Kris",  "groups": [],     "emails": []},
        ]
        new_acks = [
            # c1 already there — skip
            {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns"},
            # c3 is new
            {"contact_id": "c3", "first_name": "Janae", "last_name": "Cubeiro"},
        ]
        next_full, arrivals = _apply_responder_diff(prev, new_acks)
        assert len(next_full) == 3
        # Original groups preserved in next_full
        assert next_full[0]["groups"] == ["K9"]
        # Only c3 in arrivals
        assert len(arrivals) == 1
        assert arrivals[0]["contact_id"] == "c3"
        assert arrivals[0]["name"]       == "Cubeiro, Janae"

    def test_does_not_mutate_input_lists(self):
        prev = [{"contact_id": "c1", "name": "X", "groups": [], "emails": []}]
        new_acks = [{"contact_id": "c2", "first_name": "Y"}]
        prev_copy = list(prev)
        new_copy  = list(new_acks)
        _apply_responder_diff(prev, new_acks)
        assert prev == prev_copy
        assert new_acks == new_copy

    def test_emails_propagate_for_full_mode_invite(self):
        # Full slack_mode uses the email to lookup_user_by_email — must
        # be carried through from the ack.
        new_acks = [{"contact_id": "c1", "first_name": "Bill", "last_name": "Burns",
                     "emails": ["bill@sccssar.org", "bill-alt@example.com"]}]
        _, arrivals = _apply_responder_diff([], new_acks)
        assert arrivals[0]["emails"] == ["bill@sccssar.org", "bill-alt@example.com"]


# ---------------------------------------------------------------------------
# D4H per-YES enqueue selection logic (PR 6)
# ---------------------------------------------------------------------------
# Mirror of the decision logic in poll_incident that selects which new arrivals
# need a D4H per-YES Cloud Task. Pure-logic; does not call enqueue itself.
# Orchestration (actual Cloud Tasks call) tested at live-test time on personal-dev.
# ---------------------------------------------------------------------------

def _d4h_yes_enqueue_items(doc: dict, new_arrivals: list) -> list:
    """Mirror of the D4H per-YES selection logic in poll_incident (PR 6).

    Returns list of (email, groups) pairs — one per arrival with a resolvable
    email — when d4h_activity_id is set. Returns [] on graceful-degrade
    (no d4h_activity_id means D4H incident create failed at dispatch time).
    """
    if not doc.get("d4h_activity_id"):
        return []
    items = []
    for arr in new_arrivals:
        email = (arr.get("emails") or [""])[0]
        if not email:
            continue
        items.append((email, list(arr.get("groups", []))))
    return items


class TestD4HPerYesEnqueueItems:
    """Selection logic for D4H per-YES enqueue in poll_incident."""

    def test_returns_email_and_groups_per_arrival(self):
        """Group names reflect Kris's 2026-05-19 EB rebuild — `Canine`
        (was `SAR - Canine Team`, DOGS-* collapsed in), `UAS` (was
        `SAR - UAS Team`)."""
        doc = {"d4h_activity_id": 1616002}
        arrivals = [
            {"emails": ["bill@sccssar.org"], "groups": ["Canine"]},
            {"emails": ["kris@sccssar.org"], "groups": ["Canine", "UAS"]},
        ]
        items = _d4h_yes_enqueue_items(doc, arrivals)
        assert items == [
            ("bill@sccssar.org", ["Canine"]),
            ("kris@sccssar.org", ["Canine", "UAS"]),
        ]

    def test_graceful_degrade_when_no_activity_id(self):
        """D4H incident create failed at dispatch — skip enqueue silently."""
        doc = {}  # d4h_activity_id absent
        arrivals = [{"emails": ["bill@sccssar.org"], "groups": []}]
        assert _d4h_yes_enqueue_items(doc, arrivals) == []

    def test_graceful_degrade_when_activity_id_is_none(self):
        doc = {"d4h_activity_id": None}
        arrivals = [{"emails": ["bill@sccssar.org"], "groups": []}]
        assert _d4h_yes_enqueue_items(doc, arrivals) == []

    def test_skips_arrival_with_empty_email(self):
        doc = {"d4h_activity_id": 1616002}
        arrivals = [
            {"emails": [], "groups": ["Ground Searcher"]},
            {"emails": ["bill@sccssar.org"], "groups": []},
        ]
        items = _d4h_yes_enqueue_items(doc, arrivals)
        assert len(items) == 1
        assert items[0][0] == "bill@sccssar.org"

    def test_skips_arrival_with_missing_emails_key(self):
        doc = {"d4h_activity_id": 1616002}
        arrivals = [{"groups": ["Ground Searcher"]}]
        assert _d4h_yes_enqueue_items(doc, arrivals) == []

    def test_uses_first_email_only(self):
        """Only the first email is enqueued — matches Slack lookup behavior."""
        doc = {"d4h_activity_id": 1616002}
        arrivals = [{"emails": ["primary@sccssar.org", "backup@gmail.com"], "groups": []}]
        items = _d4h_yes_enqueue_items(doc, arrivals)
        assert items == [("primary@sccssar.org", [])]

    def test_empty_arrivals_returns_empty(self):
        doc = {"d4h_activity_id": 1616002}
        assert _d4h_yes_enqueue_items(doc, []) == []

    def test_groups_default_to_empty_list_when_absent(self):
        doc = {"d4h_activity_id": 1616002}
        arrivals = [{"emails": ["bill@sccssar.org"]}]
        items = _d4h_yes_enqueue_items(doc, arrivals)
        assert items == [("bill@sccssar.org", [])]
