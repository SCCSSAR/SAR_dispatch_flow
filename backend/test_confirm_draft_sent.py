"""test_confirm_draft_sent.py — pure-logic tests for the Phase 0 Task 9
manual-fallback validator (Task 1.10c).

Per CLAUDE.md test file pattern: mirror the pure-logic functions from
`backend/main.py` locally rather than importing the module directly.
`backend/main.py` has heavyweight GCP / Vertex AI / httpx / google-api
dependencies not installed in local pytest.

When updating the helpers in main.py, ALSO update the mirror here. The
mirror IS the test contract — drift surfaces in production behavior,
and these tests are the regression boundary.

Test coverage (Task 1.10c):
  - _validate_confirm_draft_sent() — exhaustive matrix of (doc-state,
    requester-identity) combinations, each with the expected
    (status_code, detail) tuple.

NOT exercised here (covered at live-test time on personal-dev):
  - The /confirm-draft-sent endpoint orchestration (Firestore
    read/update, Everbridge best-effort verification, Slack tally
    edit) — those are I/O wrappers around the validator.
"""
import pytest


# ---------------------------------------------------------------------------
# Mirrored helper — must be kept in sync with backend/main.py
# ---------------------------------------------------------------------------

def _validate_confirm_draft_sent(
    doc: dict | None,
    user_email: str,
) -> tuple[int, str] | None:
    """Mirror of backend/main.py::_validate_confirm_draft_sent()."""
    if not doc or doc.get("dispatcher_email") != user_email:
        return (404, "Incident not found or not yours")
    if doc.get("notification_id"):
        return (
            409,
            "Incident already has a notification_id; nothing to confirm",
        )
    status = doc.get("status")
    if status not in ("polling", "pre_discovery"):
        return (
            409,
            f"Incident is in terminal state {status!r}",
        )
    return None


# ---------------------------------------------------------------------------
# Helpers — keep test arrange short
# ---------------------------------------------------------------------------

def _doc(**overrides) -> dict:
    """Build a happy-path doc; overrides patch fields as needed."""
    base = {
        "event_id":         "2026-04-25_mpd_calaveras_1430",
        "dispatcher_email": "bill@sccssar.org",
        "notification_id":  None,
        "template_id":      "TPL_321",
        "status":           "polling",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestValidateConfirmDraftSentHappyPath:
    def test_owner_polling_no_nid_returns_none(self):
        # The canonical valid request — incident is in pre-/post-discovery
        # polling, no notification_id has been recorded yet, and the
        # requester owns the doc.
        result = _validate_confirm_draft_sent(
            _doc(),
            "bill@sccssar.org",
        )
        assert result is None

    def test_pre_discovery_status_also_valid(self):
        # Task 1.11 introduces a 'pre_discovery' status during the
        # safe-mode auto-discovery window. Both 'polling' and
        # 'pre_discovery' are valid for manual-confirm.
        result = _validate_confirm_draft_sent(
            _doc(status="pre_discovery"),
            "bill@sccssar.org",
        )
        assert result is None


# ---------------------------------------------------------------------------
# Ownership / not-found — 404
# ---------------------------------------------------------------------------

class TestValidateConfirmDraftSentOwnership:
    def test_doc_missing_returns_404(self):
        # Firestore .get() returned no doc.
        result = _validate_confirm_draft_sent(None, "bill@sccssar.org")
        assert result == (404, "Incident not found or not yours")

    def test_doc_empty_dict_returns_404(self):
        # Defensive — `not doc` covers None AND empty dict (the latter
        # would be unexpected from Firestore but worth pinning).
        result = _validate_confirm_draft_sent({}, "bill@sccssar.org")
        assert result == (404, "Incident not found or not yours")

    def test_different_dispatcher_returns_404(self):
        # Authorized dispatcher A trying to confirm dispatcher B's
        # incident — same response as not-found so we don't disclose
        # whether the event_id even exists.
        result = _validate_confirm_draft_sent(
            _doc(dispatcher_email="dana@sccssar.org"),
            "bill@sccssar.org",
        )
        assert result == (404, "Incident not found or not yours")

    def test_response_for_missing_and_wrong_owner_are_identical(self):
        # Defense-in-depth — the two failure modes produce the same
        # response so an attacker can't differentiate "exists but not
        # yours" from "doesn't exist". Pinned as a regression test.
        not_found  = _validate_confirm_draft_sent(None, "x@example.com")
        wrong_user = _validate_confirm_draft_sent(
            _doc(dispatcher_email="other@example.com"),
            "x@example.com",
        )
        assert not_found == wrong_user

    def test_empty_user_email_does_not_match_doc_with_email(self):
        # Defensive — auth dependency returns email='' on misconfigured
        # token. Must NOT validate as ownership of any real doc.
        result = _validate_confirm_draft_sent(
            _doc(dispatcher_email="bill@sccssar.org"),
            "",
        )
        assert result == (404, "Incident not found or not yours")


# ---------------------------------------------------------------------------
# Already-confirmed — 409
# ---------------------------------------------------------------------------

class TestValidateConfirmDraftSentAlreadyConfirmed:
    def test_existing_nid_returns_409(self):
        # The polling chain (Task 1.11) auto-discovered the notification_id
        # before the dispatcher could manual-confirm — nothing to do.
        result = _validate_confirm_draft_sent(
            _doc(notification_id="7000000000000016"),
            "bill@sccssar.org",
        )
        assert result == (
            409,
            "Incident already has a notification_id; nothing to confirm",
        )

    def test_409_takes_precedence_over_terminal_status(self):
        # Edge case — incident has both a notification_id AND a terminal
        # status (race between manual-confirm and auto-stop). Return the
        # already-confirmed 409, not the terminal-state 409, so the
        # dispatcher's UI gives the more accurate "already done" message.
        result = _validate_confirm_draft_sent(
            _doc(notification_id="N1", status="stopped_idle"),
            "bill@sccssar.org",
        )
        assert result == (
            409,
            "Incident already has a notification_id; nothing to confirm",
        )


# ---------------------------------------------------------------------------
# Terminal status — 409
# ---------------------------------------------------------------------------

class TestValidateConfirmDraftSentTerminalStatus:
    @pytest.mark.parametrize("terminal_status", [
        "stopped_everbridge_closed",
        "stopped_idle",
        "stopped_hard_cap",
        "stopped_error",
        "stopped_draft_unsent",
        "stopped_manual",
    ])
    def test_each_known_terminal_status_returns_409(self, terminal_status):
        # All Task 1.11 stop-reason → status mappings should refuse
        # manual-confirm. Resurrecting a stopped chain via manual-confirm
        # would skip the cleanup the stop did (TTL set, channel header
        # flipped to STOPPED) — the dispatcher should start a new
        # incident instead.
        result = _validate_confirm_draft_sent(
            _doc(status=terminal_status),
            "bill@sccssar.org",
        )
        assert result is not None
        assert result[0] == 409
        assert "terminal state" in result[1]
        # The status string is included so the dispatcher's UI can show
        # which terminal state the incident's in.
        assert terminal_status in result[1]

    def test_unknown_status_treated_as_terminal(self):
        # Defensive — any status string not in the allowed set triggers
        # the terminal-state 409. A future schema bug that stamps an
        # unrecognized value should fail loud.
        result = _validate_confirm_draft_sent(
            _doc(status="completely_unexpected_value"),
            "bill@sccssar.org",
        )
        assert result is not None
        assert result[0] == 409
        assert "terminal state" in result[1]

    def test_missing_status_field_treated_as_terminal(self):
        # Defensive — a doc missing the status field (shouldn't happen
        # post-incidents.py Task 1.8, but defensive) — manual-confirm
        # refuses rather than assuming a default.
        doc = _doc()
        doc.pop("status", None)
        result = _validate_confirm_draft_sent(doc, "bill@sccssar.org")
        assert result is not None
        assert result[0] == 409


# ---------------------------------------------------------------------------
# Validation order — pinned for stable error reporting
# ---------------------------------------------------------------------------

class TestValidationOrder:
    def test_ownership_check_runs_before_nid_check(self):
        # An authorized dispatcher querying someone else's incident must
        # get the 404, NOT the 409 ("already confirmed") — even if the
        # other dispatcher's incident does have a notification_id. We
        # don't disclose ownership status to non-owners.
        result = _validate_confirm_draft_sent(
            _doc(dispatcher_email="other@example.com",
                 notification_id="N1"),
            "bill@sccssar.org",
        )
        assert result == (404, "Incident not found or not yours")

    def test_ownership_check_runs_before_status_check(self):
        # Same principle — terminal-state info is also not disclosed to
        # non-owners.
        result = _validate_confirm_draft_sent(
            _doc(dispatcher_email="other@example.com",
                 status="stopped_idle"),
            "bill@sccssar.org",
        )
        assert result == (404, "Incident not found or not yours")
