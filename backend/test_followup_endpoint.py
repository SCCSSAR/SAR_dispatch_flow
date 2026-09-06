"""test_followup_endpoint.py — #612 POST /send-followup-notification.

Two kinds of test live here, and the distinction matters:

1. **Real tests** against `backend/incidents.py`, which is pure stdlib and
   imports directly.

2. **Source-reading pins** against `backend/main.py`. main.py cannot be imported
   in the local pytest environment (fastapi, httpx, google.cloud are
   container-only), so the established project pattern — see
   test_main_regression.py — is to read the file and assert on its text.

Source-reading pins are weaker than executable tests: they prove a line EXISTS,
not that it RUNS. They are written here to pin ORDERING and PRESENCE of the
failure-mode protections (Cluster B before the send, Cluster C after it), which
is exactly the class of regression a refactor introduces and a reviewer misses.
Every pin below was verified by mutation — reintroducing the bug it describes
makes it fail. A pin that cannot be broken is not a test.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from datetime import timedelta

from backend.incidents import (
    FOLLOWUP_STALL_SECONDS,
    followup_tombstone_is_dead,
    new_incident_doc,
    new_skeleton_incident_doc,
)


def _main_src() -> str:
    return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")


def _handler_src() -> str:
    """Just the /send-followup-notification handler body, comments included."""
    src = _main_src()
    start = src.index('@app.post("/send-followup-notification/{event_id}")')
    end = src.index('@app.post("/confirm-draft-sent/{event_id}")', start)
    return src[start:end]


def _handler_code() -> str:
    """The handler with comment LINES stripped.

    Absence assertions ("this symbol must not appear") have to read code, not
    prose — the first draft of this file failed because a comment *explaining*
    that `_parse_poll_response` is deliberately untouched matched a pin asserting
    it is not called. Presence assertions can use either; absence ones must use
    this.
    """
    out = []
    for line in _handler_src().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  # ")[0])
    return "\n".join(out)


def _base_incident_kwargs(**over):
    kwargs = dict(
        event_id="evt", event_name="n", event_name_human="h",
        dispatcher_email="d@sccssar.org", everbridge_event_id="eb1",
        notification_id="n1", template_id=None,
        slack_channel_id="C1", slack_channel_name="inc",
        active_incidents_ts="1.1", welcome_ts="2.2", staging_ts="2.5", caltopo_ts="3.3",
        everbridge_mode="full", slack_mode="full", action="send_live",
        selected_target_ids=[], requested_group_names=[],
        contact_group_map={}, contact_email_map={},
    )
    kwargs.update(over)
    return kwargs


class TestFollowupNotificationIdsPersistence:
    """Gap A — the field must exist on the doc builder (#570 symmetry)."""

    def test_new_incident_doc_carries_the_field(self):
        doc = new_incident_doc(**_base_incident_kwargs())
        assert doc["followup_notification_ids"] == []

    def test_the_field_is_a_parameter_not_just_a_default(self):
        """#570 symmetry requirement.

        A field patched incrementally but NOT accepted as a parameter here is
        silently wiped by the final .set() overwrite — the exact bug that sent a
        duplicate DM to Bill in PR #568.
        """
        doc = new_incident_doc(
            **_base_incident_kwargs(followup_notification_ids=["f1", "f2"])
        )
        assert doc["followup_notification_ids"] == ["f1", "f2"]

    def test_the_list_is_copied_not_aliased(self):
        src = ["f1"]
        doc = new_incident_doc(**_base_incident_kwargs(followup_notification_ids=src))
        src.append("f2")
        assert doc["followup_notification_ids"] == ["f1"]

    def test_none_becomes_an_empty_list(self):
        doc = new_incident_doc(
            **_base_incident_kwargs(followup_notification_ids=None)
        )
        assert doc["followup_notification_ids"] == []

    def test_skeleton_does_not_carry_the_field(self):
        """The skeleton is the ORIGINAL dispatch's tombstone.

        A follow-up cannot exist before the dispatch it follows, so adding the
        field here would imply a lifecycle that does not occur.
        """
        skel = new_skeleton_incident_doc(
            event_id="e", event_name="n", event_name_human="h",
            dispatcher_email="d@sccssar.org",
            created_at=datetime.now(timezone.utc),
        )
        assert "followup_notification_ids" not in skel


class TestFollowupTombstoneLiveness:
    """Real executable tests for the retry-unlock decision.

    This logic was originally written inline in main.py, where it could only be
    covered by source-reading pins — and its mutation test came back VACUOUS
    (inverting the whole check broke nothing). Moving the branching into
    incidents.py is what makes these assertions possible.
    """

    NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)

    def test_live_double_click_is_not_dead(self):
        """THE Cluster B case — seconds old, still sending. Must stay 409."""
        doc = {"status": "sending", "created_at": self.NOW - timedelta(seconds=1)}
        assert followup_tombstone_is_dead(doc, now=self.NOW) is False

    def test_failed_attempt_is_dead(self):
        """EB rejected it. The dispatcher will press send again, same wording."""
        doc = {"status": "failed", "created_at": self.NOW}
        assert followup_tombstone_is_dead(doc, now=self.NOW) is True

    def test_stalled_sending_is_dead(self):
        """Preempted between .create() and the send — nothing fired, nothing will."""
        doc = {"status": "sending",
               "created_at": self.NOW - timedelta(seconds=FOLLOWUP_STALL_SECONDS + 1)}
        assert followup_tombstone_is_dead(doc, now=self.NOW) is True

    def test_exactly_at_the_stall_boundary_is_still_live(self):
        doc = {"status": "sending",
               "created_at": self.NOW - timedelta(seconds=FOLLOWUP_STALL_SECONDS)}
        assert followup_tombstone_is_dead(doc, now=self.NOW) is False

    def test_completed_send_is_never_dead(self):
        """"sent" means an identical correction ALREADY went out.

        The 409 here is not an obstacle — it IS the duplicate-send protection.
        Treating it as dead would re-page the team with the same message.
        """
        doc = {"status": "sent",
               "created_at": self.NOW - timedelta(days=30)}
        assert followup_tombstone_is_dead(doc, now=self.NOW) is False

    def test_missing_doc_is_not_dead(self):
        assert followup_tombstone_is_dead(None, now=self.NOW) is False

    def test_unknown_status_is_not_dead(self):
        doc = {"status": "weird", "created_at": self.NOW - timedelta(days=1)}
        assert followup_tombstone_is_dead(doc, now=self.NOW) is False

    def test_missing_timestamp_is_not_dead(self):
        assert followup_tombstone_is_dead({"status": "sending"}, now=self.NOW) is False

    def test_non_datetime_timestamp_is_not_dead(self):
        doc = {"status": "sending", "created_at": "2026-07-28T12:00:00Z"}
        assert followup_tombstone_is_dead(doc, now=self.NOW) is False

    def test_naive_timestamp_does_not_raise(self):
        """Firestore normally returns tz-aware, but a naive value must not 500."""
        doc = {"status": "sending", "created_at": datetime(2026, 7, 28, 11, 0, 0)}
        assert followup_tombstone_is_dead(doc, now=self.NOW) is False

    def test_retried_at_takes_precedence_over_created_at(self):
        """A retry restarts the clock; the original created_at must not re-trip it."""
        doc = {
            "status":     "sending",
            "created_at": self.NOW - timedelta(hours=5),
            "retried_at": self.NOW - timedelta(seconds=2),
        }
        assert followup_tombstone_is_dead(doc, now=self.NOW) is False


class TestFollowupHandlerFailureModeDiscipline:
    """Source-reading pins for the six-question rubric (CLAUDE.md)."""

    def test_handler_exists_at_the_expected_path(self):
        assert '@app.post("/send-followup-notification/{event_id}")' in _main_src()

    def test_idempotency_guard_precedes_the_everbridge_send(self):
        """Q4 — concurrent execution. Cluster B ordering.

        The .create() tombstone MUST come before the point of no return. If a
        refactor moves the send earlier, a double-click pages the whole team
        twice with the same correction — and the rate limiter does not help
        (it catches the 6th call, not the 2nd).
        """
        h = _handler_src()
        assert h.index('collection("followup_sends")') < h.index(
            "send_followup_notification_live"
        ), "Cluster B tombstone must be created BEFORE the EB send"

    def test_already_exists_returns_409(self):
        h = _handler_src()
        assert "_FirestoreAlreadyExists" in h
        assert "status_code=409" in h

    def test_notification_id_is_persisted_immediately_after_the_send(self):
        """Q1 — point of no return. Cluster C.

        The id must be written the moment it is obtained, not at some later
        consolidated write, or a failure in between orphans a live EB
        notification with no Firestore record.
        """
        h = _handler_src()
        assert h.index("send_followup_notification_live") < h.index(
            "followup_notification_ids"
        ), "the follow-up id must be persisted AFTER the send returns"
        assert "ArrayUnion" in h, "must accumulate, not overwrite prior follow-ups"

    def test_persistence_after_the_send_is_best_effort(self):
        """Q2 — what happens between line N and N+1.

        The notification has already fired. A Firestore failure must not surface
        to the dispatcher as an error for a correction that WAS delivered.
        """
        assert "_patch_incident_doc_best_effort" in _handler_src()

    def test_everbridge_read_failure_is_typed_502(self):
        """Q5 — typed failure modes. A transient EB read failure is retriable."""
        h = _handler_src()
        assert "status_code=502" in h

    def test_send_failure_marks_the_tombstone_failed(self):
        """Q2 — a crash after the tombstone must not leave it reading 'sending'."""
        h = _handler_src()
        assert '"status": "failed"' in h

    def test_safe_mode_refuses_rather_than_drafting(self):
        """The #599/#600 reasoning, pinned.

        A hand-send from the Everbridge UI IGNORES target_contact_ids, and the
        derived recipient set is the whole feature. Drafting would silently page
        the wrong people — including the decliners this endpoint excludes.
        """
        h = _handler_src()
        assert "_route_send" in h
        assert 'decision.action != "send_live"' in h
        assert "cannot be drafted" in h

    def test_recipients_come_from_the_partition_helper_not_the_poll_parser(self):
        """§3.2 — _parse_poll_response stays byte-identical.

        It feeds the polling chain and the D4H path, and carries the #442
        guardrail against emitting decline identities.
        """
        assert "partition_contacts_for_followup" in _handler_src()
        assert "_parse_poll_response(" not in _handler_code(), (
            "the polling chain's parser must not be called here — §3.2 keeps it "
            "byte-identical so the #442 decline-identity boundary holds"
        )

    def test_recipient_state_is_read_fresh_from_everbridge(self):
        """§3.2 — live state at the moment of correction, not a stale cache."""
        assert "fetch_notification_raw" in _handler_code()

    def test_handler_uses_the_raw_fetch_not_the_parsed_poll(self):
        """THE seam bug this handler shipped with in its first draft.

        poll_notification() issues the identical request but returns
        _parse_poll_response's projection, which DISCARDS allDetails[] — the array
        the partition needs. Calling it here produced an empty recipient set on
        every invocation, silently, so every follow-up refused with a misleading
        "everyone declined". Caught in code review.

        Note the earlier version of test_recipient_state_is_read_fresh_from_everbridge
        asserted only that the string "poll_notification" appeared — and PASSED
        against the broken code, because "fetch_notification_raw" does not contain
        it but "poll_notification" did. The executable proof lives in
        test_everbridge_followup.py::TestPollAndPartitionAreNotInterchangeable.
        """
        code = _handler_code()
        assert "poll_notification(" not in code, (
            "must call fetch_notification_raw — poll_notification's parsed shape "
            "yields an empty recipient set"
        )

    def test_empty_recipient_set_is_refused(self):
        h = _handler_src()
        assert "No one to send to" in h

    def test_body_is_validated_through_the_shared_sms_policy(self):
        """The 160-char limit is enforced server-side, not just in the UI."""
        h = _handler_src()
        assert "prepare_followup_sms_body" in h
        assert 'prepared["accepted"]' in h

    def test_the_normalized_text_is_what_gets_sent(self):
        """Sending `raw` would re-introduce the UCS-2 problem normalization fixed."""
        h = _handler_src()
        assert 'notif_body = prepared["text"]' in h

    def test_sosar_prefix_is_enforced_server_side(self):
        """County policy — never trust the client to comply."""
        assert "_enforce_sosar_title_prefix" in _handler_src()

    def test_no_body_or_title_template_is_injected(self):
        """§3.3 blank slate — the server composes nothing.

        Guards against a future 'helpful' default creeping back in.
        """
        code = _handler_code()
        for banned in ("CORRECTION -", "Correction:", "-305", "event_name_streetname"):
            assert banned not in code, f"handler injects composed content: {banned!r}"

    def test_missing_incident_returns_404(self):
        assert "status_code=404" in _handler_src()

    def test_incident_without_a_sent_notification_is_refused(self):
        """No reply history means no derivable recipient set — guessing is not ok."""
        h = _handler_src()
        assert "no reply history" in h.lower() or "reply history" in h


class TestFollowupHandlerPrivacy:
    """"No PII in logs" — Critical Privacy & Security #3."""

    def test_log_lines_carry_counts_not_identities(self):
        code = _handler_code()
        for banned in ("first_name", "last_name", "fullName", "mp_name"):
            assert banned not in code, f"handler references PII field {banned!r}"
        assert "recipients=%d" in code, "recipient COUNT is the loggable form"

    def test_message_text_is_never_logged(self):
        """The body is dispatcher-authored free text and may name the subject."""
        code = _handler_code()
        # Take each logger.* call through to its closing paren.
        joined = ""
        lines = code.splitlines()
        for i, ln in enumerate(lines):
            if "logger." in ln:
                joined += "\n".join(lines[i:i + 8])
        assert "notif_body" not in joined
        assert "raw_body" not in joined
        assert "notif_title" not in joined

    def test_response_body_carries_counts_not_contact_ids(self):
        code = _handler_code()
        ret = code[code.index("return {"):]
        assert "recipient_count" in ret
        assert "followup_contact_ids" not in ret
        assert "recipients\"" not in ret


class TestFollowupIdempotencyKey:
    """Keyed on content so a real second correction is not blocked."""

    def test_key_is_defined_over_event_title_and_body(self):
        src = _main_src()
        fn = src[src.index("def _followup_idempotency_key"):]
        fn = fn[:fn.index("\n@app.post")]
        assert "hashlib.sha256" in fn
        assert "event_id" in fn and "title" in fn and "body" in fn

    def test_key_helper_is_pure_and_deterministic(self):
        """Recomputed here rather than imported (main.py is not importable)."""
        import hashlib

        def key(event_id, title, body):
            digest = hashlib.sha256(
                f"{event_id}\x00{title}\x00{body}".encode("utf-8")
            ).hexdigest()[:32]
            return f"{event_id}__{digest}"

        a = key("e1", "SOSAR - x", "body")
        b = key("e1", "SOSAR - x", "body")
        c = key("e1", "SOSAR - x", "different body")
        assert a == b, "a double-click must collide"
        assert a != c, "a genuinely different correction must NOT collide"

    def test_key_is_namespaced_by_event(self):
        assert '"{event_id}__' in _main_src() or 'f"{event_id}__' in _main_src()


class TestFollowupCounterMirrorsBackend:
    """The frontend counter duplicates backend/everbridge.py — pin the mirror.

    The duplication is deliberate: a live per-keystroke counter cannot round-trip
    to the server. But a mirror that drifts is worse than no counter — the
    dispatcher is told "142 / 160" and then refused at 161, mid-incident.

    These read BOTH files and compare. Per the project's self-mirroring lesson
    (2026-07-27), a test that only reads the frontend would be vacuous.
    """

    @staticmethod
    def _frontend() -> str:
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _everbridge() -> str:
        return (Path(__file__).parent / "everbridge.py").read_text(encoding="utf-8")

    def test_hard_limit_literal_matches(self):
        import re
        be = re.search(r"^SMS_LIMIT_GSM7\s*=\s*(\d+)", self._everbridge(), re.M)
        fe = re.search(r"const _FOLLOWUP_SMS_HARD_LIMIT\s*=\s*(\d+)", self._frontend())
        assert be and fe, "hard-limit constant not found in one of the two files"
        assert be.group(1) == fe.group(1), (
            f"SMS hard limit drifted: backend={be.group(1)} frontend={fe.group(1)}"
        )

    def test_reserve_literal_matches(self):
        """The MEASURED Everbridge auto-text overhead (2026-07-28 live send).

        Kept separate from the 160 on purpose: 160 is an EB platform fact, 61 is
        a measured property of our org's confirmation wording and short-link
        shape. A fused "99" would hide which half drifted.
        """
        import re
        be = re.search(r"^FOLLOWUP_SMS_RESERVE\s*=\s*(\d+)", self._everbridge(), re.M)
        fe = re.search(r"const _FOLLOWUP_SMS_RESERVE\s*=\s*(\d+)", self._frontend())
        assert be and fe, "reserve constant not found in one of the two files"
        assert be.group(1) == fe.group(1), (
            f"SMS reserve drifted: backend={be.group(1)} frontend={fe.group(1)} — "
            f"the dispatcher would be shown a budget the server does not enforce"
        )

    def test_effective_budget_is_99(self):
        """160 - 61. Pinned as a number because it is what the dispatcher sees.

        The real 2026-07-24 correction is 96 characters and fits with 3 to
        spare, so this budget is tight enough that an accidental change to
        either constant is operationally significant.
        """
        import re
        be_src = self._everbridge()
        hard = int(re.search(r"^SMS_LIMIT_GSM7\s*=\s*(\d+)", be_src, re.M).group(1))
        res = int(re.search(r"^FOLLOWUP_SMS_RESERVE\s*=\s*(\d+)", be_src, re.M).group(1))
        assert hard - res == 99

    def test_prepare_defaults_to_the_measured_reserve(self):
        """A caller that omits `reserve` must get the real budget, not 160.

        The handler calls prepare_followup_sms_body(raw) with no reserve, so the
        default IS the enforcement policy.
        """
        src = self._everbridge()
        assert "def prepare_followup_sms_body(\n    raw: str, *, reserve: int = FOLLOWUP_SMS_RESERVE\n) -> dict:" in src

    def test_counter_at_rest_literal_matches_the_computed_limit(self):
        """The pre-JS markup value must not contradict the computed budget."""
        import re
        fe = self._frontend()
        hard = int(re.search(r"const _FOLLOWUP_SMS_HARD_LIMIT\s*=\s*(\d+)", fe).group(1))
        res = int(re.search(r"const _FOLLOWUP_SMS_RESERVE\s*=\s*(\d+)", fe).group(1))
        m = re.search(r'id="followup-counter"[^>]*>0 / (\d+)<', fe)
        assert m, "counter at-rest literal not found"
        assert int(m.group(1)) == hard - res, (
            f"counter markup says {m.group(1)}, computed limit is {hard - res}"
        )

    def test_explainer_sets_expectations_about_the_poll(self):
        """The three facts that change what the dispatcher DOES.

        Bill, 2026-07-28 (superseding his own earlier wording the same day):
        the first draft explained the poll mechanism — that Everbridge sends
        this as a poll and asks responders to reply YES to confirm receipt —
        and read as "a wall of words, not a helpful tip". The mechanism was
        cut; the CONSEQUENCES were kept, because those are what a dispatcher
        acts on mid-incident:

          - where replies land (NOT here, so they don't sit waiting on a
            screen that will never show them),
          - the 99-char budget and what exceeding it costs them,
          - that a Slack copy goes out too, so they don't post one by hand.

        Deliberately NOT pinned any more: the literal words "poll" and "YES to
        confirm". Re-adding an assertion on either would re-import the wording
        that was explicitly rejected.
        """
        fe = self._frontend()
        panel = fe[fe.index('<details id="followup-panel"'):]
        panel = panel[:panel.index("</details>")]
        flat = " ".join(panel.split())
        assert "Everbridge console, not here" in flat, (
            "the dispatcher must be told replies do NOT surface in Dispatch Turbo"
        )
        assert "Max 99 characters" in flat, (
            "the budget must be stated — it is ENFORCED (btn.disabled on "
            "overLimit + accepted=False server-side), not advisory"
        )
        assert "confirm-receipt" in flat, (
            "the REASON for the reduced budget must survive — without it the "
            "99-char limit reads as an arbitrary product decision"
        )
        # The explainer must NOT claim that exceeding the limit degrades the
        # message to a link. Exceeding is impossible — the send is blocked.
        # The link outcome comes from NON-GSM-7 characters over 70 chars, is a
        # warning rather than a block, and already has its own live warning
        # naming the offending character. Stating it here as a consequence of
        # length is simply false, and it survived two drafts before Bill caught
        # it by asking whether the limit was enforced at all (2026-07-28).
        assert "Exceed" not in flat, (
            "the explainer implies a dispatcher can exceed the limit — they "
            "cannot; the Send button is disabled and the server refuses"
        )
        assert "Also goes to Slack" in flat, (
            "the dispatcher must know the Slack copy is automatic, or they will "
            "post a duplicate by hand"
        )

    def test_punctuation_normalization_tables_cover_the_same_characters(self):
        """Every character the backend flattens must also be flattened client-side.

        If the frontend misses one, its counter reports the UCS-2 length while the
        server measures the flattened GSM-7 cost — two different numbers for the
        same text.
        """
        import re
        be_block = re.search(
            r"_SMS_PUNCTUATION_NORMALIZATION: dict\[str, str\] = \{(.*?)\n\}",
            self._everbridge(), re.S,
        )
        fe_block = re.search(
            r"const _FOLLOWUP_PUNCT_NORMALIZATION = \{(.*?)\n  \};",
            self._frontend(), re.S,
        )
        assert be_block and fe_block, "normalization table not found in one file"
        def _decode(k: str) -> str:
            """Frontend keys use \\uXXXX escapes so invisible characters are
            reviewable in the source; the backend uses literals. Compare the
            DECODED characters so either representation is acceptable."""
            return k.encode("utf-8").decode("unicode_escape") if "\\u" in k else k

        def _pairs(block: str) -> dict:
            """key -> replacement. Handles both "x" and 'x' value quoting, which
            both files need because the values include a quote character."""
            out = {}
            for k, v_dq, v_sq in re.findall(
                r'"([^"]+)"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')',
                block,
            ):
                out[_decode(k)] = _decode(v_dq if v_dq or v_sq == "" else v_sq)
            return out

        be_map = _pairs(be_block.group(1))
        fe_map = _pairs(fe_block.group(1))

        assert set(be_map) == set(fe_map), (
            f"normalization tables drifted — backend-only: "
            f"{sorted(set(be_map) - set(fe_map))}; "
            f"frontend-only: {sorted(set(fe_map) - set(be_map))}"
        )
        # Values matter as much as keys. A frontend entry mapping "…" to itself
        # while the backend maps it to "..." makes the counter report 1 septet
        # where the server charges 3 — silent, and only visible at the boundary.
        differing = {k for k in be_map if be_map[k] != fe_map[k]}
        assert not differing, (
            "normalization REPLACEMENTS drifted for "
            + ", ".join(
                f"{k!r}: backend={be_map[k]!r} frontend={fe_map[k]!r}"
                for k in sorted(differing)
            )
        )

    def test_gsm7_extension_set_matches(self):
        import re
        be = re.search(r'_GSM7_EXTENDED = frozenset\("([^"]*)"\)', self._everbridge())
        fe = re.search(r'const _GSM7_EXT_CHARS = "([^"]*)"', self._frontend())
        assert be and fe, "extension set not found in one of the two files"
        assert be.group(1) == fe.group(1), (
            "GSM-7 extension set drifted — these characters cost 2 septets each, "
            "so a mismatch mis-counts the budget"
        )

    def test_frontend_normalizes_before_measuring(self):
        """Same order as prepare_followup_sms_body().

        Measuring raw under-counts characters that EXPAND: one "…" becomes three
        septets, so a raw count accepts a body the server refuses.
        """
        fe = self._frontend()
        fn = fe[fe.index("function _followupBudget"):]
        fn = fn[:fn.index("\n  function ")]
        assert "_followupNormalize(raw)" in fn
        assert fn.index("_followupNormalize(raw)") < fn.index("for (const ch of text)")

    def test_frontend_does_not_enforce_the_70_char_ceiling(self):
        """Bill, 2026-07-28: flat 160. The 70 figure is a WARNING threshold only."""
        fe = self._frontend()
        fn = fe[fe.index("function _followupBudget"):]
        fn = fn[:fn.index("\n  function ")]
        assert "overLimit: used > _FOLLOWUP_SMS_LIMIT" in fn
        assert "truncationRisk" in fn, "the 70-char case must still WARN"

    def test_panel_is_reset_for_a_new_incident(self):
        """The stale-event_id bug: a follow-up aimed at the previous callout."""
        fe = self._frontend()
        clear = fe[fe.index("function clearResults()"):]
        clear = clear[:clear.index("\n  }")]
        assert "_followupResetForNewIncident" in clear

    def test_reset_clears_the_event_id(self):
        fe = self._frontend()
        fn = fe[fe.index("function _followupResetForNewIncident"):]
        fn = fn[:fn.index("\n  function ")]
        assert "_followupEventId = null" in fn

    def test_panel_is_revealed_only_for_a_live_send(self):
        """On the draft path no notification exists, so the endpoint would 409.

        Offering a button that cannot work reads as the app being broken (#652).
        """
        fe = self._frontend()
        assert "data.action === 'send_live'" in fe
        idx = fe.index("_followupReveal(data.event_id)")
        assert "send_live" in fe[idx - 400:idx]

    def test_send_button_starts_disabled(self):
        assert 'id="followup-send-btn" type="button" disabled' in self._frontend()

    def test_no_body_or_title_template_in_the_markup(self):
        """§3.3 blank slate — both fields open empty.

        A `value=` on the title input or text between the textarea tags would
        re-introduce the pre-canned content that decision removed.
        """
        fe = self._frontend()
        assert '<textarea id="followup-body-input" rows="3" maxlength="400"' in fe
        assert 'aria-describedby="followup-counter"></textarea>' in fe, (
            "textarea must be EMPTY — any content between the tags is a template"
        )
        title = fe[fe.index('id="followup-title-input"'):]
        title = title[:title.index(">")]
        assert "value=" not in title, "title input must not be pre-filled"


class TestFollowupSlackCourtesyPost:
    """The follow-up is also copied into the incident channel (Bill, 2026-07-28)."""

    def test_courtesy_post_happens(self):
        code = _handler_code()
        assert "slack_module.post_message" in code
        assert "Dispatch Follow-Up Message" in code

    def test_courtesy_post_is_TOP_LEVEL_never_threaded(self):
        """⚠️ THE one that matters.

        #660/#661 threaded the routine YES-arrival posts precisely BECAUSE a
        bot-authored threaded reply notifies NOBODY (verified on-device). A
        correction is the exact inverse — it must reach people.

        Tidying this under the pinned welcome reads as an obvious channel-noise
        improvement and would silently make the correction invisible. There is no
        error, no failed test, and no symptom until a real callout.
        """
        code = _handler_code()
        post = code[code.index("slack_module.post_message"):]
        post = post[:post.index("slack_courtesy_posted = True")]
        assert "thread_ts" not in post, (
            "the courtesy copy must be TOP-LEVEL — a threaded reply notifies "
            "nobody, which defeats the entire purpose of a correction"
        )

    def test_courtesy_post_is_not_gated_on_full_slack_mode(self):
        """personal-dev runs shadow and is the ONLY sanctioned EB/Slack test env.

        A `_SLACK_MODE == "full"` gate would make this unreachable exactly where
        it gets tested — which is how #661's predecessor failed its live test on
        2026-07-28.
        """
        code = _handler_code()
        post_region = code[code.index("Step 8"):code.index("PII discipline")] \
            if "Step 8" in code else code
        assert '_SLACK_MODE == "full"' not in post_region

    def test_courtesy_post_is_best_effort_and_last(self):
        """The Everbridge notification has already fired.

        A Slack failure must not turn a delivered correction into an error, and
        must not prevent the notification id being persisted.
        """
        code = _handler_code()
        assert code.index("followup_notification_ids") < code.index("slack_module.post_message"), (
            "the notification id must be persisted BEFORE the courtesy post"
        )
        post = code[code.index("slack_module.post_message"):]
        assert "except Exception" in post[:600], "courtesy post must be wrapped"

    def test_slack_failure_is_logged_as_non_fatal(self):
        h = _handler_src()
        assert "Everbridge\n        # notification WAS sent" in h or \
               "notification WAS sent" in h

    def test_outcome_is_surfaced_to_the_dispatcher(self):
        code = _handler_code()
        ret = code[code.index("return {"):]
        assert "slack_courtesy_posted" in ret

    def test_posts_the_normalized_body_not_the_raw(self):
        """Same text responders received over SMS — flattened punctuation and all."""
        code = _handler_code()
        post = code[code.index("Dispatch Follow-Up Message"):]
        assert "notif_body" in post[:200]
        assert "raw_body" not in post[:200]

    def test_no_channel_id_is_handled_without_crashing(self):
        code = _handler_code()
        assert "if slack_channel_id:" in code

    def test_explainer_mentions_the_slack_copy(self):
        fe = (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        panel = fe[fe.index('<details id="followup-panel"'):]
        panel = panel[:panel.index("</details>")]
        # Bill, 2026-07-28: the original two sentences ("...as a courtesy copy,
        # so responders already in Slack see it without digging through their
        # texts. No reply is expected there.") were cut in the explainer trim —
        # they explained a behaviour that asks nothing of the dispatcher. What
        # MUST survive is that the Slack copy is automatic, so nobody posts a
        # duplicate into the incident channel by hand during a correction.
        flat = " ".join(panel.split())
        assert "Also goes to Slack" in flat, (
            "the UI must still disclose that the follow-up is copied to Slack "
            "automatically — otherwise the #663 courtesy post is invisible to "
            "the person sending it"
        )
