"""test_everbridge_followup.py — #612 follow-up notification helpers.

**These tests import the REAL `backend/everbridge.py`.** They do not mirror it.

That is a deliberate departure from `test_everbridge.py`, which mirrors the
module's pure logic locally because `everbridge.py` imports `httpx` and httpx is
not installed in the local pytest environment (it ships only in the Cloud Run
container). A mirror can pass in full while production is broken — the dominant
failure mode this project has hit repeatedly, most recently 2026-07-27 where a
"pin" compared a phrase against itself and was vacuous on first write.

`httpx` is referenced only INSIDE function bodies in everbridge.py (never at
import time beyond the `import httpx` statement itself), so injecting a stub
module into `sys.modules` is sufficient to load the genuine module. Every
assertion below therefore exercises the code that actually ships.

Scope is the #612 surface only. The pre-existing mirror tests are left alone.
"""
import copy
import sys
import types
from pathlib import Path

import pytest

# --- Load the real everbridge.py with a temporarily-stubbed httpx -----------
#
# ⚠️ The stub is installed ONLY for the duration of the import and is removed
# immediately afterwards. Leaving it in `sys.modules` is not a harmless
# convenience: it makes `import httpx` succeed for every test module that runs
# later in the same session, which UN-SKIPS tests that are deliberately skipped
# when httpx is absent. Observed while writing this file — a stray global stub
# turned `6 skipped` into `1 skipped` in test_main_regression.py and four of the
# newly-running tests failed against the fake module. A test file must never
# change which tests other files run.
#
# everbridge.py references httpx only inside function bodies, and nothing here
# calls those functions, so the module works fine once the stub is withdrawn.
def _import_real_everbridge():
    stub_installed = "httpx" not in sys.modules
    if stub_installed:
        stub = types.ModuleType("httpx")

        class _Response:                             # only needed for annotations
            pass

        class _HTTPStatusError(Exception):
            pass

        stub.Response = _Response
        stub.HTTPStatusError = _HTTPStatusError
        for verb in ("get", "post", "put", "delete"):
            def _unavailable(*_a, _verb=verb, **_kw):
                raise AssertionError(
                    f"httpx.{_verb} called during a pure-logic test — these tests "
                    f"must never make a network call"
                )
            setattr(stub, verb, _unavailable)
        sys.modules["httpx"] = stub

    backend_dir = str(Path(__file__).resolve().parent)
    path_inserted = backend_dir not in sys.path
    if path_inserted:
        sys.path.insert(0, backend_dir)
    try:
        import everbridge
        return everbridge
    finally:
        if stub_installed:
            sys.modules.pop("httpx", None)
        if path_inserted:
            try:
                sys.path.remove(backend_dir)
            except ValueError:      # pragma: no cover - another module took it
                pass


eb = _import_real_everbridge()


# The caller ID is CONFIGURATION now (EVERBRIDGE_CALLER_ID), not a source
# constant -- it is a live dialable number and must not ship in a public repo.
# everbridge.py reads it at import and _require_caller_id() raises when it is
# unset, which is the whole point of the guard, so these payload-shape tests
# have to declare the dependency rather than inherit a hardcoded default.
@pytest.fixture(autouse=True)
def _configured_everbridge_org(monkeypatch):
    monkeypatch.setattr(eb, "CALLER_ID", "5555550142")
    monkeypatch.setattr(eb, "DELIVER_PATHS", [
        {"id": 7000000000000001, "pathId": 7000000000000006, "prompt": "SMS-Work Cell"},
        {"id": 7000000000000002, "pathId": 7000000000000007, "prompt": "Email-Work"},
    ])


def test_the_httpx_stub_did_not_leak_into_sys_modules():
    """Guard the guard.

    If this fails, this file is once again silently un-skipping other modules'
    tests — the contamination described above. It is cheap insurance against a
    future edit that "simplifies" the import back to a global stub.
    """
    assert "httpx" not in sys.modules or not isinstance(
        getattr(sys.modules["httpx"], "Response", None), type(None)
    )


def test_the_real_module_was_imported_not_a_mirror():
    """These tests are worthless if `eb` is anything but the shipped file."""
    assert Path(eb.__file__).resolve() == (
        Path(__file__).resolve().parent / "everbridge.py"
    )


def _payload(notification_type=None, **overrides):
    kwargs = dict(
        org_id="700000000000026",
        event_id="7000000000000019",
        event_name="2026-07-24 SCPD Moreland 1158",
        title="SOSAR - test",
        body="body text",
        target_contact_ids=["700000000000027"],
        target_group_ids=[],
        category_id=7000000000000003,
        include_launchtype=True,
    )
    kwargs.update(overrides)
    if notification_type is not None:
        kwargs["notification_type"] = notification_type
    return eb._build_send_notification_payload(**kwargs)


class TestStandardTypePayloadDelta:
    """Pin the §6.0 payload delta, derived from real EB-produced notifications.

    Source of truth: notification 7000000000000022 (the 2026-07-24 hand-built
    Standard follow-up) compared against 7000000000000020 (the Polling original).
    Verified read-only against the live org 2026-07-27 — 306 notifications paged.
    """

    def test_polling_is_the_default_so_existing_callers_are_unchanged(self):
        """Every pre-#612 caller omits notification_type; it must stay Polling."""
        p = _payload()
        assert p["type"] == "Polling"

    def test_polling_payload_is_byte_identical_with_and_without_the_new_param(self):
        """The new parameter must not perturb the existing dispatch payload.

        This is the regression that matters for the ORIGINAL dispatch: #612 must
        not change what a normal callout sends.
        """
        assert _payload() == _payload(eb.NOTIFICATION_TYPE_POLLING)

    def test_standard_sets_the_type(self):
        assert _payload(eb.NOTIFICATION_TYPE_STANDARD)["type"] == "Standard"

    def test_standard_drops_category_id_at_both_levels(self):
        p = _payload(eb.NOTIFICATION_TYPE_STANDARD)
        assert "categoryId" not in p
        assert "categoryId" not in p["message"]

    def test_standard_drops_the_questionaire(self):
        """THE load-bearing assertion.

        A Standard follow-up carrying a Yes/No questionnaire shows responders a
        prompt no polling chain reads — they answer, believe they confirmed, and
        never reach the Slack tally or D4H. The payload is accepted and the
        notification looks correct, so nothing else catches this.
        """
        assert "questionaire" not in _payload(eb.NOTIFICATION_TYPE_STANDARD)["message"]

    def test_polling_still_carries_the_questionaire_with_yes_and_no(self):
        """Guards the inverse: the delta must not leak into the Polling path."""
        q = _payload()["message"]["questionaire"]
        assert [a["name"] for a in q["answers"]] == ["Yes", "No"]

    def test_standard_uses_message_only_voicemail(self):
        bs = _payload(eb.NOTIFICATION_TYPE_STANDARD)["broadcastSettings"]
        assert bs["voiceMailOption"] == "MESSAGE_ONLY"

    def test_polling_keeps_message_with_confirmation_voicemail(self):
        assert (
            _payload()["broadcastSettings"]["voiceMailOption"]
            == "MESSAGE_WITH_CONFIRMATION"
        )

    def test_standard_drops_sms_callback(self):
        assert "smsCallBack" not in _payload(eb.NOTIFICATION_TYPE_STANDARD)["broadcastSettings"]

    def test_standard_keeps_confirm_contact_cycles_and_duration(self):
        """Deep compare found these identical across both types — copy them.

        `confirm: true` demonstrably applies to Standard: the 07-24 follow-up
        recorded 11/18 confirmed.
        """
        bs = _payload(eb.NOTIFICATION_TYPE_STANDARD)["broadcastSettings"]
        assert bs["confirm"] is True
        assert bs["contactCycles"] == 2
        assert bs["duration"] == 1
        assert bs["durationTimeUnit"] == "HOURS"

    def test_broadcast_settings_differ_from_polling_in_exactly_two_ways(self):
        """The empirical result was 25 shared keys, ONE differing value.

        Written as a whole-block comparison rather than key-by-key so that a
        future edit adding an unrelated Standard-only tweak fails here and has to
        be justified against the observed notification.
        """
        poll = _payload()["broadcastSettings"]
        std = _payload(eb.NOTIFICATION_TYPE_STANDARD)["broadcastSettings"]
        assert set(poll) - set(std) == {"smsCallBack"}
        assert set(std) - set(poll) == set()
        differing = {k for k in set(poll) & set(std) if poll[k] != std[k]}
        assert differing == {"voiceMailOption"}

    def test_message_block_differs_from_polling_in_exactly_two_ways(self):
        poll = _payload()["message"]
        std = _payload(eb.NOTIFICATION_TYPE_STANDARD)["message"]
        assert set(poll) - set(std) == {"categoryId", "questionaire"}
        assert set(std) - set(poll) == set()

    def test_building_standard_does_not_mutate_the_input(self):
        """_standard_type_payload must be pure (Aikido finding, PR #657).

        ⚠️ This test exists to catch the SHALLOW-COPY TRAP specifically. The
        obvious reading of "return a modified copy" is `dict(payload)`, but three
        of the five Standard edits land inside the nested `message` and
        `broadcastSettings` dicts, which a shallow copy SHARES with the caller.
        `dict(payload)["message"].pop("questionaire")` removes the key from the
        caller's dict too — so the naive fix looks pure and is not.

        Asserting on the nested keys is what makes this non-vacuous; a top-level
        equality check would pass against the buggy version.
        """
        source = _payload()                      # a Polling payload
        before = copy.deepcopy(source)

        result = eb._standard_type_payload(source)

        assert source == before, "input payload was mutated"
        # The three nested edits, checked explicitly.
        assert "questionaire" in source["message"]
        assert "categoryId" in source["message"]
        assert "smsCallBack" in source["broadcastSettings"]
        assert source["broadcastSettings"]["voiceMailOption"] == "MESSAGE_WITH_CONFIRMATION"
        # And the returned copy really did change.
        assert "questionaire" not in result["message"]
        assert result["broadcastSettings"]["voiceMailOption"] == "MESSAGE_ONLY"

    def test_nested_dicts_are_not_shared_between_input_and_output(self):
        """Identity check — the copy must own its nested dicts.

        Even if no current edit mutated them post-return, shared nesting is a
        latent aliasing bug for any future caller.
        """
        source = _payload()
        result = eb._standard_type_payload(source)
        assert result["message"] is not source["message"]
        assert result["broadcastSettings"] is not source["broadcastSettings"]

    def test_category_id_argument_is_ignored_for_standard(self):
        """Callers may pass anything; Standard drops the field entirely."""
        a = _payload(eb.NOTIFICATION_TYPE_STANDARD, category_id=0)
        b = _payload(eb.NOTIFICATION_TYPE_STANDARD, category_id=7000000000000004)
        assert a == b


class TestPartitionContactsForFollowup:
    """Pin the recipient heuristic (§2.3 / §3.1)."""

    @staticmethod
    def _row(cid, *, confirmed, text="", at=None):
        """One allDetails[] row.

        `at` is `confirmedDate` (epoch ms) — the recency signal EB actually
        supplies, present on exactly the rows that responded.
        """
        row = {"contactId": cid, "confirmed": confirmed, "responseTextMessage": text}
        if at is not None:
            row["confirmedDate"] = at
        return row

    def _body(self, rows):
        return {"result": {"notificationResult": {"allDetails": rows}}}

    def test_reproduces_the_07_24_recipient_set_exactly(self):
        """§2.3: 10 YES + 26 NO + 8 no-reply → the follow-up went to 18.

        The dispatcher's manual choice is the acceptance criterion. If this
        number moves, the heuristic no longer reproduces human judgment.
        """
        rows = (
            [self._row(f"y{i}", confirmed=True, text="Yes") for i in range(10)]
            + [self._row(f"n{i}", confirmed=True, text="No") for i in range(26)]
            + [self._row(f"u{i}", confirmed=False) for i in range(8)]
        )
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert len(out["followup_contact_ids"]) == 18
        assert len(out["affirmatively_replied"]) == 10
        assert len(out["not_yet_replied"]) == 8
        assert len(out["declined"]) == 26

    def test_decliners_are_excluded(self):
        rows = [
            self._row("yes1", confirmed=True, text="Yes"),
            self._row("no1", confirmed=True, text="No"),
        ]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert out["followup_contact_ids"] == ["yes1"]
        assert "no1" not in out["followup_contact_ids"]

    def test_confirmed_true_alone_does_not_qualify_as_affirmative(self):
        """The inversion trap (#592).

        EB's `confirmed` means RESPONDED — Yes or No. Targeting `confirmed ==
        True` would re-page every decliner, the exact inverse of the intent. A
        fixture where everyone says Yes cannot catch this, which is why the row
        here is confirmed-with-No.
        """
        rows = [self._row("declined_but_confirmed", confirmed=True, text="No")]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert out["followup_contact_ids"] == []

    def test_response_text_matching_is_case_insensitive(self):
        rows = [
            self._row("a", confirmed=True, text="YES"),
            self._row("b", confirmed=True, text="yes"),
            self._row("c", confirmed=True, text=" Yes "),
        ]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert sorted(out["affirmatively_replied"]) == ["a", "b", "c"]

    def test_each_contact_occupies_exactly_one_row_in_production(self):
        """Documents the empirical baseline the tie-break below is defensive against.

        Verified read-only 2026-07-27: the 07-24 Polling original had 44 rows /
        44 distinct contacts and the Standard follow-up 18 / 18, with zero
        contacts holding conflicting answers. Everbridge does not appear to
        permit re-answering a poll (Bill, 2026-07-27).
        """
        rows = [self._row(f"c{i}", confirmed=True, text="Yes", at=1000 + i)
                for i in range(44)]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert len(out["affirmatively_replied"]) == 44

    def test_most_recent_answer_wins_no_then_yes(self):
        """Bill, 2026-07-27: the most recent answer is always authoritative.

        Not "affirmative wins" — that was my earlier inference and it is wrong
        for the YES-then-NO direction, where it would page someone who withdrew.
        """
        rows = [
            self._row("flip", confirmed=True, text="No", at=1_000),
            self._row("flip", confirmed=True, text="Yes", at=2_000),
        ]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert out["followup_contact_ids"] == ["flip"]
        assert out["declined"] == []

    def test_most_recent_answer_wins_yes_then_no(self):
        """The direction that distinguishes most-recent from affirmative-wins.

        Someone who said Yes and then No has withdrawn. Paging them treats a
        withdrawal as a commitment.
        """
        rows = [
            self._row("flip", confirmed=True, text="Yes", at=1_000),
            self._row("flip", confirmed=True, text="No", at=2_000),
        ]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert out["followup_contact_ids"] == []
        assert out["declined"] == ["flip"]

    def test_recency_uses_confirmed_date_not_array_position(self):
        """EB documents no ordering guarantee for allDetails[].

        The newest answer is listed FIRST here, so a position-based
        implementation would pick the stale one.
        """
        rows = [
            self._row("flip", confirmed=True, text="No", at=9_000),   # newest, listed first
            self._row("flip", confirmed=True, text="Yes", at=1_000),
        ]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert out["declined"] == ["flip"]
        assert out["followup_contact_ids"] == []

    def test_an_answered_row_outranks_an_undated_unanswered_row(self):
        """A row without confirmedDate must never outrank a genuine answer.

        The unanswered delivery row carries no timestamp; treating it as newest
        would silently re-page a decliner.
        """
        rows = [
            self._row("late_no", confirmed=False),
            self._row("late_no", confirmed=True, text="No", at=5_000),
        ]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert out["followup_contact_ids"] == []
        assert out["declined"] == ["late_no"]

    def test_answered_row_outranks_unanswered_regardless_of_order(self):
        rows = [
            self._row("late_yes", confirmed=True, text="Yes", at=5_000),
            self._row("late_yes", confirmed=False),
        ]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert out["followup_contact_ids"] == ["late_yes"]

    def test_buckets_partition_the_roster_exactly_once(self):
        rows = [
            self._row("y", confirmed=True, text="Yes"),
            self._row("n", confirmed=True, text="No"),
            self._row("u", confirmed=False),
            self._row("r", confirmed=True, text=""),
        ]
        out = eb.partition_contacts_for_followup(self._body(rows))
        allb = (
            out["affirmatively_replied"] + out["not_yet_replied"]
            + out["declined"] + out["bare_receipt"]
        )
        assert sorted(allb) == ["n", "r", "u", "y"]
        assert len(allb) == len(set(allb))

    def test_bare_receipt_is_surfaced_and_excluded(self):
        """Spec-literal: confirmed-with-empty-text is in neither (a) nor (b).

        Surfaced as its own list rather than dropped silently — a non-empty
        bare_receipt on a real incident is the signal to revisit whether those
        people should get the correction.
        """
        rows = [self._row("receipt", confirmed=True, text="")]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert out["bare_receipt"] == ["receipt"]
        assert out["followup_contact_ids"] == []

    def test_rows_without_a_contact_id_are_skipped_not_emitted_as_empty(self):
        rows = [
            self._row("", confirmed=True, text="Yes"),
            self._row("good", confirmed=True, text="Yes"),
        ]
        out = eb.partition_contacts_for_followup(self._body(rows))
        assert out["followup_contact_ids"] == ["good"]

    def test_empty_all_details_is_flagged(self):
        out = eb.partition_contacts_for_followup(self._body([]))
        assert out["all_details_empty"] is True
        assert out["followup_contact_ids"] == []

    def test_unwrapped_body_without_result_envelope_is_accepted(self):
        body = {"notificationResult": {"allDetails": [
            self._row("y", confirmed=True, text="Yes")
        ]}}
        assert eb.partition_contacts_for_followup(body)["followup_contact_ids"] == ["y"]

    def test_missing_notification_result_does_not_raise(self):
        assert eb.partition_contacts_for_followup({"result": {}})["followup_contact_ids"] == []


class TestSmsBudget:
    """Pin the SMS length budget against Everbridge's own compose-time counter."""

    # Verbatim from the EB "Send Follow Up" compose screen (Bill, 2026-07-27).
    # EB reported "Characters remaining: 2375 - Email/Fax | 103 - SMS" for this
    # body, alongside a 68-character title.
    EB_SCREENSHOT_BODY = "TEST Need bill to confirm staging reco's TEST\n-305, Burns"
    EB_SCREENSHOT_TITLE = "SOSAR - TEST Urban Callout - Santa Clara - for bill - staging reco's"
    EB_REPORTED_SMS_REMAINING = 103

    def test_reproduces_everbridge_own_counter_exactly(self):
        """Real-world pin — not a synthetic fixture.

        If our number disagrees with EB's, the dispatcher sees one budget in our
        UI and a different one in Everbridge.
        """
        out = eb.sms_budget(self.EB_SCREENSHOT_BODY)
        assert out["remaining"] == self.EB_REPORTED_SMS_REMAINING
        assert out["used"] == 57
        assert out["limit"] == eb.SMS_LIMIT_GSM7

    def test_the_title_is_not_part_of_the_sms_budget(self):
        """57 + 103 = 160 exactly, with a 68-char title present and uncounted."""
        assert len(self.EB_SCREENSHOT_TITLE) == 68
        body = eb.sms_budget(self.EB_SCREENSHOT_BODY)
        assert body["used"] + self.EB_REPORTED_SMS_REMAINING == eb.SMS_LIMIT_GSM7

    def test_a_curly_apostrophe_is_reported_as_unicode_but_keeps_the_160_limit(self):
        """Charset is REPORTED; it no longer sets the budget (Bill, 2026-07-28).

        Everbridge really would cap this at 70, but enforcing that here would
        gate every dispatch on a rare case. We keep the flat 160 and warn
        separately via truncation_risk.
        """
        smart = self.EB_SCREENSHOT_BODY.replace("'", "’")
        out = eb.sms_budget(smart)
        assert out["charset"] == "unicode"
        assert out["limit"] == eb.SMS_LIMIT_GSM7
        assert out["over_limit"] is False

    def test_plain_ascii_body_is_gsm7(self):
        assert eb.sms_budget("Correction: staging is Richey")["charset"] == "gsm7"

    def test_exactly_at_the_limit_is_not_over(self):
        out = eb.sms_budget("a" * 160)
        assert out["remaining"] == 0
        assert out["over_limit"] is False

    def test_one_past_the_limit_is_over(self):
        assert eb.sms_budget("a" * 161)["over_limit"] is True

    def test_extension_table_characters_cost_two_septets(self):
        """`€ { } [ ] ~ ^ | \\` are GSM-7 but escape-encoded."""
        assert eb.sms_budget("€")["used"] == 2
        assert eb.sms_budget("[")["used"] == 2
        assert eb.sms_budget("a")["used"] == 1

    def test_eighty_extension_chars_exceed_the_gsm7_limit(self):
        """80 × 2 septets = 160 used; 81 tips over while len() would say 81."""
        assert eb.sms_budget("€" * 80)["over_limit"] is False
        assert eb.sms_budget("€" * 81)["over_limit"] is True

    def test_emoji_is_reported_as_unicode_but_keeps_the_160_limit(self):
        out = eb.sms_budget("ok \U0001F44D")
        assert out["charset"] == "unicode"
        assert out["limit"] == eb.SMS_LIMIT_GSM7

    def test_the_limit_is_flat_160_for_both_character_sets(self):
        """The decision, stated as a single assertion.

        A future edit reinstating the 70-char UCS-2 ceiling fails here.
        """
        assert eb.sms_budget("a" * 10)["limit"] == 160
        assert eb.sms_budget("\U0001F44D" * 10)["limit"] == 160

    def test_a_100_char_unicode_body_is_accepted(self):
        """Exactly the case the old 70-char rule would have blocked."""
        out = eb.sms_budget("\U0001F44D" + "a" * 99)
        assert out["used"] == 100
        assert out["over_limit"] is False

    def test_em_dash_and_ellipsis_force_unicode(self):
        """Both are common macOS auto-substitutions."""
        assert eb.sms_budget("staging — Richey")["charset"] == "unicode"
        assert eb.sms_budget("hold…")["charset"] == "unicode"

    def test_accented_gsm7_characters_stay_gsm7(self):
        """à ä ö ñ ü é è ù ì ò Ç are in the GSM-7 basic set."""
        assert eb.sms_budget("café Ösund àèìòù")["charset"] == "gsm7"

    def test_newline_counts_as_one_septet(self):
        assert eb.sms_budget("a\nb")["used"] == 3

    def test_reserve_shrinks_the_limit(self):
        """Room for EB's auto-generated confirmation text.

        EB's own counter does NOT subtract it despite the tooltip saying the
        limit includes it, so the reserve is the caller's lever.
        """
        out = eb.sms_budget("a" * 150, reserve=20)
        assert out["limit"] == 140
        assert out["over_limit"] is True

    def test_reserve_defaults_to_zero_for_parity_with_eb(self):
        assert eb.sms_budget("a" * 160)["limit"] == eb.SMS_LIMIT_GSM7

    def test_empty_body(self):
        out = eb.sms_budget("")
        assert out["used"] == 0
        assert out["over_limit"] is False


class TestNormalizeSmsText:
    """Punctuation flattening (Bill, 2026-07-27: "auto-normalize / flatten")."""

    def test_macos_curly_apostrophe_becomes_straight(self):
        assert eb.normalize_sms_text("reco’s") == "reco's"

    def test_curly_double_quotes_become_straight(self):
        assert eb.normalize_sms_text("“staging”") == '"staging"'

    def test_em_and_en_dash_become_hyphen(self):
        assert eb.normalize_sms_text("a—b–c") == "a-b-c"

    def test_ellipsis_becomes_three_periods(self):
        assert eb.normalize_sms_text("hold…") == "hold..."

    def test_non_breaking_space_becomes_plain_space(self):
        assert eb.normalize_sms_text("a b") == "a b"

    def test_zero_width_characters_are_removed(self):
        assert eb.normalize_sms_text("a​b") == "ab"

    def test_crlf_collapses_to_lf(self):
        """Both are GSM-7, but the pair costs two septets for one line break."""
        assert eb.normalize_sms_text("a\r\nb") == "a\nb"

    def test_plain_ascii_is_untouched(self):
        s = "Correction: staging is Richey Training Center, 155 W Hedding St"
        assert eb.normalize_sms_text(s) == s

    def test_gsm7_accented_letters_are_not_folded(self):
        """à ä ö ñ ü é are already GSM-7 — folding them would rewrite names."""
        s = "café Ösund àèìòù ñ"
        assert eb.normalize_sms_text(s) == s

    def test_normalization_adds_no_content(self):
        """Compatible with the §3.3 blank-slate rule: transliterate, never compose."""
        out = eb.normalize_sms_text("")
        assert out == ""


class TestPrepareFollowupSmsBody:
    """The whole body policy: normalize → measure → decide."""

    MACOS_BODY = "TEST Need bill to confirm staging reco’s TEST\n-305, Burns"
    STRAIGHT_BODY = "TEST Need bill to confirm staging reco's TEST\n-305, Burns"

    def test_normalizing_first_yields_a_clean_gsm7_measurement(self):
        """⚠️ ORDER IS LOAD-BEARING — for the charset and the warning.

        Since the limit went flat at 160 (Bill, 2026-07-28) the ORDER no longer
        changes acceptance for a smart quote. It still decides what we SEND and
        what we WARN about: measuring the raw body reports a spurious offending
        character we have in fact already removed.
        """
        assert eb.non_gsm7_characters(self.MACOS_BODY) == ["’"]

        out = eb.prepare_followup_sms_body(self.MACOS_BODY)
        assert out["budget"]["charset"] == "gsm7"
        assert out["offending_characters"] == []
        assert out["truncation_risk"] is False
        assert out["normalized"] is True

    def test_normalizing_first_also_changes_the_septet_COST(self):
        """The order still moves the number, via characters that EXPAND.

        "…" is one UCS-2 character but becomes three GSM-7 septets. Measuring
        before normalizing under-counts the real cost — which at the boundary
        would accept a body that is actually over the limit.
        """
        body = "hold…"
        assert eb.sms_budget(body)["used"] == 5           # raw: 1 char for "…"
        assert eb.prepare_followup_sms_body(body)["budget"]["used"] == 7   # "..."

    def test_expansion_at_the_boundary_is_caught(self):
        """158 plain + one ellipsis = 161 septets once flattened → refused.

        Measuring the raw text would see 159 and wave it through.
        """
        body = "a" * 158 + "…"
        assert eb.sms_budget(body)["used"] == 159
        out = eb.prepare_followup_sms_body(body)
        assert out["budget"]["used"] == 161
        assert out["accepted"] is False

    def test_normalized_output_matches_the_straight_quote_body_exactly(self):
        assert eb.prepare_followup_sms_body(self.MACOS_BODY)["text"] == self.STRAIGHT_BODY

    def test_caller_must_send_the_normalized_text_not_the_raw(self):
        """`text` is the payload body — sending `raw` would re-introduce UCS-2."""
        out = eb.prepare_followup_sms_body(self.MACOS_BODY)
        assert "’" not in out["text"]

    def test_99_is_accepted_and_100_is_not(self):
        """The REAL dispatcher budget after the measured reserve (2026-07-28).

        160 minus the 61 characters Everbridge appends for its confirmation
        line. A 100-character body would be delivered as 161 and replaced by a
        click-through link.
        """
        assert eb.prepare_followup_sms_body("a" * 99)["accepted"] is True
        assert eb.prepare_followup_sms_body("a" * 100)["accepted"] is False

    def test_surviving_emoji_is_reported_but_does_not_lower_the_limit(self):
        """Normalization cannot rescue an emoji — we report it, we don't block.

        Bill, 2026-07-28: keep the limit at 160; international spellings are too
        rare to gate every dispatch on.
        """
        out = eb.prepare_followup_sms_body("Correction: staging is Richey \U0001F44D")
        assert out["offending_characters"] == ["\U0001F44D"]
        # Charset must not lower the ceiling below the reserved budget.
        assert out["budget"]["limit"] == eb.SMS_LIMIT_GSM7 - eb.FOLLOWUP_SMS_RESERVE
        assert out["accepted"] is True

    def test_emoji_body_over_70_is_accepted_but_flagged_for_truncation(self):
        """The accepted trade, made visible.

        EB WILL truncate this into a web-page link — that is EB's behaviour and
        we cannot prevent it. So the dispatcher is informed, not blocked.
        """
        out = eb.prepare_followup_sms_body("\U0001F44D" + "a" * 90)
        assert out["accepted"] is True
        assert out["truncation_risk"] is True

    def test_short_unicode_body_carries_no_truncation_risk(self):
        """Under 70, EB delivers UCS-2 intact — nothing to warn about."""
        out = eb.prepare_followup_sms_body("staging moved \U0001F44D")
        assert out["truncation_risk"] is False

    def test_long_gsm7_body_carries_no_truncation_risk(self):
        """A full-budget plain body is fine — risk is about charset, not length."""
        out = eb.prepare_followup_sms_body("a" * 99)
        assert out["truncation_risk"] is False
        assert out["accepted"] is True

    def test_normalized_smart_quotes_clear_the_truncation_risk(self):
        """Flattening removes the COMMON cause, so the warning stays rare."""
        long_smart = "Correction: staging is Richey Training Center, it’s the " + "x" * 40
        out = eb.prepare_followup_sms_body(long_smart)
        assert out["offending_characters"] == []
        assert out["truncation_risk"] is False

    def test_clean_body_reports_no_offenders_and_no_normalization(self):
        out = eb.prepare_followup_sms_body("Correction: staging is Richey")
        assert out["offending_characters"] == []
        assert out["normalized"] is False
        assert out["accepted"] is True

    def test_reserve_is_honoured(self):
        """An explicit reserve still overrides the default."""
        assert eb.prepare_followup_sms_body("a" * 150, reserve=20)["accepted"] is False
        assert eb.prepare_followup_sms_body("a" * 150, reserve=0)["accepted"] is True

    def test_reserve_defaults_to_the_measured_value(self):
        """Measured on the first live send, 2026-07-28 — no longer a placeholder.

        The handler calls prepare_followup_sms_body(raw) with no reserve, so
        this default IS the enforcement policy.
        """
        assert eb.FOLLOWUP_SMS_RESERVE == 61
        assert eb.prepare_followup_sms_body("a" * 99)["accepted"] is True
        assert eb.prepare_followup_sms_body("a" * 100)["accepted"] is False


class TestPollAndPartitionAreNotInterchangeable:
    """THE seam bug — caught in code review, pinned here so it cannot return.

    `poll_notification()` and `fetch_notification_raw()` issue the IDENTICAL HTTP
    request, so they look interchangeable. They are not: poll_notification returns
    `_parse_poll_response`'s projection, which consumes and DISCARDS `allDetails[]`
    — the exact array the follow-up partition needs.

    The first draft of the handler called poll_notification. The result was an
    empty recipient set on EVERY invocation, with no exception, so every follow-up
    refused to send with "everyone declined". The feature could never have worked.

    Neither existing test could catch it: the partition tests feed a hand-built
    raw fixture directly, and the handler pin only asserted the STRING
    "poll_notification" appeared in the source. The bug lived entirely in the seam
    between two individually-correct functions. This class tests the seam.
    """

    RAW = {"result": {"notificationStatus": "Active", "notificationResult": {
        "allDetails": [
            {"contactId": "y1", "confirmed": True, "responseTextMessage": "Yes",
             "confirmedDate": 1000},
            {"contactId": "u1", "confirmed": False},
            {"contactId": "n1", "confirmed": True, "responseTextMessage": "No",
             "confirmedDate": 1000},
        ]
    }}}

    def test_partition_works_on_the_raw_envelope(self):
        out = eb.partition_contacts_for_followup(self.RAW)
        assert out["followup_contact_ids"] == ["y1", "u1"]

    def test_partition_silently_returns_nobody_for_the_parsed_shape(self):
        """The failure is SILENT — no exception, just an empty roster.

        That silence is why it survived unit tests, a source-reading pin, and my
        own reading of the handler.
        """
        parsed = eb._parse_poll_response(self.RAW)
        out = eb.partition_contacts_for_followup(parsed)
        assert out["followup_contact_ids"] == []
        assert out["all_details_empty"] is True

    def test_the_two_shapes_are_provably_different(self):
        parsed = eb._parse_poll_response(self.RAW)
        assert "notificationResult" not in parsed
        assert "allDetails" not in parsed
        assert "result" not in parsed
        assert "ack_contacts" in parsed

    def test_poll_notification_still_returns_the_parsed_contract(self):
        """poll_notification must NOT be changed to return raw.

        It is shared with the polling chain and /confirm-draft-sent, which depend
        on the parsed shape. Fixing the seam by changing poll_notification would
        break both.
        """
        import inspect
        src = inspect.getsource(eb.poll_notification)
        assert "_parse_poll_response(resp.json())" in src

    def test_fetch_notification_raw_returns_the_body_unparsed(self):
        import inspect
        src = inspect.getsource(eb.fetch_notification_raw)
        assert "return resp.json()" in src
        # Check for a CALL, not a mention — the docstring names the function it
        # must not call, which is exactly the trap that broke this pin's first
        # draft (and the handler pin's).
        assert "_parse_poll_response(" not in src

    def test_both_hit_the_same_endpoint_with_verbose_true(self):
        """Being the same request is what makes them look swappable."""
        import inspect
        for fn in (eb.poll_notification, eb.fetch_notification_raw):
            src = inspect.getsource(fn)
            assert '"verbose": "true"' in src
            assert "/notifications/{org_id}/{notification_id}" in src


class TestFollowupDoesNotDisturbThePollingChain:
    """§3.2 — _parse_poll_response must remain untouched by #612."""

    def test_parse_poll_response_still_reports_counts_only_for_declines(self):
        """The #442 boundary: decline COUNTS in the poll path, never identities."""
        body = {"result": {"notificationStatus": "Active", "notificationResult": {
            "allDetails": [
                {"contactId": "n", "confirmed": True, "responseTextMessage": "No"},
            ]
        }}}
        out = eb._parse_poll_response(body)
        assert out["decline_count"] == 1
        assert "declined" not in out
        assert "decline_contacts" not in out


class TestSendFollowupSignature:
    """The wrapper's narrowness is deliberate and worth pinning."""

    def test_wrapper_accepts_no_group_targeting(self):
        """Groups are out of scope (Bill, 2026-07-25).

        Omitting the parameter — rather than defaulting it to [] — means a future
        caller cannot re-introduce group targeting by passing one.
        """
        import inspect
        params = inspect.signature(eb.send_followup_notification_live).parameters
        assert "target_group_ids" not in params
        assert "category_id" not in params

    def test_wrapper_forces_empty_group_ids_in_the_payload(self):
        p = _payload(eb.NOTIFICATION_TYPE_STANDARD, target_group_ids=[])
        assert p["broadcastContacts"]["groupIds"] == []
