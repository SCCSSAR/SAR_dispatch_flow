"""test_incidents.py — pure-logic tests for the incidents.py data shape.

Direct imports work here (incidents.py is pure stdlib — datetime + typing
only) so we don't need the local-mirror pattern used in test_everbridge.py
and test_slack.py. When incidents.py grows to take Firestore-SDK calls
(it currently doesn't — those live in main.py /Task 1.10), revisit and
mirror at that point.
"""
from datetime import datetime, timezone

import pytest

from backend.incidents import (
    FULL_MODE_ERROR_TOLERANCE_S,
    FULL_MODE_POLL_INTERVAL_S,
    HARD_CAP_S,
    IDLE_S,
    SAFE_MODE_ERROR_TOLERANCE_S,
    SAFE_MODE_POLL_INTERVAL_S,
    TTL_AFTER_CLOSE_S,
    new_incident_doc,
    new_skeleton_incident_doc,
    now_utc,
    stop_incident,
)


# ---------------------------------------------------------------------------
# Mode-dependent timing constants (design Section 3, Item 2 amendment)
# ---------------------------------------------------------------------------

class TestTimingConstants:
    def test_safe_and_full_poll_interval_equal_15s(self):
        # Item 2: same cadence in both modes. Earlier drafts had 60s (safe)
        # and 15s (full). Slowing safe-mode polling would mask the live
        # Slack tally that dispatchers + search mgmt watch during shadow
        # mode — exactly the UI behavior we need to see end-to-end before
        # cutover.
        assert SAFE_MODE_POLL_INTERVAL_S == 15
        assert FULL_MODE_POLL_INTERVAL_S == 15

    def test_safe_mode_error_tolerance_30_minutes(self):
        # Safe mode is more lenient — dispatcher is engaged with the
        # Everbridge UI and transient discovery failures are common.
        assert SAFE_MODE_ERROR_TOLERANCE_S == 30 * 60

    def test_full_mode_error_tolerance_10_minutes(self):
        # Full mode is tighter — dispatcher has handed off and we should
        # fail visibly faster.
        assert FULL_MODE_ERROR_TOLERANCE_S == 10 * 60

    def test_hard_cap_4_hours(self):
        # Hard cap matches the longest realistic SCCSSAR callout duration.
        assert HARD_CAP_S == 4 * 60 * 60

    def test_idle_60_minutes(self):
        # Stop after 60min with no new YES — prevents an open-ended chain
        # when an incident effectively concludes but Everbridge hasn't
        # expired the notification yet. 60min matches the team's
        # stand-down policy; 30min was found too aggressive after the
        # 2026-05-07 SJSU live incident review.
        assert IDLE_S == 60 * 60

    def test_ttl_after_close_24_hours(self):
        # The Terraform google_firestore_field.incidents_ttl resource
        # reaps the doc when wall-clock passes `expire_at`. This constant
        # is the single source for the value written into that field at
        # stop time.
        assert TTL_AFTER_CLOSE_S == 24 * 60 * 60


# ---------------------------------------------------------------------------
# now_utc()
# ---------------------------------------------------------------------------

class TestNowUtc:
    def test_returns_aware_utc_datetime(self):
        # tzinfo must be set so timestamps written to Firestore are
        # unambiguous across runtimes.
        ts = now_utc()
        assert isinstance(ts, datetime)
        assert ts.tzinfo is timezone.utc


# ---------------------------------------------------------------------------
# new_incident_doc() — initial doc shape
# ---------------------------------------------------------------------------

def _live_path_kwargs(**overrides):
    base = dict(
        event_id="2026-04-25_mpd_calaveras_1430",
        event_name="2026-04-25 MPD CALAVERAS 1430",
        event_name_human="2026-04-25 MPD CALAVERAS",
        dispatcher_email="bill@example.com",
        everbridge_event_id="EVT_456",
        notification_id="NOTIF_789",
        template_id=None,
        slack_channel_id="C123",
        slack_channel_name="2026-04-25_mpd_calaveras",
        active_incidents_ts="1234.5678",
        welcome_ts="9876.5432",
        staging_ts="9876.6000",
        caltopo_ts="9876.6543",
        everbridge_mode="full",
        slack_mode="full",
        action="send_live",
        selected_target_ids=["g1", "g2"],
        requested_group_names=["K9", "UAS"],
        contact_group_map={"c1": ["K9"], "c2": ["K9", "UAS"]},
        contact_email_map={"c1": ["bill@example.com"], "c2": ["kris@example.com"]},
    )
    base.update(overrides)
    return base


def _safe_draft_kwargs(**overrides):
    base = dict(
        event_id="2026-04-25_mpd_calaveras_1430",
        event_name="2026-04-25 MPD CALAVERAS 1430",
        event_name_human="2026-04-25 MPD CALAVERAS",
        dispatcher_email="bill@example.com",
        everbridge_event_id="EVT_456",
        notification_id=None,
        template_id="TPL_321",
        slack_channel_id="C123",
        slack_channel_name="2026-04-25_mpd_calaveras",
        active_incidents_ts="1234.5678",
        welcome_ts="9876.5432",
        staging_ts="9876.6000",
        caltopo_ts="",
        everbridge_mode="safe",
        slack_mode="shadow",
        action="send_draft",
        selected_target_ids=["c1"],
        requested_group_names=[],
        contact_group_map={},
        contact_email_map={},
    )
    base.update(overrides)
    return base


class TestNewIncidentDocLivePath:
    def test_required_keys_present_and_correct(self):
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["event_id"] == "2026-04-25_mpd_calaveras_1430"
        assert doc["event_name"].endswith(" 1430")            # canonical retains HHMM
        assert doc["event_name_human"] == "2026-04-25 MPD CALAVERAS"
        assert doc["everbridge_event_id"] == "EVT_456"
        assert doc["notification_id"] == "NOTIF_789"
        assert doc["template_id"] is None
        assert doc["slack_channel_id"] == "C123"
        assert doc["slack_channel_name"] == "2026-04-25_mpd_calaveras"
        assert doc["everbridge_mode"] == "full"
        assert doc["slack_mode"] == "full"
        assert doc["action"] == "send_live"

    def test_initial_status_is_polling(self):
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["status"] == "polling"
        assert doc["stop_reason"] is None

    def test_slack_status_defaults_ok(self):
        # Step 6 best-effort guard: new_incident_doc() defaults slack_status to
        # "ok" / slack_error to None; /send-notification overwrites both before
        # the final .set() with the real channel-creation outcome (same pattern
        # as d4h_status). The keys MUST exist at creation so /dispatch-status
        # and the frontend can read them without a key-existence check.
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["slack_status"] == "ok"
        assert doc["slack_error"] is None

    def test_responder_lists_start_empty(self):
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["responders"] == []
        # Phase 0 Task 8 cache field must be present at creation so the
        # poll handler can update it without checking key existence.
        assert doc["last_non_empty_responders"] == []

    def test_timestamps_set_correctly_at_creation(self):
        doc = new_incident_doc(**_live_path_kwargs())
        # created_at is set; the polling/responder/error timestamps are
        # populated by the /poll-incident handler in main.py.
        assert isinstance(doc["created_at"], datetime)
        assert doc["created_at"].tzinfo is timezone.utc
        assert doc["last_poll_at"] is None
        assert doc["last_responder_at"] is None
        assert doc["first_error_at"] is None

    def test_expire_at_is_none_at_creation(self):
        # TTL field is only populated on stop_incident() so the Firestore
        # TTL service doesn't reap a polling-in-progress doc.
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["expire_at"] is None

    def test_selected_target_ids_preserved(self):
        # Stored as IDs only — no PII (no names, no emails). The Slack
        # group display in main.py resolves IDs to names at render time.
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["selected_target_ids"] == ["g1", "g2"]

    def test_selected_target_ids_is_a_copy(self):
        # Caller's list mutation must not bleed into the doc.
        ids = ["g1", "g2"]
        doc = new_incident_doc(**_live_path_kwargs(selected_target_ids=ids))
        ids.append("g3")
        assert doc["selected_target_ids"] == ["g1", "g2"]

    def test_contact_group_map_stored(self):
        cgm = {"c1": ["K9"], "c2": ["K9", "UAS"]}
        doc = new_incident_doc(**_live_path_kwargs(contact_group_map=cgm))
        assert doc["contact_group_map"] == cgm

    def test_contact_group_map_is_a_copy(self):
        cgm = {"c1": ["K9"]}
        doc = new_incident_doc(**_live_path_kwargs(contact_group_map=cgm))
        cgm["c9"] = ["Extra"]
        assert "c9" not in doc["contact_group_map"]

    def test_contact_group_map_empty_for_direct_send(self):
        doc = new_incident_doc(**_live_path_kwargs(contact_group_map={}))
        assert doc["contact_group_map"] == {}

    def test_contact_group_map_none_normalizes_to_empty(self):
        doc = new_incident_doc(**_live_path_kwargs(contact_group_map=None))
        assert doc["contact_group_map"] == {}

    def test_contact_email_map_stored(self):
        cem = {"c1": ["bill@example.com"], "c2": ["kris@example.com"]}
        doc = new_incident_doc(**_live_path_kwargs(contact_email_map=cem))
        assert doc["contact_email_map"] == cem

    def test_contact_email_map_is_a_copy(self):
        cem = {"c1": ["bill@example.com"]}
        doc = new_incident_doc(**_live_path_kwargs(contact_email_map=cem))
        cem["c9"] = ["extra@example.com"]
        assert "c9" not in doc["contact_email_map"]

    def test_contact_email_map_none_normalizes_to_empty(self):
        doc = new_incident_doc(**_live_path_kwargs(contact_email_map=None))
        assert doc["contact_email_map"] == {}

    def test_slack_dm_sent_user_ids_defaults_to_empty_list(self):
        # PR-after-#568 fix for the poll-time duplicate-DM bug: field must
        # exist in the doc with an empty-list default so poll-time's
        # doc.get("slack_dm_sent_user_ids") never returns None. Existing
        # callers that don't pass this kwarg get [] — safe default.
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["slack_dm_sent_user_ids"] == []

    def test_slack_dm_sent_user_ids_preserved(self):
        # Send-time Step 7b accumulates user_ids in a Python-local list and
        # passes them here. The final .set(new_incident_doc(...)) at Step 11
        # must re-write the field with the accumulated values — otherwise
        # send-time's ArrayUnion patches get overwritten to empty and
        # poll-time fires duplicate DMs. See PR-after-#568.
        uids = ["U123", "U456"]
        doc = new_incident_doc(
            **_live_path_kwargs(slack_dm_sent_user_ids=uids),
        )
        assert doc["slack_dm_sent_user_ids"] == ["U123", "U456"]

    def test_slack_dm_sent_user_ids_is_a_copy(self):
        # Callers must not be able to mutate the stored list via their own
        # reference — same defensive-copy pattern as selected_target_ids and
        # requested_group_names.
        uids = ["U123"]
        doc = new_incident_doc(
            **_live_path_kwargs(slack_dm_sent_user_ids=uids),
        )
        uids.append("U999")
        assert "U999" not in doc["slack_dm_sent_user_ids"]

    def test_slack_dm_sent_user_ids_none_normalizes_to_empty(self):
        # None → [] for the shadow-mode path (no DMs sent, but the field
        # must still exist so poll-time reads a valid empty list).
        doc = new_incident_doc(
            **_live_path_kwargs(slack_dm_sent_user_ids=None),
        )
        assert doc["slack_dm_sent_user_ids"] == []

    def test_contact_email_map_empty_for_group_only_send(self):
        # Group-member emails fetched by list_group_member_contacts; if that
        # call fails the map is {} (safe default — invite won't fire).
        doc = new_incident_doc(**_live_path_kwargs(contact_email_map={}))
        assert doc["contact_email_map"] == {}

    def test_returned_doc_is_a_fresh_dict(self):
        # Two calls must not share mutable state — Firestore writes the
        # exact dict so any cross-doc bleed is a real bug.
        a = new_incident_doc(**_live_path_kwargs())
        b = new_incident_doc(**_live_path_kwargs())
        assert a is not b
        assert a["responders"] is not b["responders"]
        a["responders"].append("X")
        assert b["responders"] == []


class TestNewIncidentDocSafeDraftPath:
    def test_template_id_set_notification_id_none(self):
        doc = new_incident_doc(**_safe_draft_kwargs())
        assert doc["template_id"] == "TPL_321"
        assert doc["notification_id"] is None

    def test_action_send_draft(self):
        doc = new_incident_doc(**_safe_draft_kwargs())
        assert doc["action"] == "send_draft"

    def test_modes_safe_and_shadow(self):
        # Phase 1 default safe combo. The (safe, full) combo is rejected
        # at module-import time in main.py; this test pins that the data
        # shape for (safe, shadow) goes through cleanly.
        doc = new_incident_doc(**_safe_draft_kwargs())
        assert doc["everbridge_mode"] == "safe"
        assert doc["slack_mode"] == "shadow"


# ---------------------------------------------------------------------------
# D4H Phase 2 PR 5 — new_incident_doc D4H status fields
# ---------------------------------------------------------------------------
# The /send-notification handler attempts D4H create-incident after EB+Slack
# work; the result lands in incidents/{event_id} via these fields. The
# /dispatch-status endpoint exposes them to the frontend for post-dispatch
# querying (e.g., "did D4H succeed?"). The d4h_event_log list captures
# Cloud-Tasks-worker events that fire after /send-notification returned
# (bulk-ABSENT summaries, per-YES sync events landing via PR 6).

class TestNewIncidentDocD4HFields:
    def test_d4h_status_defaults_to_pending(self):
        """Doc is written at end-of-dispatch with whatever final D4H status
        was achieved. 'pending' here is just the structural initial value;
        the /send-notification handler overwrites it before persistence."""
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["d4h_status"] == "pending"

    def test_d4h_activity_id_starts_none(self):
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["d4h_activity_id"] is None

    def test_d4h_error_starts_none(self):
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["d4h_error"] is None

    def test_d4h_event_log_starts_empty_list(self):
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["d4h_event_log"] == []
        assert isinstance(doc["d4h_event_log"], list)

    def test_d4h_event_log_is_a_copy(self):
        """Caller mutation of the doc's event_log must not bleed across
        new_incident_doc() invocations — each fresh doc gets its own list."""
        a = new_incident_doc(**_live_path_kwargs())
        b = new_incident_doc(**_live_path_kwargs())
        a["d4h_event_log"].append("test entry")
        assert b["d4h_event_log"] == []

    def test_d4h_fields_present_on_safe_draft_path(self):
        """Safe-draft + shadow path also gets D4H fields — D4H integration
        is independent of EB live-vs-draft and Slack full-vs-shadow modes."""
        doc = new_incident_doc(**_safe_draft_kwargs())
        assert doc["d4h_status"] == "pending"
        assert doc["d4h_activity_id"] is None
        assert doc["d4h_error"] is None
        assert doc["d4h_event_log"] == []


# ---------------------------------------------------------------------------
# Slack message ts fields (Cluster C — incremental persistence)
# ---------------------------------------------------------------------------

class TestNewIncidentDocSlackMessageTs:
    """Pin the welcome_ts and caltopo_ts fields added in Cluster C.

    These fields capture Slack message timestamps from the per-incident
    channel posts so /send-notification can write them via .update()
    incrementally (resilient to a later failure that prevents the final
    .set()) AND so a future retry-dedup pass / admin audit can identify
    already-pinned messages by ts.

    The /send-notification handler also writes these via incremental
    .update() the moment each post returns. The final .set() at the end
    of /send-notification idempotently writes the same values.
    """

    def test_welcome_ts_captured_when_welcome_post_succeeded(self):
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["welcome_ts"] == "9876.5432"

    def test_caltopo_ts_captured_when_caltopo_post_succeeded(self):
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["caltopo_ts"] == "9876.6543"

    def test_welcome_ts_empty_string_when_no_post(self):
        """Empty string distinguishes 'no welcome posted' (welcome guard
        fired or this is a safe-draft path that doesn't post a welcome)
        from 'welcome post returned a real ts'. Matches the empty-string
        convention of active_incidents_ts."""
        doc = new_incident_doc(**_live_path_kwargs(welcome_ts=""))
        assert doc["welcome_ts"] == ""

    def test_caltopo_ts_empty_string_when_no_caltopo_url(self):
        """Empty string is the correct default when caltopo_url was
        absent from the request body — handler skips the CalTopo post
        entirely. NOT None — keeps the field type stable as str."""
        doc = new_incident_doc(**_live_path_kwargs(caltopo_ts=""))
        assert doc["caltopo_ts"] == ""

    def test_staging_ts_captured_when_staging_post_succeeded(self):
        """#673 — the staging message is its own post with its own ts.

        This ts is the handle an admin needs to find and delete the staging
        message when a dispatch went out with the wrong location, which is the
        entire reason staging was split out of the welcome.
        """
        doc = new_incident_doc(**_live_path_kwargs())
        assert doc["staging_ts"] == "9876.6000"

    def test_staging_ts_empty_string_when_no_post(self):
        """Same empty-string convention as welcome_ts / caltopo_ts — NOT None."""
        doc = new_incident_doc(**_live_path_kwargs(staging_ts=""))
        assert doc["staging_ts"] == ""

    def test_staging_ts_is_a_required_parameter(self):
        """#570 symmetry, enforced rather than described.

        staging_ts is incrementally patched to Firestore at Step 8. If it were
        an optional param with a default, a caller that forgot to pass it would
        make the final .set() overwrite the patched value with "" — silently,
        exactly the way slack_dm_sent_user_ids was wiped between send-time and
        poll-time in #568 before #570 fixed it. A required param turns that
        mistake into a TypeError at the call site.
        """
        kwargs = _live_path_kwargs()
        kwargs.pop("staging_ts")
        with pytest.raises(TypeError):
            new_incident_doc(**kwargs)

    def test_welcome_ts_and_caltopo_ts_are_strings(self):
        """Slack message ts values are decimal-string microseconds
        (e.g. '1234567890.123456'). Stored as str to preserve precision
        and match what slack_sdk returns from post_message."""
        doc = new_incident_doc(**_live_path_kwargs())
        assert isinstance(doc["welcome_ts"], str)
        assert isinstance(doc["caltopo_ts"], str)


# ---------------------------------------------------------------------------
# stop_incident() — set status + expire_at TTL field
# ---------------------------------------------------------------------------

class TestStopIncident:
    def test_status_prefixed_with_stopped_reason(self):
        doc = new_incident_doc(**_live_path_kwargs())
        stop_incident(doc, "everbridge_closed")
        assert doc["status"] == "stopped_everbridge_closed"
        assert doc["stop_reason"] == "everbridge_closed"

    def test_status_for_each_known_reason(self):
        # Pin the reason → status mapping for the four expected stop reasons
        # the /poll-incident handler emits. A future PR that introduces a
        # new reason can add a row here without breaking existing ones.
        for reason in ("everbridge_closed", "idle", "hard_cap", "error"):
            doc = new_incident_doc(**_live_path_kwargs())
            stop_incident(doc, reason)
            assert doc["status"] == f"stopped_{reason}"
            assert doc["stop_reason"] == reason

    def test_expire_at_is_now_plus_24h(self):
        # The TTL window. Fuzz with a 5-second tolerance to cover the time
        # between now_utc() reads inside the function and outside.
        from datetime import timedelta
        before = now_utc()
        doc = new_incident_doc(**_live_path_kwargs())
        stop_incident(doc, "everbridge_closed")
        after = now_utc()
        delta = timedelta(seconds=TTL_AFTER_CLOSE_S)
        fuzz  = timedelta(seconds=5)
        assert before + delta - fuzz <= doc["expire_at"] <= after + delta + fuzz

    def test_expire_at_is_a_datetime(self):
        # Must be datetime so the Firestore SDK serializes it as TIMESTAMP.
        # The Firestore TTL service silently ignores numeric fields — only
        # TIMESTAMP fields trigger auto-deletion.
        doc = new_incident_doc(**_live_path_kwargs())
        stop_incident(doc, "idle")
        assert isinstance(doc["expire_at"], datetime)

    def test_mutates_in_place_and_returns_same_doc(self):
        # Caller convenience: the /poll-incident handler in main.py wraps
        # this in a Firestore transaction; both pointer + mutation matter.
        doc = new_incident_doc(**_live_path_kwargs())
        result = stop_incident(doc, "hard_cap")
        assert result is doc


# ---------------------------------------------------------------------------
# new_skeleton_incident_doc() — double-dispatch guard skeleton
# ---------------------------------------------------------------------------

def _skeleton_kwargs(**overrides):
    base = dict(
        event_id="2026-04-25_mpd_calaveras_1430",
        event_name="2026-04-25 MPD CALAVERAS 1430",
        event_name_human="2026-04-25 MPD CALAVERAS",
        dispatcher_email="bill@example.com",
        created_at=datetime(2026, 4, 25, 14, 30, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return base


class TestNewSkeletonIncidentDoc:
    """Mirror tests for new_skeleton_incident_doc().

    The skeleton is the SUCCESS-path side of the atomic double-dispatch
    guard. Two contracts to pin:

    1. Required-field contract — must include the fields downstream
       endpoints look up via `.get()` on a stuck skeleton, so a handler
       crash mid-flight doesn't break ownership checks or status reads.

    2. No-extra-noise contract — must NOT pre-populate placeholder
       values for fields that new_incident_doc() will fill in on the
       success path (notification_id, slack_channel_id, etc.). The
       skeleton is a tombstone, not a partial-state model — pre-set
       placeholders would diverge from the .set() overwrite if either
       side ever changes.
    """

    def test_required_fields_for_close_polling_ownership_check(self):
        doc = new_skeleton_incident_doc(**_skeleton_kwargs())
        assert doc["event_id"] == "2026-04-25_mpd_calaveras_1430"
        assert doc["dispatcher_email"] == "bill@example.com"

    def test_status_creating_identifies_tombstone(self):
        # /dispatch-status returns the 7-field response via .get() defaults;
        # status='creating' is the visible signal that this doc is a
        # skeleton (handler crashed between .create() and .set()) rather
        # than a completed dispatch.
        doc = new_skeleton_incident_doc(**_skeleton_kwargs())
        assert doc["status"] == "creating"

    def test_created_at_is_a_datetime(self):
        doc = new_skeleton_incident_doc(**_skeleton_kwargs())
        assert isinstance(doc["created_at"], datetime)
        assert doc["created_at"].tzinfo is timezone.utc

    def test_event_name_human_preserved_verbatim(self):
        # Slack channel naming + cross-app display use event_name_human.
        # The skeleton holds it so a future cleanup pass over stuck
        # skeletons has the human-readable identifier without parsing
        # event_id.
        doc = new_skeleton_incident_doc(**_skeleton_kwargs())
        assert doc["event_name_human"] == "2026-04-25 MPD CALAVERAS"

    def test_no_placeholder_for_notification_id(self):
        # No pre-populated None / "" for fields the success-path .set()
        # writes. Storing placeholders would create a second source of
        # truth and risk drift from new_incident_doc().
        doc = new_skeleton_incident_doc(**_skeleton_kwargs())
        assert "notification_id" not in doc
        assert "template_id" not in doc
        assert "slack_channel_id" not in doc

    def test_no_placeholder_for_d4h_fields(self):
        doc = new_skeleton_incident_doc(**_skeleton_kwargs())
        assert "d4h_status" not in doc
        assert "d4h_activity_id" not in doc

    def test_no_placeholder_for_responder_state(self):
        doc = new_skeleton_incident_doc(**_skeleton_kwargs())
        assert "responders" not in doc
        assert "last_non_empty_responders" not in doc

    def test_skeleton_field_count_is_exactly_six(self):
        # Hardcoded count pins the minimal-shape contract. If a future
        # PR adds a 7th field, that's a design change that should also
        # update _validate_close_polling / _build_dispatch_status_response
        # expectations — the test failure is the conversation trigger.
        doc = new_skeleton_incident_doc(**_skeleton_kwargs())
        assert len(doc) == 6
