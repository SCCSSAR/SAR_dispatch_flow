"""test_slack.py — pure-logic tests for slack.py helpers.

Per CLAUDE.md test file pattern: mirror the constants + pure-logic
functions from `backend/slack.py` locally rather than importing the
module directly. `slack.py` imports `slack_sdk`, which is not installed
in the local pytest environment (it lives in the Cloud Run container).

When updating helpers in slack.py, ALSO update the mirror here. The
mirror IS the test contract — if slack.py drifts away from this mirror,
that drift will surface in production behavior, and these tests are the
regression boundary.

Test architecture (Tasks 1.5–1.7 design decision, with Bill 2026-04-26):
  - Pure-logic helpers (channel naming, gender/at-risk normalization,
    message formatting) ARE mirrored here and exercised under pytest.
  - SDK wrappers (find_or_create_private_channel, invite_user,
    post_message, edit_message, pin_message, lookup_user_by_email,
    get_active_incident_management_members, get_incident_channel_initial_members)
    are NOT exercised here — they're thin glue (build args → SDK call →
    return). They're covered at live-test time on personal-dev. Revisit
    at end of Phase 1 (after Task 1.11) for a requirements-test.txt +
    venv-based pytest env.

Specifically tested (Task 1.7):
  - incident_channel_name() / _with_collision_suffix() — naming policy
    across surfaces; HHMM stripped by default, suffix appended on collision
  - _gender_display() — long-form mapping (Item 4)
  - format_pinned_welcome() — full pinned message shape (Items 4 + 5)
  - format_would_invite_message() — shadow-mode line shape
  - format_groups_requested() — channel-creation message (Item 7)
  - format_tally_responder_line() / _multi_team_line() — live tally lines
    including Item 6 (`↳` indent, NOT 🔸 emoji)
"""
import ast
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Mirrored constants and helpers — must be kept in sync with backend/slack.py
# ---------------------------------------------------------------------------

_HHMM_SUFFIX_RE = re.compile(r"\s\d{4}$")

_GENDER_LONG_FORM = {
    "M":  "Male",
    "F":  "Female",
    "NB": "Non-Binary",
}


def _strip_hhmm_suffix(event_name: str) -> str:
    return _HHMM_SUFFIX_RE.sub("", event_name)


# Mirror of backend/slack.py Slack channel-name character policy.
_SLACK_CHANNEL_DISALLOWED_RE = re.compile(r"[^a-z0-9_-]+")
_SLACK_CHANNEL_MAX_LEN = 80


def _slugify_channel_name(text: str, *, max_len: int = _SLACK_CHANNEL_MAX_LEN) -> str:
    """Mirror of backend/slack.py::_slugify_channel_name()."""
    slug = "_".join(text.lower().split())
    slug = slug.replace("'", "").replace("’", "")
    slug = _SLACK_CHANNEL_DISALLOWED_RE.sub("_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_-")
    return slug[:max_len].rstrip("_-")


def incident_channel_name(event_name: str) -> str:
    """Mirror of backend/slack.py::incident_channel_name()."""
    bare = _strip_hhmm_suffix(event_name)
    return _slugify_channel_name(bare)


def incident_channel_name_with_collision_suffix(event_name_with_hhmm: str) -> str:
    """Mirror of backend/slack.py::incident_channel_name_with_collision_suffix()."""
    m = _HHMM_SUFFIX_RE.search(event_name_with_hhmm)
    if not m:
        raise ValueError(
            "incident_channel_name_with_collision_suffix requires the HHMM suffix "
            "from the Everbridge event name"
        )
    hhmm = m.group().strip()
    base = _slugify_channel_name(
        _strip_hhmm_suffix(event_name_with_hhmm),
        max_len=_SLACK_CHANNEL_MAX_LEN - len(hhmm) - 1,
    )
    return f"{base}_{hhmm}"


def _gender_display(gender: str) -> str:
    """Mirror of backend/slack.py::_gender_display()."""
    return _GENDER_LONG_FORM.get((gender or "").strip().upper(), "Unknown")


def novel_notes(raw_notes: str, at_risk: str) -> str:
    """Mirror of backend/slack.py::novel_notes() (issue #670)."""
    if not raw_notes or not raw_notes.strip():
        return ""
    haystack = (at_risk or "").lower()
    kept = []
    for snippet in raw_notes.split(";"):
        snippet = snippet.strip()
        if not snippet:
            continue
        words = re.findall(r"[a-z]{4,}", snippet.lower())
        if words and all(w in haystack for w in words):
            continue
        kept.append(snippet)
    return "; ".join(kept)


def _mrkdwn_escape(text):
    """Mirror of backend/slack.py::_mrkdwn_escape()."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_pinned_welcome(
    *,
    event_name: str,
    mp_name: str,
    age,
    gender: str,
    at_risk: str,
    wearing: str = "",
    notes: str = "",
    last_seen: str = "",
    request: str = "",
    officer_contact: str = "",
) -> str:
    """Mirror of backend/slack.py::format_pinned_welcome().

    Staging left this message in #673 — it is its own pinned message now, so
    it can be deleted and re-posted when a dispatch goes out with the wrong
    location. See format_staging_message below.
    """
    gender_display = _gender_display(gender)
    at_risk_clean = at_risk.strip()
    if not at_risk_clean:
        at_risk_clean = "no risk factors"
    # Cluster F (Slack-M6) mirror: also treat empty-string as "no age."
    age_str = f"{age}yo" if age not in (None, "") else "?yo"
    notes_clean = novel_notes(notes, at_risk_clean)
    mp_name = _mrkdwn_escape(mp_name)
    at_risk_clean = _mrkdwn_escape(at_risk_clean)
    mp_line = f"MP: {mp_name} – {age_str} {gender_display}, {at_risk_clean}"

    contact_clean = officer_contact.strip()
    event_name = _mrkdwn_escape(event_name)
    lines = [
        f"*{event_name}*",
        mp_line,
    ]
    wearing_clean = wearing.strip()
    if (wearing_clean and not wearing_clean.startswith("[")
            and wearing_clean.casefold() not in ("not recorded", "unknown", "n/a")):
        wearing_clean = _mrkdwn_escape(wearing_clean)
        lines.append(f"Wearing: {wearing_clean}")
    if notes_clean:
        notes_clean = _mrkdwn_escape(notes_clean)
        lines.append(f"Notes: {notes_clean}")
    last_seen_clean = last_seen.strip()
    if last_seen_clean and not last_seen_clean.startswith("["):
        last_seen_clean = _mrkdwn_escape(last_seen_clean)
        lines.append(f"Last seen: {last_seen_clean}")
    request_clean = request.strip()
    if request_clean and request_clean.lower() != "[not recorded]":
        request_clean = _mrkdwn_escape(request_clean)
        lines.append(f"Request: {request_clean}")
    if contact_clean:
        contact_clean = _mrkdwn_escape(contact_clean)
        lines.append(f"Contact: {contact_clean}")
    return "\n".join(lines)


STAGING_UNVERIFIED_WARNING = (
    "⚠️ Location conflict — the officer's staging coordinate is far from the "
    "mapped Last Known Position. Confirm with Dispatch before rolling."
)


STAGING_UNMAPPED_WARNING = (
    "⚠️ Unverified address — no mapped locations were found near the Last Known "
    "Position, so these options were not checked against map data. Confirm with "
    "Dispatch before rolling."
)


def format_staging_message(
    *,
    staging_address: str,
    staging_apple_url: str,
    staging_google_url: str,
    unverified: bool = False,
    unmapped: bool = False,
) -> str:
    """Mirror of backend/slack.py::format_staging_message() (issue #673)."""
    staging_address = _mrkdwn_escape(staging_address)
    lines = [f"Staging: <{staging_apple_url}|{staging_address}> (<{staging_google_url}|G>)"]
    if unverified:
        lines.append(STAGING_UNVERIFIED_WARNING)
    if unmapped:
        lines.append(STAGING_UNMAPPED_WARNING)
    return "\n".join(lines)


def to_conversational_name(name: str) -> str:
    """Mirror of backend/slack.py::to_conversational_name().

    Converts 'Last, First' → 'First Last' for display. Splits on the FIRST
    ', ' only so multi-word last names like 'Cadena Resendez, Priscilla'
    become 'Priscilla Cadena Resendez' rather than 'Priscilla Cadena, Resendez'.
    """
    if not name or ", " not in name:
        return name
    last, first = name.split(", ", 1)
    return f"{first} {last}"


def format_would_invite_message(
    *,
    already_member_names: list,
    resolvable_names: list,
    unresolvable_names: list,
) -> str:
    """Mirror of backend/slack.py::format_would_invite_message()."""
    lines = []
    for name in already_member_names:
        lines.append(f"{to_conversational_name(name)} added")
    if resolvable_names:
        converted = [to_conversational_name(n) for n in resolvable_names]
        lines.append(f"Would invite: {'; '.join(converted)}")
    if unresolvable_names:
        converted = [to_conversational_name(n) for n in unresolvable_names]
        lines.append(
            f"Cannot invite (no SAR email in Everbridge): "
            f"{'; '.join(converted)}"
        )
    return "\n".join(lines)


# Mirror of backend/slack.py INVITE_FAIL_* + format_invite_failed_message().
INVITE_FAIL_NO_EB_EMAIL   = "no_eb_email"
INVITE_FAIL_NO_SLACK_USER = "no_slack_user"
INVITE_FAIL_ERROR         = "error"


def format_invite_failed_message(*, name: str, reason: str) -> str:
    """Mirror of backend/slack.py::format_invite_failed_message()."""
    who = to_conversational_name(name)
    if reason == INVITE_FAIL_NO_EB_EMAIL:
        why = "has no SAR email in Everbridge"
    elif reason == INVITE_FAIL_NO_SLACK_USER:
        why = "has no Slack account"
    else:
        why = "could not be added automatically"
    return f"⚠️ {who} replied YES but {why} — add to channel manually"


def format_groups_requested(group_names: list) -> str:
    """Mirror of backend/slack.py::format_groups_requested()."""
    return f"Groups requested by dispatcher: {', '.join(group_names)}"


def format_off_call_excluded(names: list) -> str:
    """Mirror of backend/slack.py::format_off_call_excluded()."""
    return f"🚫 Unavailable in D4H (not paged): {', '.join(names)}"


def format_tally_responder_line(group: str, names: list) -> str:
    """Mirror of backend/slack.py::format_tally_responder_line()."""
    converted = [to_conversational_name(n) for n in names]
    return f"• {group} ({len(names)}): {'; '.join(converted)}"


def format_tally_multi_team_line(name: str, groups: list) -> str:
    """Mirror of backend/slack.py::format_tally_multi_team_line()."""
    return f"  ↳ {to_conversational_name(name)}: {', '.join(groups)}"


def format_incident_dm_text(*, channel_id: str, test_label: str = "") -> str:
    """Mirror of backend/slack.py::format_incident_dm_text().

    Composes the responder DM sent when they're added to an incident channel.
    Pairs with the VIP-breakthrough-DM feature — PRD at
    SAR Slack Onboarding/PRD_DispatchTurbo_Responder_DM_VIP_Breakthrough.
    """
    channel_id_clean = (channel_id or "").strip()
    if not channel_id_clean:
        raise ValueError(
            "format_incident_dm_text requires a non-empty channel_id"
        )
    body = (
        f"You've been added to incident <#{channel_id_clean}>. "
        f"Tap to open and check in for further instructions."
    )
    label_clean = (test_label or "").strip()
    if not label_clean:
        return body
    return f"{label_clean}\n\n{body}"


_SWALLOWED_INVITE_ERRORS = frozenset({
    "already_in_channel",
    "cant_invite_self",
})


def _should_swallow_invite_error(err):
    """Mirror of backend/slack.py::_should_swallow_invite_error()."""
    return err in _SWALLOWED_INVITE_ERRORS


# ---------------------------------------------------------------------------
# incident_channel_name() — naming policy across surfaces
# ---------------------------------------------------------------------------

class TestIncidentChannelName:
    def test_lowercases_and_underscores(self):
        # Default: strip the Everbridge HHMM suffix so Slack channel name
        # matches the human-facing form used in CalTopo + D4H.
        assert incident_channel_name("2026-04-25 MPD CALAVERAS 1430") == \
            "2026-04-25_mpd_calaveras"

    def test_strips_extra_whitespace(self):
        # split()+join handles arbitrary internal whitespace cleanly.
        assert incident_channel_name("2026-04-25  MPD   CALAVERAS 1430") == \
            "2026-04-25_mpd_calaveras"

    def test_handles_event_name_without_suffix(self):
        # Defensive — handles event names that already lack the suffix
        # (test helpers, manual ops). HHMM regex finds nothing → no-op.
        assert incident_channel_name("2026-04-25 MPD CALAVERAS") == \
            "2026-04-25_mpd_calaveras"

    def test_lowercases_uppercase_inputs(self):
        # Slack channel names are lowercase by spec; uppercase street names
        # from the canonical Everbridge format must be downcased.
        assert incident_channel_name("2026-04-25 SJPD UNIVERSITY 0930") == \
            "2026-04-25_sjpd_university"

    def test_strips_period_from_geocoded_park_name(self):
        # Live regression (2026-07-16 SJ Grant mock): the geocoder canonicalized
        # the LKP to the park's official name "Joseph D. Grant County Park". The
        # "." in "D." reached conversations.create → 'invalid_name_specials' →
        # /send-notification 500'd at Step 6 AFTER Everbridge had fired,
        # orphaning a half-incident. The period MUST be stripped.
        assert incident_channel_name(
            "2026-07-18 SCCO Joseph D. Grant County Park 1904"
        ) == "2026-07-18_scco_joseph_d_grant_county_park"

    def test_strips_apostrophe_and_period(self):
        # SCC has real locations like "St. Joseph's Hill" — apostrophe + period
        # are both Slack-illegal.
        assert incident_channel_name("2026-04-25 SCCO St. Joseph's Hill 1200") == \
            "2026-04-25_scco_st_josephs_hill"

    def test_strips_ampersand_intersection_and_collapses(self):
        # Intersection LKP "5th & Main": the "&" run collapses to a single
        # underscore (not deleted → would fuse to "5thmain").
        assert incident_channel_name("2026-04-25 SJPD 5th & Main 0800") == \
            "2026-04-25_sjpd_5th_main"

    def test_strips_assorted_punctuation(self):
        # Comma, slash, hash, parens all removed; runs collapse.
        assert incident_channel_name("2026-04-25 SO A/B, C#3 (Lot) 0900") == \
            "2026-04-25_so_a_b_c_3_lot"

    def test_preserves_hyphens(self):
        # Hyphens are legal in Slack names — the YYYY-MM-DD date prefix and
        # hyphenated street names must survive untouched.
        assert incident_channel_name("2026-04-25 MPD Verde-Vista 1000") == \
            "2026-04-25_mpd_verde-vista"

    def test_trims_leading_and_trailing_separators(self):
        # Punctuation at the edges must not leave a leading/trailing separator
        # (Slack rejects those); the date prefix keeps this from ever being
        # empty in practice.
        assert incident_channel_name("... 2026-04-25 SO Main ... 1100") == \
            "2026-04-25_so_main"

    def test_caps_at_slack_80_char_limit(self):
        # Slack hard-caps channel names at 80 chars; a long multi-word street
        # must be truncated without a trailing separator.
        long_street = "Very Long Multi Word Street Name That Keeps Going " \
                      "And Going Past The Limit"
        out = incident_channel_name(f"2026-04-25 SO {long_street} 1200")
        assert len(out) <= 80
        assert not out.endswith(("_", "-"))

    def test_output_always_matches_slack_grammar(self):
        # Property check: for a battery of punctuation-laden inputs, the output
        # is always a valid Slack channel name (^[a-z0-9_-]{1,80}$).
        nasty = [
            "2026-07-18 SCCO Joseph D. Grant County Park 1904",
            "2026-04-25 SJPD 5th & Main 0800",
            "2026-04-25 SCCO St. Joseph's Hill 1200",
            "2026-04-25 SO A/B, C#3 (Lot) 0900",
            "2026-04-25 PD Café Niño 1300",
        ]
        for ev in nasty:
            out = incident_channel_name(ev)
            assert re.fullmatch(r"[a-z0-9_-]{1,80}", out), f"{ev!r} -> {out!r}"


class TestIncidentChannelNameWithCollisionSuffix:
    def test_appends_hhmm_matching_everbridge(self):
        # When a same-day same-street incident already owns the bare name,
        # append the HHMM so the Slack channel matches the Everbridge event
        # 1:1 in the rare collision case.
        out = incident_channel_name_with_collision_suffix(
            "2026-04-25 MPD CALAVERAS 1430"
        )
        assert out == "2026-04-25_mpd_calaveras_1430"

    def test_raises_when_event_name_lacks_suffix(self):
        # Caller must pass the canonical Everbridge event name (with HHMM
        # suffix). Calling without is a programmer error and should fail
        # loudly, not silently produce a nonsense channel name.
        with pytest.raises(ValueError, match="HHMM suffix"):
            incident_channel_name_with_collision_suffix("2026-04-25 MPD CALAVERAS")

    def test_raises_when_suffix_is_only_3_digits(self):
        # The regex anchors on exactly 4 digits — '930' is a bug, not the
        # Everbridge canonical format.
        with pytest.raises(ValueError, match="HHMM suffix"):
            incident_channel_name_with_collision_suffix("2026-04-25 MPD CALAVERAS 930")

    def test_strips_period_but_keeps_hhmm(self):
        # Same sanitization as the bare name, but the HHMM suffix is retained
        # (kept 1:1 with the Everbridge event). The "." in "D." is stripped.
        out = incident_channel_name_with_collision_suffix(
            "2026-07-18 SCCO Joseph D. Grant County Park 1904"
        )
        assert out == "2026-07-18_scco_joseph_d_grant_county_park_1904"
        assert re.fullmatch(r"[a-z0-9_-]{1,80}", out)

    def test_long_name_preserves_hhmm_uniqueness(self):
        # Aikido review (PR #576): the 80-char cap must NOT right-truncate the
        # HHMM token, or two same-day-same-street collisions at different times
        # would collapse to the same channel name — defeating the whole point
        # of the collision suffix. Reserve room for "_HHMM" so it survives.
        long_street = ("Rancho San Antonio Open Space Preserve "
                       "Deer Hollow Farm Loop Trailhead")
        a = incident_channel_name_with_collision_suffix(
            f"2026-04-25 MROSD {long_street} 1200")
        b = incident_channel_name_with_collision_suffix(
            f"2026-04-25 MROSD {long_street} 1830")
        assert len(a) <= 80 and len(b) <= 80
        assert a.endswith("_1200") and b.endswith("_1830")
        assert a != b  # uniqueness preserved despite truncation


# ---------------------------------------------------------------------------
# _gender_display() — short-form → long-form (Item 4)
# ---------------------------------------------------------------------------

class TestGenderDisplay:
    def test_male_short_form(self):
        assert _gender_display("M") == "Male"

    def test_female_short_form(self):
        assert _gender_display("F") == "Female"

    def test_non_binary_short_form(self):
        assert _gender_display("NB") == "Non-Binary"

    def test_lowercase_input_normalized(self):
        # OCR sometimes returns lowercase — case insensitive.
        assert _gender_display("m") == "Male"
        assert _gender_display("f") == "Female"
        assert _gender_display("nb") == "Non-Binary"

    def test_whitespace_tolerated(self):
        assert _gender_display("  M  ") == "Male"

    def test_unknown_falls_through(self):
        # If OCR returns an unrecognized value, render placeholder so the
        # MP line still flows. Caller doesn't have to special-case this.
        assert _gender_display("Q") == "Unknown"

    def test_empty_string_falls_through(self):
        assert _gender_display("") == "Unknown"

    def test_none_safe(self):
        # Defensive — slack.py uses `(gender or "")` so None must not crash.
        assert _gender_display(None) == "Unknown"


# ---------------------------------------------------------------------------
# format_pinned_welcome() — Items 4 + 5 (design Section 5)
# ---------------------------------------------------------------------------

class TestFormatPinnedWelcome:
    def _kwargs(self, **overrides):
        base = dict(
            event_name="2026-04-25 MPD CALAVERAS",   # HHMM stripped by caller
            mp_name="John Smith",
            age=79, gender="M", at_risk="dementia",
            officer_contact="",   # omitted from welcome when blank
        )
        base.update(overrides)
        return base

    def test_event_name_bold(self):
        msg = format_pinned_welcome(**self._kwargs())
        # Slack mrkdwn: *...* renders bold. Pinning the asterisks here so a
        # future formatter switch (e.g. to Block Kit) doesn't quietly drop
        # the visual emphasis.
        assert "*2026-04-25 MPD CALAVERAS*" in msg

    def test_hhmm_does_not_leak_into_pinned_message(self):
        # Caller is responsible for stripping HHMM before passing event_name.
        # Pin: if a future PR forwards the canonical Everbridge name verbatim,
        # this test fails immediately.
        msg = format_pinned_welcome(**self._kwargs())
        assert "1430" not in msg

    def test_dispatcher_line_removed(self):
        # 2026-04-29: the welcome no longer includes a Dispatcher: line.
        # Dispatcher identity is recorded in EB notification metadata and the
        # backend audit log; field responders don't need it on the pinned
        # context message. If a future PR re-introduces "Dispatcher: ", this
        # test fails immediately.
        msg = format_pinned_welcome(**self._kwargs())
        assert "Dispatcher:" not in msg

    def test_mp_name_present(self):
        # Policy reversal 2026-04-29: MP full name IS included (was excluded
        # in design Section 5 Item 4). See CLAUDE.md "Slack welcome — MP name
        # policy" Locked Design Decision.
        msg = format_pinned_welcome(**self._kwargs(mp_name="Jane Doe"))
        assert "MP: Jane Doe – " in msg

    def test_mp_name_with_comma_preserved_verbatim(self):
        # Officers sometimes write "Lastname, Firstname" on the form. The
        # comma form must pass through unchanged — do NOT normalize it.
        # Verbatim passthrough ensures the welcome matches what the
        # dispatcher reviewed in the textarea.
        msg = format_pinned_welcome(**self._kwargs(mp_name="Smith, John"))
        assert "MP: Smith, John – " in msg

    def test_mp_line_uses_en_dash_not_hyphen(self):
        # Separator after MP name is en-dash (U+2013), NOT hyphen-minus
        # (U+002D). The en-dash renders with visual padding that
        # distinguishes name from age at a glance.
        msg = format_pinned_welcome(**self._kwargs(mp_name="John Smith"))
        assert "John Smith – 79yo" in msg
        # Forbidden: hyphen-minus separator
        assert "John Smith - 79yo" not in msg

    def test_mp_line_long_form_male(self):
        # Item 4: short-form 'M' → long-form 'Male'. Both halves of the rule
        # asserted (long-form present AND short-form leak absent).
        msg = format_pinned_welcome(
            **self._kwargs(mp_name="John Smith", age=79, gender="M", at_risk="dementia")
        )
        assert "MP: John Smith – 79yo Male, dementia" in msg
        assert "MP: John Smith – 79yo M," not in msg

    def test_mp_line_long_form_female(self):
        msg = format_pinned_welcome(
            **self._kwargs(mp_name="Jane Doe", age=23, gender="F", at_risk="autism")
        )
        assert "MP: Jane Doe – 23yo Female, autism" in msg

    def test_mp_line_long_form_non_binary(self):
        msg = format_pinned_welcome(
            **self._kwargs(mp_name="Alex Roe", age=9, gender="NB", at_risk="autism")
        )
        assert "MP: Alex Roe – 9yo Non-Binary, autism" in msg

    def test_mp_line_unknown_gender_falls_through(self):
        msg = format_pinned_welcome(
            **self._kwargs(mp_name="Pat Q", age=50, gender="", at_risk="dementia")
        )
        assert "MP: Pat Q – 50yo Unknown, dementia" in msg

    def test_at_risk_empty_renders_no_risk_factors(self):
        # Item 5 — a blank field is ambiguous to readers; explicit phrase
        # makes the absence intentional. Forbidden patterns explicitly pinned.
        msg = format_pinned_welcome(
            **self._kwargs(mp_name="Pat Q", age=50, gender="F", at_risk="")
        )
        assert "MP: Pat Q – 50yo Female, no risk factors" in msg
        assert "MP: Pat Q – 50yo Female\n"  not in msg   # missing-trailing behavior must NOT recur
        assert "MP: Pat Q – 50yo Female,,"  not in msg   # double-comma also forbidden

    def test_at_risk_whitespace_only_renders_no_risk_factors(self):
        # Item 5 edge case — whitespace-only at_risk is functionally empty.
        msg = format_pinned_welcome(**self._kwargs(at_risk="   "))
        assert "no risk factors" in msg

    def test_at_risk_long_passes_through_verbatim(self):
        # At-risk indicators are dispatcher-curated and arrive in
        # non-deterministic order. A length cap would silently hide a
        # critical factor (e.g., "armed" or "no proper outerwear" at the
        # tail). Pinned per CLAUDE.md "Slack at-risk — no truncation".
        long = (
            "First time runaway, depression, anxiety, prior suicide "
            "attempt, Alone, No proper outerwear, no medication, "
            "developmental disability, autism"
        )
        assert len(long) > 80  # would have been truncated under the prior cap
        msg = format_pinned_welcome(**self._kwargs(at_risk=long))
        assert long in msg
        assert "…" not in msg  # ellipsis is the truncation fingerprint

    def test_at_risk_extreme_length_passes_through_verbatim(self):
        # Defensive boundary — even a pathologically long at-risk string
        # is preserved verbatim. No cap exists at any length.
        long = "x" * 500
        msg = format_pinned_welcome(**self._kwargs(at_risk=long))
        assert long in msg
        assert "…" not in msg

    def test_staging_is_not_in_the_welcome(self):
        # #673: staging left this message so it can be deleted and re-posted
        # on its own when a dispatch goes out with the wrong location. Folding
        # it back in removes the only correction path responders have.
        msg = format_pinned_welcome(**self._kwargs())
        assert "Staging:" not in msg
        assert "maps.apple.com" not in msg

    def test_caltopo_url_NOT_in_welcome(self):
        # 2026-04-29 (post-PR-#324 follow-up): CalTopo URL is intentionally
        # NOT in the welcome text. It's posted as a separate follow-up
        # message so Slack unfurls the map preview card. Posting the same
        # URL twice in a channel causes Slack to dedupe and skip the unfurl
        # on the second occurrence — verified empirically against a fresh
        # channel that had never seen the URL (unfurled correctly the FIRST
        # time, did NOT unfurl when posted again after a welcome that
        # contained the URL with unfurl_links=False).
        msg = format_pinned_welcome(**self._kwargs())
        assert "caltopo.com" not in msg
        assert "CalTopo:" not in msg

    def test_remaining_pii_still_excluded(self):
        # 2026-04-29 policy: MP name IS now included (see test_mp_name_present).
        # DOB, residence, LKP coords, and the Google Doc working-notes link
        # remain excluded — they're search-management context, not field-
        # responder context. CalTopo (which the channel members can open)
        # already exposes these; the welcome does not duplicate them.
        kw = self._kwargs()
        msg = format_pinned_welcome(**kw)
        assert "DOB" not in msg
        assert "Residence" not in msg
        assert "Google Doc" not in msg
        assert "LKP" not in msg.upper()

    def test_line_count(self):
        # Pin: exactly 2 lines. event_name / MP.
        # 4 -> 3 on 2026-04-29 (CalTopo moved to its own follow-up so Slack
        # would unfurl the map preview). 3 -> 2 on 2026-08-01 (#673: staging
        # moved to its own pinned message so a wrong location can be deleted
        # and re-posted without destroying subject and contact info).
        msg = format_pinned_welcome(**self._kwargs())
        assert msg.count("\n") == 1   # 2 lines = 1 newline (officer_contact="" via _kwargs)

    def test_officer_contact_follows_the_mp_line(self):
        # Contact is line 3 and now LAST — staging left in #673.
        msg = format_pinned_welcome(
            **self._kwargs(officer_contact="Sgt. Johnson (408) 555-1234")
        )
        lines = msg.split("\n")
        assert len(lines) == 3
        assert lines[1].startswith("MP:")
        assert lines[2] == "Contact: Sgt. Johnson (408) 555-1234"

    def test_officer_contact_omitted_when_blank(self):
        for val in ("", "   "):
            msg = format_pinned_welcome(**self._kwargs(officer_contact=val))
            assert "Contact:" not in msg
            assert msg.count("\n") == 1   # still exactly 2 lines

    def test_officer_contact_not_recorded_shown_literally(self):
        # Pin the user-approved 2026-05-05 behavior: when the OCR pipeline
        # returns its "Not recorded" sentinel (officer info absent on form),
        # we render it literally rather than omitting the line. The dual
        # signal — "we have it" vs. "we looked, it wasn't there" — is more
        # useful for the IC than silent omission. Guard against future
        # "let's filter sentinel values" refactors that would lose this.
        msg = format_pinned_welcome(
            **self._kwargs(officer_contact="Not recorded")
        )
        lines = msg.split("\n")
        assert len(lines) == 3
        assert lines[2] == "Contact: Not recorded"

    def test_age_none_renders_question_mark_not_noneyo(self):
        # Issue #374: when the frontend age parser fails (or any future
        # caller passes None), render "?yo" rather than letting Python
        # f-string leak the literal "Noneyo" into the welcome. Live bug
        # surfaced 2026-05-05 with a 2-digit-year DOB ("10/7/45") that
        # JS Date couldn't parse — fix is dual: frontend parses Gemini's
        # already-computed age hint, AND backend defends against None.
        msg = format_pinned_welcome(**self._kwargs(age=None))
        assert "None" not in msg
        assert "Noneyo" not in msg
        assert "?yo" in msg

    def test_age_empty_string_also_renders_question_mark(self):
        # Cluster F (Slack-M6): the frontend sends `mp_age: ""` (empty
        # string, not null) when OCR fails to extract age. Pre-fix the
        # `age is not None` guard let "" through and produced the bare
        # literal "yo" in the welcome ("MP: Jane Doe – yo Female, ...").
        # Treat "" the same as None — render the "?yo" placeholder.
        msg = format_pinned_welcome(**self._kwargs(age=""))
        assert "?yo" in msg
        # Defensive: the literal " yo " with no leading digit must NOT
        # appear in the MP line (that's the pre-fix symptom).
        mp_line = [line for line in msg.splitlines() if line.startswith("MP:")][0]
        assert "– yo " not in mp_line


# ---------------------------------------------------------------------------
# format_staging_message() — standalone pinned staging message (issue #673)
# ---------------------------------------------------------------------------

class TestFormatStagingMessage:
    """Staging is its own pinned message so it can be deleted and re-posted.

    A dispatch that goes out with the wrong staging location had NO correction
    path: only a message's author can edit a Slack message and the bot is the
    author. A workspace admin CAN delete a bot message, so splitting staging
    out makes the wrong part independently removable without destroying the
    subject and contact info in the welcome.

    chat.update on the stored welcome_ts was considered and rejected (Bill,
    2026-08-01): it only works while something is still driving the app, and by
    the time staging is known to be wrong the dispatcher has closed Dispatch
    Turbo and is driving to staging. Same assumption that made the #612
    follow-up fail on its first real use (#455).
    """

    def _kwargs(self, **overrides):
        base = dict(
            staging_address="Cardoza Park, Milpitas",
            staging_apple_url="https://maps.apple.com/?q=Cardoza+Park",
            staging_google_url="https://www.google.com/maps/?q=Cardoza+Park",
            unverified=False, unmapped=False,
        )
        base.update(overrides)
        return base

    def test_apple_link_format(self):
        # Slack mrkdwn: <URL|Text> renders as clickable Text.
        kw = self._kwargs()
        assert f"<{kw['staging_apple_url']}|{kw['staging_address']}>" in \
            format_staging_message(**kw)

    def test_google_link_format(self):
        # Compact 'G' label keeps the line scannable on phone screens.
        kw = self._kwargs()
        assert f"(<{kw['staging_google_url']}|G>)" in format_staging_message(**kw)

    def test_is_a_single_line(self):
        # One line, one pin, one thing to delete when it is wrong.
        assert "\n" not in format_staging_message(**self._kwargs())

    def test_starts_with_the_staging_label(self):
        assert format_staging_message(**self._kwargs()).startswith("Staging:")

    def test_address_text_is_verbatim(self):
        """THE TEXT IS THE QUERY.

        Both URLs are built by the frontend from this same staging text
        (maps.apple.com/?q=<TEXT>, google.com/maps/search/<TEXT>), never from
        coordinates — Locked Decision "Staging line text IS the responders'
        maps-link query". Any transformation here silently desynchronises the
        words responders read from the place their phone routes them to.
        """
        odd = "37.33520, -121.88900 — 10S EG 12345 67890"
        assert odd in format_staging_message(**self._kwargs(staging_address=odd))

    def test_no_warning_by_default(self):
        """Default OFF. A warning on every dispatch trains responders to skip it."""
        msg = format_staging_message(**self._kwargs())
        assert "⚠️" not in msg
        assert "\n" not in msg

    def test_warning_appended_when_unverified(self):
        msg = format_staging_message(**self._kwargs(unverified=True))
        lines = msg.split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("Staging:")
        assert lines[1] == STAGING_UNVERIFIED_WARNING

    def test_warning_is_actionable_and_does_not_ask_for_adjudication(self):
        """On the 2026-08-01 Humboldt run the LKP was wrong and the officer's
        coordinate was right. A responder cannot tell which — telling them to
        confirm with Dispatch is the only actionable instruction."""
        assert "Confirm with Dispatch before rolling." in STAGING_UNVERIFIED_WARNING
        assert "one of them is wrong" not in STAGING_UNVERIFIED_WARNING.lower()

    def test_the_link_still_works_when_warned(self):
        """The warning must not disturb the maps query — responders still tap it."""
        kw = self._kwargs(unverified=True)
        msg = format_staging_message(**kw)
        assert f"<{kw['staging_apple_url']}|{kw['staging_address']}>" in msg

    def test_unmapped_warning_appended(self):
        """Gemini generated the whole list with no POI data behind it."""
        msg = format_staging_message(**self._kwargs(unmapped=True))
        lines = msg.split("\n")
        assert len(lines) == 2
        assert lines[1] == STAGING_UNMAPPED_WARNING

    def test_both_warnings_can_appear_and_conflict_comes_first(self):
        """They co-occurred live on 2026-08-01: a remote anchor (no POIs) whose
        officer coordinate ALSO disagreed by 268 mi.

        Not merged into one sentence — they call for different scepticism. A
        conflict means the REGION may be wrong; unmapped means the ADDRESS may
        not exist. Conflict first: it is the larger error.
        """
        msg = format_staging_message(**self._kwargs(unverified=True, unmapped=True))
        lines = msg.split("\n")
        assert len(lines) == 3
        assert lines[0].startswith("Staging:")
        assert lines[1] == STAGING_UNVERIFIED_WARNING
        assert lines[2] == STAGING_UNMAPPED_WARNING

    def test_unmapped_warning_is_actionable(self):
        assert "Confirm with Dispatch before rolling." in STAGING_UNMAPPED_WARNING
        assert "not checked against map data" in STAGING_UNMAPPED_WARNING

    def test_no_warnings_by_default_with_both_flags_off(self):
        msg = format_staging_message(**self._kwargs())
        assert "⚠️" not in msg and "\n" not in msg

    def test_caltopo_url_not_in_staging_message(self):
        # Slack dedupes URLs per channel; the CalTopo follow-up needs the
        # single allowed occurrence to render its map preview card.
        assert "caltopo.com" not in format_staging_message(**self._kwargs())


class TestNovelNotes:
    """Pin the #670 intake-free-text filter.

    The responder-facing at-risk list is built from CHECKBOX state only. On the
    2026-07-31 mutual-aid callout the mental-health question carried a
    description of a severe, search-strategy-altering cognitive impairment, the
    box was blank, and nobody in the field ever saw it.

    Corpus sizing (40 forms) drove the design and contradicted the issue's
    framing that this was a length problem:
      * 48 free-text snippets, median 14 chars, MAX 48  -> a cap is dead code
      * 26 of the 48 merely RESTATE the at-risk line    -> dedup is the work
    """

    def test_novel_detail_survives(self):
        """The 07-31 case: box blank, text carrying the fact that mattered."""
        out = novel_notes(
            "extremely cognitively impaired, 5-minute short term memory",
            "Alone, No phone")
        assert "5-minute short term memory" in out

    def test_pure_echo_of_the_at_risk_line_is_dropped(self):
        """A form whose at-risk reads 'Dementia' also carries
        'Q6 — reason: DEMENTIA' and 'Q9 — detail: DEMENTIA' — 26 of 48 corpus
        snippets are this shape."""
        assert novel_notes("DEMENTIA", "Dementia, Alone, No phone") == ""

    def test_mixed_keeps_only_the_novel_snippet(self):
        out = novel_notes("DEMENTIA; wanders toward creek beds", "Dementia, Alone")
        assert out == "wanders toward creek beds"

    def test_case_insensitive(self):
        assert novel_notes("dementia", "DEMENTIA") == ""

    def test_filler_words_do_not_block_echo_detection(self):
        """The 4-letter floor, asserted in the direction it actually matters.

        The drop condition is `all(word in at_risk)`, so every extra word makes
        a drop HARDER — a lower floor means FEWER drops. The floor is therefore
        what lets "the DEMENTIA" still be recognised as an echo of "Dementia";
        counting "the" would fail the match and let the echo into the welcome.

        My first version of this test asserted the opposite direction with a
        fixture that did not discriminate, and survived a floor mutation.
        """
        assert novel_notes("the DEMENTIA", "Dementia, Alone") == ""
        assert novel_notes("has dementia", "Dementia, Alone") == ""

    def test_one_unmatched_substantial_word_keeps_the_snippet(self):
        """The other direction — this is what protects genuinely novel detail."""
        assert novel_notes("dementia and wanders", "Dementia, Alone") == \
            "dementia and wanders"

    def test_empty_and_whitespace_are_no_ops(self):
        for raw in ("", "   ", ";", " ; ; "):
            assert novel_notes(raw, "Dementia") == ""

    def test_missing_at_risk_keeps_everything(self):
        assert novel_notes("wanders at night", "") == "wanders at night"

    def test_long_text_is_never_truncated(self):
        """Same rule as the at-risk segment — the text is dispatcher-curated and
        may carry the one fact that changes search strategy."""
        long = "A" * 400 + " unique detail"
        assert long in novel_notes(long, "Dementia")

    def test_no_risk_factor_is_ever_inferred(self):
        """Per Bill 2026-07-31: show the text, never infer. A wrong risk factor
        is worse than a missing one. This helper only ever REMOVES."""
        out = novel_notes("suicidal ideation noted by reporting party", "Alone")
        assert out == "suicidal ideation noted by reporting party"


class TestWelcomeNotesLine:
    def _kwargs(self, **overrides):
        base = dict(
            event_name="2026-04-25 MPD CALAVERAS",
            mp_name="John Smith",
            age=79, gender="M", at_risk="dementia",
            notes="", officer_contact="",
        )
        base.update(overrides)
        return base

    def test_notes_line_absent_when_empty(self):
        msg = format_pinned_welcome(**self._kwargs())
        assert "Notes:" not in msg
        assert msg.count("\n") == 1  # unchanged 2-line welcome

    def test_notes_line_absent_when_only_an_echo(self):
        """An all-echo notes field must not add an empty 'Notes:' row."""
        msg = format_pinned_welcome(**self._kwargs(notes="DEMENTIA"))
        assert "Notes:" not in msg

    def test_notes_sits_directly_under_the_mp_line(self):
        """Notes is risk detail and belongs beside the at-risk list it
        qualifies; Contact is logistics and stays below it."""
        msg = format_pinned_welcome(**self._kwargs(
            notes="5-minute short term memory",
            officer_contact="Sgt. Johnson (408) 555-1234"))
        lines = msg.split("\n")
        assert len(lines) == 4
        assert lines[1].startswith("MP:")
        assert lines[2] == "Notes: 5-minute short term memory"
        assert lines[3].startswith("Contact:")

    def test_notes_without_officer_contact(self):
        msg = format_pinned_welcome(**self._kwargs(notes="wanders at night"))
        lines = msg.split("\n")
        assert len(lines) == 3
        assert lines[2] == "Notes: wanders at night"

    def test_staging_still_absent(self):
        """#673 must survive #670 — staging is its own pinned message."""
        msg = format_pinned_welcome(**self._kwargs(notes="wanders at night"))
        assert "Staging:" not in msg



class TestSlackMrkdwnEscaping:
    """Intake free text is interpolated into mrkdwn (security review 2026-09-06).

    Every case below is a string an adversary can put on an intake form —
    a hoax caller, a spoofed mutual-aid PDF — and that reached responders
    verbatim in a message authored by the trusted dispatch bot. All run
    against the mirrors; production parity is pinned in the class below.
    """

    def _welcome(self, **kw):
        base = dict(event_name="2026-09-06 SJPD Test", mp_name="DOE, JANE",
                    age=30, gender="F", at_risk="Dementia")
        base.update(kw)
        return format_pinned_welcome(**base)

    def test_phishing_link_in_request_is_neutralised(self):
        """The strongest vector: Request is unescaped BY DESIGN — never
        echo-filtered, never truncated — so nothing else ever touched it."""
        out = self._welcome(request="K9 + 2 teams. <https://evil.example/login|Updated staging — tap here>")
        assert "<https://evil.example" not in out
        assert "&lt;https://evil.example/login|Updated staging — tap here&gt;" in out

    def test_channel_wide_ping_in_notes_is_neutralised(self):
        out = self._welcome(notes="<!channel> <!here> check the creek")
        assert "<!channel>" not in out and "<!here>" not in out
        assert "&lt;!channel&gt; &lt;!here&gt; check the creek" in out

    def test_mp_name_and_at_risk_are_escaped(self):
        out = self._welcome(mp_name="DOE, JANE <!here>", at_risk="Armed & <dangerous>")
        assert "MP: DOE, JANE &lt;!here&gt; – 30yo" in out
        assert "Armed &amp; &lt;dangerous&gt;" in out

    def test_last_seen_and_contact_are_escaped_after_their_sentinel_checks(self):
        """Escaping runs AFTER the `[` sentinel test, so a bracketed
        placeholder is still dropped rather than rendered as `[…]`."""
        out = self._welcome(last_seen="<!here> 14:30", officer_contact="Sgt <X> 555-0100")
        assert "Last seen: &lt;!here&gt; 14:30" in out
        assert "Contact: Sgt &lt;X&gt; 555-0100" in out
        assert "Last seen" not in self._welcome(last_seen="[date/time only from form]")

    def test_echo_filter_still_compares_raw_text(self):
        """novel_notes must see the UNESCAPED at-risk text. A note that is a
        pure echo of the at-risk line is dropped whether or not it carries
        an ampersand — the escape must not make it 'novel'."""
        out = self._welcome(at_risk="Food & water issues", notes="food water issues")
        assert "Notes:" not in out

    def test_staging_label_cannot_close_the_link_early(self):
        """A `>` in the address slot ends the mrkdwn link and the remainder
        parses as fresh markup. The two URLs are built with
        encodeURIComponent upstream and must NOT be escaped."""
        out = format_staging_message(
            staging_address="123 Main St> <https://evil.example|tap>",
            staging_apple_url="https://maps.apple.com/?q=123%20Main",
            staging_google_url="https://www.google.com/maps/search/123%20Main",
        )
        assert "Staging: <https://maps.apple.com/?q=123%20Main|123 Main St&gt; &lt;https://evil.example|tap&gt;> (<https://www.google.com/maps/search/123%20Main|G>)" in out

    def test_a_plain_address_is_byte_identical(self):
        """Content-preserving: the common case renders exactly as before, so
        the no-truncation rules on at_risk and Request are untouched."""
        out = self._welcome(request="SEARCH AND RESCUE FOR 10-65 AT RISK",
                            at_risk="Dementia, Alone, No proper equipment")
        assert "Request: SEARCH AND RESCUE FOR 10-65 AT RISK" in out
        assert "Dementia, Alone, No proper equipment" in out

    def test_ampersand_is_escaped_first(self):
        """Otherwise `<` → `&lt;` → `&amp;lt;` and Slack renders the literal."""
        assert _mrkdwn_escape("<&>") == "&lt;&amp;&gt;"


class TestSlackMrkdwnEscapingProductionParity:
    """The tests above run against mirrors; these read backend/slack.py.

    slack.py is not importable here (no slack_sdk), so without this class
    every escape could be removed from production and the suite would stay
    green — the exact failure TestWelcomeAndStagingProductionParity was
    written for. Each pin asserts a CALL SITE on comment-stripped code, and
    each was verified by reintroducing the defect it names.
    """

    @staticmethod
    def _prod():
        return (Path(__file__).parent / "slack.py").read_text(encoding="utf-8")

    @classmethod
    def _fn(cls, name):
        m = re.search(rf"^def {name}\(.*?(?=\n\n\S)", cls._prod(), re.DOTALL | re.MULTILINE)
        assert m, f"{name} not found in slack.py"
        return m.group(0)

    @staticmethod
    def _code_only(text):
        body = re.sub(r'"""(?:.|\n)*?"""', "", text)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_helper_matches_production(self):
        prod = self._code_only(self._fn("_mrkdwn_escape"))
        assert 'return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")' in prod, (
            "_mrkdwn_escape in slack.py no longer escapes &, <, > in that order"
        )
        assert prod.index('"&", "&amp;"') < prod.index('"<", "&lt;"'), (
            "ampersand is no longer escaped FIRST — every other escape double-encodes"
        )

    def test_every_external_value_is_escaped_in_the_welcome(self):
        code = self._code_only(self._fn("format_pinned_welcome"))
        for value in ("mp_name", "at_risk_clean", "event_name", "notes_clean",
                      "last_seen_clean", "request_clean", "contact_clean"):
            assert f"{value} = _mrkdwn_escape({value})" in code, (
                f"`{value}` reaches the Slack welcome unescaped — a crafted form "
                f"field can smuggle <url|label> or <!channel> into a pinned message"
            )

    def test_escapes_happen_before_their_f_strings(self):
        """Order, not presence: an escape AFTER the append is a no-op that
        still contains every string the pin above looks for."""
        code = self._code_only(self._fn("format_pinned_welcome"))
        assert code.index("mp_name = _mrkdwn_escape(mp_name)") < code.index('mp_line = f"MP: ')
        assert code.index("event_name = _mrkdwn_escape(event_name)") < code.index('f"*{event_name}*"')
        for value, tag in (("notes_clean", "Notes"), ("last_seen_clean", "Last seen"),
                           ("request_clean", "Request"), ("contact_clean", "Contact")):
            assert code.index(f"{value} = _mrkdwn_escape({value})") < code.index(f'f"{tag}: '), (
                f"`{value}` is escaped after it is rendered"
            )

    def test_echo_filter_receives_raw_at_risk(self):
        """novel_notes must run BEFORE at_risk_clean is escaped, or the echo
        comparison sees `&amp;` on one side and `&` on the other."""
        code = self._code_only(self._fn("format_pinned_welcome"))
        assert code.index("novel_notes(notes, at_risk_clean)") < code.index("at_risk_clean = _mrkdwn_escape(")

    def test_staging_label_is_escaped_and_urls_are_not(self):
        code = self._code_only(self._fn("format_staging_message"))
        assert "staging_address = _mrkdwn_escape(staging_address)" in code
        assert code.index("staging_address = _mrkdwn_escape(") < code.index('lines = [f"Staging: ')
        assert "_mrkdwn_escape(staging_apple_url)" not in code
        assert "_mrkdwn_escape(staging_google_url)" not in code, (
            "escaping a URL breaks the link responders tap — the URLs are "
            "encodeURIComponent'd upstream and are the bot's own markup"
        )


class TestWelcomeAndStagingProductionParity:
    """Tie the two mirrors above to backend/slack.py.

    THIS FILE HAD NO PRODUCTION-READING PIN AT ALL. Found the hard way on
    2026-08-01: three parameters were removed from the real
    `slack.py::format_pinned_welcome` and the entire suite — 1841 tests —
    stayed green, because every assertion here runs against the local mirror
    and nothing read the module. The Slack welcome format is a Locked
    Decision, and its own test file could not see it change.

    Scope is deliberately the two functions this PR touched. The rest of the
    mirrors in this file are still unpinned; treat them as unverified claims
    until each grows a pin like this one.

    See [[project-test-mirrors-lack-production-pins]] — same failure shape as
    _STAGING_TIER and gemini.py::type_labels, third file.
    """

    @staticmethod
    def _prod():
        return (Path(__file__).parent / "slack.py").read_text(encoding="utf-8")

    @classmethod
    def _fn(cls, name):
        src = cls._prod()
        m = re.search(
            rf"^def {name}\(.*?(?=\n\n(?:def |async def |# -{{10,}}))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, f"{name} not found in slack.py"
        return m.group(0)

    @staticmethod
    def _code_only(text):
        """Strip docstring and comments before asserting.

        The rationale for the split is written directly above the code and
        names the same identifiers, so a raw-source search finds the prose and
        passes with the implementation gone.
        """
        body = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_production_staging_line_is_identical_to_the_mirror(self):
        """The exact rendered shape, not merely 'a function exists'.

        Pins the LINE CONSTRUCTION rather than the closing statement: the
        function now appends an optional warning after building it, so an
        `endswith` on the return would break on every future addition without
        telling us anything about the shape responders actually tap.
        """
        prod = self._code_only(self._fn("format_staging_message"))
        assert (
            'lines = [f"Staging: <{staging_apple_url}|{staging_address}> '
            '(<{staging_google_url}|G>)"]'
        ) in prod, (
            "slack.py::format_staging_message no longer renders the shape this "
            "file mirrors. Responders tap this link — the text IS the maps "
            "query."
        )

    def test_production_warns_only_when_unverified(self):
        """Structure: the warning must be gated, and appended AFTER the link.

        An ungated append would put a location-conflict warning on every
        dispatch, which trains responders to ignore it — the failure mode that
        makes a warning worse than none.
        """
        prod = self._code_only(self._fn("format_staging_message"))
        for gate, const in (("if unverified:", "STAGING_UNVERIFIED_WARNING"),
                            ("if unmapped:", "STAGING_UNMAPPED_WARNING")):
            assert gate in prod, (
                f"The staging warning is no longer gated on `{gate}` — it would "
                f"fire on every dispatch."
            )
            assert const in prod, (
                f"{const} is no longer used; the wording is agreed copy, not "
                f"ad-hoc text."
            )
            assert prod.index('lines = [f"Staging:') < prod.index(gate), (
                "The warning is applied before the staging link is built."
            )
        assert prod.index("if unverified:") < prod.index("if unmapped:"), (
            "Location conflict must come first — it is the larger error "
            "(wrong REGION vs an address that may not exist)."
        )

    def test_production_warning_wording_is_actionable(self):
        """Bill, 2026-08-01: tell them what to DO.

        "One of them is wrong" asks a responder to adjudicate a conflict they
        cannot see. The agreed instruction is to confirm before rolling.
        """
        src = self._prod()
        assert "Confirm with Dispatch before rolling." in src, (
            "The staging warning no longer carries the agreed actionable "
            "instruction."
        )

    def test_production_welcome_no_longer_builds_a_staging_line(self):
        """#673's actual invariant, asserted on code with comments stripped."""
        code = self._code_only(self._fn("format_pinned_welcome"))
        assert "Staging:" not in code, (
            "slack.py::format_pinned_welcome builds a staging line again. "
            "Folding staging back into the welcome removes the only path a "
            "human has to correct a wrong staging location — the bot authored "
            "the message, so nobody but the bot can edit it."
        )

    def test_production_welcome_does_not_accept_staging_arguments(self):
        """Signature parity — a leftover parameter means the mirror lies."""
        sig = self._fn("format_pinned_welcome").split(") -> str:")[0]
        for gone in ("staging_address", "staging_apple_url", "staging_google_url"):
            assert gone not in sig, (
                f"slack.py::format_pinned_welcome still takes {gone}, but the "
                f"mirror in this file does not. They have diverged."
            )

    def test_production_welcome_still_takes_what_the_mirror_takes(self):
        """The other direction — the mirror must not be missing a real param."""
        sig = self._fn("format_pinned_welcome").split(") -> str:")[0]
        for required in ("event_name", "mp_name", "age", "gender", "at_risk",
                         "notes", "officer_contact"):
            assert required in sig, (
                f"slack.py::format_pinned_welcome no longer takes {required}; "
                f"the mirror in this file still does."
            )

    def test_production_welcome_filters_notes_before_rendering(self):
        """#670 — assert the CALL, on code with comments stripped.

        `novel_notes` is named by its own def line and throughout the docstring
        that explains it; neither proves the filter still runs on the way into
        the welcome. Rendering `notes` raw would echo the at-risk line one row
        above on 26 of every 48 corpus snippets.
        """
        code = self._code_only(self._fn("format_pinned_welcome"))
        assert "novel_notes(notes, at_risk_clean)" in code, (
            "The welcome renders `notes` without filtering it through "
            "novel_notes() — pure echoes of the at-risk line come back."
        )
        assert code.index("novel_notes(notes") < code.index('f"Notes: '), (
            "The filter runs after the line is built, so it cannot affect it."
        )

    def test_production_novel_notes_never_truncates(self):
        """Same rule as the at-risk segment (its own Locked Decision).

        Measured max is 48 chars so a cap would be dead code — but a future
        'scannability' pass is exactly how the at-risk cap got introduced, and
        that one cut a real suicide-attempt indicator off the tail in 2026-05.
        """
        code = self._code_only(self._fn("novel_notes"))
        assert "[:" not in code, (
            "novel_notes now slices its input — a length cap on dispatcher-"
            "curated safety text can silently hide the one fact that changes "
            "search strategy."
        )

    def test_production_novel_notes_only_removes(self):
        """It must never invent a risk factor (Bill, 2026-07-31).

        Every kept snippet has to be a verbatim member of the input, so the
        helper cannot rewrite, classify, or append.
        """
        code = self._code_only(self._fn("novel_notes"))
        assert "kept.append(snippet)" in code, (
            "novel_notes no longer appends the ORIGINAL snippet — it may be "
            "transforming the dispatcher's text rather than just filtering it."
        )


# ---------------------------------------------------------------------------
# format_would_invite_message() — shadow-mode message
# ---------------------------------------------------------------------------

class TestFormatWouldInvite:
    # PR-fix-3 (2026-05-08): names were stored as 'Last, First' for sort but
    # rendered as 'First Last' for display, with ';' separators for unambiguous
    # multi-name lines. Old assertions ("Burns, Black") replaced with new
    # conversational form ("Bill Burns; Kris Black"). Single-name inputs
    # (no comma) pass through unchanged.

    def test_with_resolvable_only(self):
        msg = format_would_invite_message(
            already_member_names=[],
            resolvable_names=["Burns", "Black"],   # bare last names — no comma → pass-through
            unresolvable_names=[],
        )
        assert "Would invite: Burns; Black" in msg
        assert "Cannot invite" not in msg
        assert "added" not in msg

    def test_with_both_resolvable_and_unresolvable(self):
        msg = format_would_invite_message(
            already_member_names=[],
            resolvable_names=["Burns"],
            unresolvable_names=["Oliver"],
        )
        assert "Would invite: Burns" in msg
        assert "Cannot invite (no SAR email in Everbridge): Oliver" in msg

    def test_with_unresolvable_only(self):
        # Edge: a cycle where every responder lacks a matching Slack account.
        # Shadow mode still surfaces the 'cannot invite' line so the
        # confidence-builders see the issue.
        msg = format_would_invite_message(
            already_member_names=[],
            resolvable_names=[],
            unresolvable_names=["Oliver", "Vera"],
        )
        assert "Cannot invite (no SAR email in Everbridge): Oliver; Vera" in msg
        assert "Would invite" not in msg

    def test_empty_returns_empty_string(self):
        # No responders this cycle (idle) → empty message; caller decides
        # whether to post anything.
        msg = format_would_invite_message(
            already_member_names=[], resolvable_names=[], unresolvable_names=[],
        )
        assert msg == ""

    def test_already_member_only_uses_added_suffix(self):
        # Safe-list pre-invitee replied YES — they're already a channel
        # member, so the line is the full-mode-style timeline marker.
        # PR-fix-3: 'Burns, Bill' → 'Bill Burns added' (conversational order).
        msg = format_would_invite_message(
            already_member_names=["Burns, Bill"],
            resolvable_names=[],
            unresolvable_names=[],
        )
        assert msg == "Bill Burns added"
        assert "Would invite" not in msg

    def test_already_member_with_other_buckets(self):
        # Mixed cycle: pre-invitee replies alongside a non-safe-list
        # responder. Expect both line types — both in conversational order.
        msg = format_would_invite_message(
            already_member_names=["Burns, Bill"],
            resolvable_names=["Mateos, Miguel"],
            unresolvable_names=[],
        )
        assert "Bill Burns added" in msg
        assert "Would invite: Miguel Mateos" in msg
        # Order: already-member lines come first (matches full-mode "added"
        # wording); would-invite/cannot-invite come after.
        assert msg.index("added") < msg.index("Would invite")

    def test_multiple_already_members_each_on_own_line(self):
        # Two safe-list pre-invitees in one cycle → one "added" line each,
        # matching full-mode's per-arrival pattern. PR-fix-3: conversational
        # order on each line.
        msg = format_would_invite_message(
            already_member_names=["Burns, Bill", "Black, Kris"],
            resolvable_names=[],
            unresolvable_names=[],
        )
        assert msg == "Bill Burns added\nKris Black added"

    def test_no_would_invite_for_safe_list_member(self):
        # Regression pin (2026-04-30 live test bug): a safe-list responder
        # MUST NOT appear in the 'Would invite:' line — they're already in
        # the channel from the send-time pre-invite. Misleading otherwise.
        msg = format_would_invite_message(
            already_member_names=["Burns, Bill"],
            resolvable_names=[],
            unresolvable_names=[],
        )
        # Conversational form must also not appear in Would invite.
        assert "Would invite: Bill Burns" not in msg
        assert "Would invite" not in msg

    def test_multi_word_last_name_handled_correctly(self):
        # PR-fix-3 (2026-05-08): EB ack `last_name="Cadena Resendez"`,
        # `first_name="Priscilla"` produces 'Cadena Resendez, Priscilla' from
        # _shape_responder_name. Conversational form must split on the FIRST
        # ', ' only — result is 'Priscilla Cadena Resendez', NOT
        # 'Priscilla Cadena, Resendez' (which would happen with a sloppy
        # split on every comma).
        msg = format_would_invite_message(
            already_member_names=[],
            resolvable_names=["Cadena Resendez, Priscilla"],
            unresolvable_names=[],
        )
        assert "Would invite: Priscilla Cadena Resendez" in msg
        assert "Priscilla Cadena, Resendez" not in msg

    def test_semicolon_separator_for_disambiguation(self):
        # The whole point of PR-fix-3: comma-joined "Last, First" entries are
        # ambiguous (e.g. "Burns, Bill, Black, Kris" reads as 4 first-names
        # OR 2 people). Semicolon separation in conversational form is
        # unambiguous regardless of name structure.
        msg = format_would_invite_message(
            already_member_names=[],
            resolvable_names=["Burns, Bill", "Black, Kris", "Cubeiro, Janae"],
            unresolvable_names=[],
        )
        assert "Would invite: Bill Burns; Kris Black; Janae Cubeiro" in msg
        # Old format must NOT appear:
        assert ", Bill, " not in msg
        assert "Burns, Bill" not in msg

    def test_unresolvable_label_names_the_email_gap_not_slack(self):
        # 2026-07-19: this line read "Cannot invite (no matching Slack
        # account)", false on two counts — shadow mode performs NO Slack
        # lookup, and the real condition is a missing EB email. It misfired on
        # a responder who had both a Slack account and an @sccssar.org address
        # in Everbridge. The label must never claim a Slack account was checked.
        msg = format_would_invite_message(
            already_member_names=[], resolvable_names=[],
            unresolvable_names=["Cost, Sonja"],
        )
        assert "Cannot invite (no SAR email in Everbridge): Sonja Cost" in msg
        assert "Slack account" not in msg

    def test_unresolvable_wording_matches_full_mode(self):
        # Shadow and full mode must describe the same condition the same way —
        # a channel that ran in shadow then flipped to full would otherwise
        # show two different phrasings for one situation in a single timeline
        # (same class as the Cluster F / Slack-M9 name-format fix).
        shadow = format_would_invite_message(
            already_member_names=[], resolvable_names=[],
            unresolvable_names=["Cost, Sonja"],
        )
        full = format_invite_failed_message(
            name="Cost, Sonja", reason=INVITE_FAIL_NO_EB_EMAIL,
        )
        assert "no SAR email in Everbridge" in shadow
        assert "no SAR email in Everbridge" in full


# ---------------------------------------------------------------------------
# format_invite_failed_message() — full-mode counterpart to '<Name> added'
# ---------------------------------------------------------------------------
# Regression boundary for the 2026-07-19 false-positive: main.py posted
# '<Name> added' unconditionally, so a responder who was never invited still
# read as added in the incident channel. Each reason maps to a different
# dispatcher remedy, so the wording must stay distinguishable.

class TestFormatInviteFailedMessage:
    def test_no_eb_email_names_the_everbridge_remedy(self):
        msg = format_invite_failed_message(
            name="Cost, Sonja", reason=INVITE_FAIL_NO_EB_EMAIL,
        )
        assert msg == (
            "⚠️ Sonja Cost replied YES but has no SAR email in Everbridge "
            "— add to channel manually"
        )

    def test_no_slack_user_names_the_workspace_remedy(self):
        msg = format_invite_failed_message(
            name="Cost, Sonja", reason=INVITE_FAIL_NO_SLACK_USER,
        )
        assert "has no Slack account" in msg
        # Must NOT blame Everbridge — the EB record is fine in this case.
        assert "Everbridge" not in msg

    def test_error_reason_is_generic(self):
        msg = format_invite_failed_message(
            name="Cost, Sonja", reason=INVITE_FAIL_ERROR,
        )
        assert "could not be added automatically" in msg

    def test_unknown_reason_falls_back_not_raises(self):
        # A poll cycle must never die in a message-formatting branch.
        msg = format_invite_failed_message(name="Cost, Sonja", reason="bogus")
        assert "could not be added automatically" in msg

    def test_name_is_conversational_not_sortable(self):
        # Same display rule as the '<Name> added' line (Cluster F / Slack-M9).
        msg = format_invite_failed_message(
            name="Cost, Sonja", reason=INVITE_FAIL_NO_EB_EMAIL,
        )
        assert "Sonja Cost" in msg
        assert "Cost, Sonja" not in msg

    def test_never_leaks_an_email_address(self):
        # Incident channels carry PHI — the address adds nothing the
        # dispatcher cannot see in Everbridge.
        for reason in (INVITE_FAIL_NO_EB_EMAIL, INVITE_FAIL_NO_SLACK_USER,
                       INVITE_FAIL_ERROR):
            assert "@" not in format_invite_failed_message(
                name="Cost, Sonja", reason=reason,
            )

    def test_all_reasons_are_actionable_and_distinguishable(self):
        msgs = {
            r: format_invite_failed_message(name="Cost, Sonja", reason=r)
            for r in (INVITE_FAIL_NO_EB_EMAIL, INVITE_FAIL_NO_SLACK_USER,
                      INVITE_FAIL_ERROR)
        }
        # Distinguishable — a dispatcher must be able to tell the remedies apart.
        assert len(set(msgs.values())) == 3
        for m in msgs.values():
            assert m.startswith("⚠️ ")
            assert "add to channel manually" in m
            assert "replied YES" in m

    def test_is_not_the_added_line(self):
        # The whole point: this must never read as a positive confirmation.
        msg = format_invite_failed_message(
            name="Cost, Sonja", reason=INVITE_FAIL_NO_EB_EMAIL,
        )
        assert not msg.endswith(" added")


# ---------------------------------------------------------------------------
# format_groups_requested() — Item 7 channel-creation message
# ---------------------------------------------------------------------------

class TestFormatGroupsRequested:
    def test_basic(self):
        # Item 7 — wording is "Groups requested by dispatcher" (NOT "Would
        # invite groups") because Slack is people-only.
        msg = format_groups_requested(["K9", "UAS", "Drivers"])
        assert msg == "Groups requested by dispatcher: K9, UAS, Drivers"

    def test_does_not_say_would_invite_groups(self):
        # Pin against accidental wording regression — "Would invite groups"
        # implies Slack is the source of truth for group membership, which
        # it isn't.
        msg = format_groups_requested(["K9"])
        assert "Would invite groups" not in msg


# ---------------------------------------------------------------------------
# format_off_call_excluded() — #active-incidents line for members NOT paged
# ---------------------------------------------------------------------------

class TestFormatOffCallExcluded:
    """Names arrive 'First Last' from the Everbridge contact record (not D4H's
    'Last, First'), so ', ' is unambiguous here — unlike the '; '-joined
    responder lines."""

    NAMES = ["Damian Romard", "Kris Black"]
    EXPECTED = "🚫 Unavailable in D4H (not paged): Damian Romard, Kris Black"

    def test_format(self):
        assert format_off_call_excluded(self.NAMES) == self.EXPECTED

    def test_production_renders_the_same_line(self):
        """Exec the real function out of slack.py (pure string work, no
        slack_sdk dependency) and run the SAME fixture through it, so a
        wording change in production fails here and not only in the mirror."""
        src = (Path(__file__).parent / "slack.py").read_text(encoding="utf-8")
        node = next(n for n in ast.parse(src).body
                    if isinstance(n, ast.FunctionDef) and n.name == "format_off_call_excluded")
        ns: dict = {}
        exec(ast.get_source_segment(src, node), ns)
        prod = ns["format_off_call_excluded"]
        assert prod(self.NAMES) == self.EXPECTED
        assert prod(["Kris Black"]) == format_off_call_excluded(["Kris Black"])


# ---------------------------------------------------------------------------
# format_tally_responder_line() and format_tally_multi_team_line() (Item 6)
# ---------------------------------------------------------------------------

class TestFormatTallyLines:
    # PR-fix-3 (2026-05-08): tally lines flipped to '; '-joined conversational
    # names. The old comma form (`Burns, Black, Cubeiro`) was ambiguous when
    # any input was actually 'Last, First' — it read as 6 first-names instead
    # of 3 people. Semicolons disambiguate; conversational order matches what
    # dispatchers and search managers say in conversation.

    def test_responder_line_basic(self):
        # Single-token names (no comma) pass through; separator becomes ';'.
        msg = format_tally_responder_line("K9", ["Burns", "Black", "Cubeiro"])
        assert msg == "• K9 (3): Burns; Black; Cubeiro"

    def test_responder_line_with_last_first_input(self):
        # Realistic input from _shape_responder_name (Last, First) → display
        # in conversational order, semicolon-separated. The exact pattern
        # Bill called out in the 2026-05-07 SJSU tally screenshot.
        msg = format_tally_responder_line(
            "Canine Team",
            ["Chamberlin, Mark", "Chuck, Edward", "Davidson, James"],
        )
        assert msg == "• Canine Team (3): Mark Chamberlin; Edward Chuck; James Davidson"

    def test_responder_line_with_multi_word_last_name(self):
        # Multi-word last names (e.g. 'Cadena Resendez, Priscilla') keep
        # all the words of the last name together, not just the last token.
        msg = format_tally_responder_line(
            "Search Management",
            ["Cadena Resendez, Priscilla"],
        )
        assert msg == "• Search Management (1): Priscilla Cadena Resendez"

    def test_responder_line_zero_responders(self):
        # Defensive — caller should typically not call with empty names,
        # but format must still render correctly.
        msg = format_tally_responder_line("K9", [])
        assert msg == "• K9 (0): "

    def test_multi_team_line_uses_arrow_indent_not_diamond(self):
        # Item 6: the PoC used 🔸 (small orange diamond). Slack renders that
        # emoji larger than expected, dominating the tally on big incidents.
        # Replaced with two-space indent + ↳ which Slack renders small and
        # visually subordinate. Pin both halves of the rule.
        # PR-fix-3: name converts to conversational form; group list keeps
        # comma separation (group names are single tokens like "K9").
        msg = format_tally_multi_team_line("Burns, Bill", ["K9", "Drivers"])
        assert msg == "  ↳ Bill Burns: K9, Drivers"
        assert "🔸" not in msg

    def test_multi_team_line_three_groups(self):
        msg = format_tally_multi_team_line("Black, Kris", ["K9", "UAS", "Drivers"])
        assert msg == "  ↳ Kris Black: K9, UAS, Drivers"

    def test_multi_team_line_single_token_name_passthrough(self):
        # Defensive — when only one name is known (last OR first), pass through
        # without comma rearrangement.
        msg = format_tally_multi_team_line("Burns", ["K9"])
        assert msg == "  ↳ Burns: K9"


# ---------------------------------------------------------------------------
# _should_swallow_invite_error() — invite_user idempotency policy
#
# Live test 2026-04-27 surfaced cant_invite_self when the bot enumerated
# #active-incidents members (where the bot is a member to post tally
# updates) and tried to invite itself to a newly-created incident channel
# that it had just created (auto-member as creator). Same end state as
# already_in_channel — desired outcome reached, swallow.
# ---------------------------------------------------------------------------

class TestShouldSwallowInviteError:
    def test_already_in_channel_swallowed(self):
        assert _should_swallow_invite_error("already_in_channel") is True

    def test_cant_invite_self_swallowed(self):
        # Regression — the live failure shape from 2026-04-27.
        assert _should_swallow_invite_error("cant_invite_self") is True

    def test_unknown_error_not_swallowed(self):
        # Real Slack errors must propagate so the orchestration sees them.
        # Examples: "user_not_found", "channel_not_found",
        # "missing_scope" (token doesn't have groups:write), "invalid_auth".
        assert _should_swallow_invite_error("user_not_found") is False
        assert _should_swallow_invite_error("channel_not_found") is False
        assert _should_swallow_invite_error("missing_scope") is False

    def test_none_not_swallowed(self):
        # Defensive — Slack response with no `error` field shouldn't
        # silently swallow.
        assert _should_swallow_invite_error(None) is False

    def test_empty_string_not_swallowed(self):
        assert _should_swallow_invite_error("") is False

    def test_swallow_set_does_not_grow_silently(self):
        # Pin the exact membership of the swallow set so a future PR that
        # adds a new error code has to update both source and tests
        # (caught at PR review time via the test diff).
        assert _SWALLOWED_INVITE_ERRORS == frozenset({
            "already_in_channel",
            "cant_invite_self",
        })


# ---------------------------------------------------------------------------
# format_incident_dm_text() — VIP-breakthrough responder DM
# ---------------------------------------------------------------------------
# PRD: SAR Slack Onboarding/PRD_DispatchTurbo_Responder_DM_VIP_Breakthrough.
# Wiring into /send-notification + /poll-incident lands in a follow-up PR.
# This suite pins the pure-text contract only.
# ---------------------------------------------------------------------------

class TestFormatIncidentDmText:
    def test_basic_shape_no_test_label(self):
        # Production shape: no test-label → output starts with the incident
        # sentence directly. This is what a real responder sees on their
        # phone during a live callout.
        msg = format_incident_dm_text(channel_id="C01ABC234DE")
        assert msg == (
            "You've been added to incident <#C01ABC234DE>. "
            "Tap to open and check in for further instructions."
        )

    def test_channel_id_rendered_as_slack_mrkdwn_link(self):
        # <#CHANNEL_ID> is Slack's channel deep-link mrkdwn — auto-renders
        # as #channel-name and opens the channel on tap. Empirically
        # confirmed 2026-07-10 spike: real channel IDs from
        # find_or_create_private_channel() render as #channel-name; unknown
        # IDs fall back to "🔒 private channel" (not our concern here —
        # caller passes IDs from the actual channel just created).
        msg = format_incident_dm_text(channel_id="C99XYZ")
        assert "<#C99XYZ>" in msg

    def test_test_label_appears_as_first_line(self):
        # LOAD-BEARING: locked-screen notification previews truncate. The
        # test-label MUST survive truncation, so it's the FIRST line. If a
        # future PR moves the label to a suffix or embeds it mid-message,
        # phone previews would show the incident sentence with no test
        # marker at all — a real responder could mobilize on a test
        # dispatch. See
        # feedback-test-messages-to-humans-need-explicit-test-label.
        label = "[TEST from Bill Burns — Dispatch Turbo Slack testing]"
        msg = format_incident_dm_text(channel_id="C123ABC", test_label=label)
        assert msg.startswith(label)
        # And the incident text follows AFTER the label
        assert msg.index(label) < msg.index("You've been added")

    def test_test_label_blank_line_separator(self):
        # Slack renders \n\n as a blank line between paragraphs. A single
        # \n would run the label and incident text together visually and
        # weaken the marker's readability under stress.
        label = "[TEST from Bill Burns — Dispatch Turbo Slack testing]"
        msg = format_incident_dm_text(channel_id="C123ABC", test_label=label)
        # Exactly one blank line between the label and the body
        assert f"{label}\n\nYou've been added" in msg

    def test_empty_test_label_treated_as_no_label(self):
        # Explicit "": no label prefix. Same output as the default arg.
        with_empty = format_incident_dm_text(channel_id="C123ABC", test_label="")
        without = format_incident_dm_text(channel_id="C123ABC")
        assert with_empty == without

    def test_whitespace_only_test_label_treated_as_no_label(self):
        # Safety belt: env-var typo (" " or "\n") shouldn't render a blank
        # label line. Whitespace-only labels are stripped to empty and
        # treated as no-label. Rationale: caller in main.py will read
        # DISPATCH_TURBO_TEST_LABEL from os.environ — env values sometimes
        # arrive with trailing whitespace, and a blank-looking DM prefix
        # would confuse recipients.
        with_ws = format_incident_dm_text(channel_id="C123ABC", test_label="   ")
        without = format_incident_dm_text(channel_id="C123ABC")
        assert with_ws == without

    def test_test_label_leading_trailing_whitespace_stripped(self):
        # Env vars sometimes get an accidental trailing newline. Strip so
        # the DM text doesn't have a leaking blank line before the label
        # or an extra newline between label and body.
        msg = format_incident_dm_text(
            channel_id="C123ABC",
            test_label="  [TEST]  \n",
        )
        assert msg.startswith("[TEST]")
        assert "[TEST]\n\nYou've been added" in msg

    def test_empty_channel_id_raises(self):
        # Caller bug — silent broken deep link (<#>) is worse than a loud
        # ValueError at DM composition time. Consistent with the
        # incident_channel_name_with_collision_suffix pattern (raise on
        # obvious caller error, don't emit garbage).
        with pytest.raises(ValueError, match="channel_id"):
            format_incident_dm_text(channel_id="")

    def test_whitespace_only_channel_id_raises(self):
        with pytest.raises(ValueError, match="channel_id"):
            format_incident_dm_text(channel_id="   ")

    def test_channel_id_appears_only_inside_mrkdwn_wrapper(self):
        # Sanity: the channel ID should only appear inside the <#...>
        # deep-link, never bare in the text. If a future PR adds a
        # "see channel C123ABC" fallback in plain text, it would leak
        # the raw ID and defeat the deep-link tap behavior.
        msg = format_incident_dm_text(channel_id="C99UNIQUE")
        assert msg.count("C99UNIQUE") == 1
        assert "<#C99UNIQUE>" in msg

    def test_output_is_plain_string_not_block_kit(self):
        # PRD out-of-scope: "Rich Block Kit DM (e.g., a 'Join channel'
        # button, staging/subject summary in the DM)." Return type MUST be
        # a plain str, not a dict/list of blocks. Pinned to prevent a
        # future "add richer formatting" PR from silently changing the
        # return type and breaking main.py callers that expect str.
        msg = format_incident_dm_text(channel_id="C123ABC")
        assert isinstance(msg, str)


class TestWelcomeRequestLine:
    """The intake form's own `Request:` box, surfaced to responders.

    Sized 2026-08-01 by reading the `request` AcroForm field straight out of
    the 20 v2 fillable PDFs in experiments/test_forms — NOT from the cached
    OCR corpus, whose outputs predate the field and would have reported a
    confident zero. 14 of 20 populated; min 2 / median 16.5 / max 482 chars.

    Seven of the 14 name a dog resource (K9, canine, cadaver). "CADAVER DOGS"
    tells responders this is a recovery, not a rescue — and reached nobody
    before this line existed.
    """

    def _kwargs(self, **overrides):
        base = dict(
            event_name="2026-04-25 MPD CALAVERAS",
            mp_name="John Smith",
            age=79, gender="M", at_risk="dementia",
            notes="", request="", officer_contact="",
        )
        base.update(overrides)
        return base

    def test_absent_when_empty(self):
        msg = format_pinned_welcome(**self._kwargs())
        assert "Request:" not in msg
        assert msg.count("\n") == 1  # unchanged 2-line welcome

    def test_absent_when_the_not_recorded_sentinel(self):
        """pdf_extract.py renders a blank Request box as this literal, and 6
        of 20 corpus forms leave it blank. Surfacing the sentinel would put
        'Request: [not recorded]' in front of every responder on 30% of PDF
        dispatches."""
        for sentinel in ("[not recorded]", "[NOT RECORDED]", "  [Not Recorded]  "):
            msg = format_pinned_welcome(**self._kwargs(request=sentinel))
            assert "Request:" not in msg, sentinel

    def test_sits_below_notes_and_above_contact(self):
        """subject → tasking → logistics."""
        msg = format_pinned_welcome(**self._kwargs(
            notes="5-minute short term memory",
            request="CADAVER DOGS",
            officer_contact="Sgt. Johnson (408) 555-1234"))
        lines = msg.split("\n")
        assert len(lines) == 5
        assert lines[1].startswith("MP:")
        assert lines[2] == "Notes: 5-minute short term memory"
        assert lines[3] == "Request: CADAVER DOGS"
        assert lines[4].startswith("Contact:")

    def test_renders_without_notes_or_contact(self):
        msg = format_pinned_welcome(**self._kwargs(request="K9"))
        lines = msg.split("\n")
        assert len(lines) == 3
        assert lines[2] == "Request: K9"

    def test_long_request_passes_through_verbatim(self):
        """The 482-char corpus entry, which is the one that mattered most: it
        carried a located vehicle and a possible sighting — coordinates found
        NOWHERE else on the form — and it carried them at the END, exactly
        where a tail-truncation would have cut. Same rule as at-risk."""
        long_req = (
            "Four family members went hiking at approximately 3pm and never "
            "returned. The Sheriff's Office, in collaboration with the County "
            "Parks team, determined that the family's vehicle was located at: "
            "37.33669, -121.71486. Possible sighting at 37.32406, -121.69897."
        )
        msg = format_pinned_welcome(**self._kwargs(request=long_req))
        assert f"Request: {long_req}" in msg
        assert "…" not in msg and "..." not in msg
        assert "37.32406, -121.69897." in msg, (
            "the tail coordinates were lost — a length cap was reintroduced"
        )

    def test_is_not_echo_filtered_against_at_risk(self):
        """Deliberately NOT run through novel_notes(). Exactly one of the 14
        populated corpus values is contentless, so an echo filter would fire
        once in fourteen and buy nothing — while risking the loss of a
        resource ask that happens to reuse a risk word."""
        msg = format_pinned_welcome(**self._kwargs(
            at_risk="dementia", request="DEMENTIA"))
        assert "Request: DEMENTIA" in msg

    def test_staging_still_absent(self):
        """#673 must survive this change too."""
        msg = format_pinned_welcome(**self._kwargs(request="K9"))
        assert "Staging:" not in msg


class TestWelcomeWearingLine:
    """#845 — the subject's clothing on the pinned welcome.

    Extracted since v1, reaching the D4H *Description* only by accident, and
    structurally unable to reach the surface responders read: format_pinned_welcome
    had no parameter for it. On the 2026-09-09 SJPD callout — an at-risk elderly
    subject with dementia and no English — the dispatcher pasted the clothing
    description into the channel by hand.
    """

    _BASE = dict(event_name="2026-09-09 SJPD ALLENWOOD", mp_name="John Doe",
                 age=78, gender="M", at_risk="dementia, alone")

    def _lines(self, **kw):
        return format_pinned_welcome(**{**self._BASE, **kw}).split("\n")

    def test_wearing_renders_directly_below_the_mp_line(self):
        lines = self._lines(wearing="blue windbreaker, tan slacks, white sneakers")
        assert lines[1].startswith("MP: ")
        assert lines[2] == "Wearing: blue windbreaker, tan slacks, white sneakers"

    def test_wearing_precedes_notes_when_both_present(self):
        lines = self._lines(wearing="red jacket", notes="carries an oxygen tank")
        assert lines[2] == "Wearing: red jacket"
        assert lines[3] == "Notes: carries an oxygen tank"

    def test_wearing_renders_verbatim(self):
        """Officer shorthand is not tidied, expanded or re-cased — this is the
        description responders call out against."""
        raw = "BLU JKT/blk pants, NO shoes"
        assert f"Wearing: {raw}" in self._lines(wearing=raw)

    def test_long_value_is_not_truncated(self):
        """Same rule as at-risk: a tail-truncation would drop the distinctive
        detail, which is commonly the last item written."""
        raw = ("dark green parka with orange lining, grey sweatpants, black "
               "orthopedic shoes, red knit cap with a white pom, and a silver "
               "medical alert bracelet on the left wrist")
        assert f"Wearing: {raw}" in self._lines(wearing=raw)

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_adds_no_row(self, value):
        assert not any(l.startswith("Wearing:") for l in self._lines(wearing=value))

    def test_omitted_parameter_adds_no_row(self):
        assert not any(l.startswith("Wearing:") for l in self._lines())

    @pytest.mark.parametrize("value", [
        "Not recorded", "not recorded", "NOT RECORDED", "  Not Recorded  ",
        "Unknown", "unknown", "N/A", "n/a",
    ])
    def test_unbracketed_not_recorded_sentinel_is_dropped(self, value):
        """THE trap in this feature. Last Seen At renders a blank as the
        BRACKETED "[not recorded]", so slack.py's last_seen guard tests only for
        a leading "[". Last Seen Wearing renders a blank UNBRACKETED on BOTH
        intake paths — pdf_extract's `_f("mp_wearing") or "Not recorded"` and
        gemini.py's `else "Not recorded"`. A guard copied from the last_seen
        shape would publish "Wearing: Not recorded" to every responder on every
        blank-clothing dispatch."""
        assert not any(l.startswith("Wearing:") for l in self._lines(wearing=value))

    @pytest.mark.parametrize("value", [
        "[not recorded]",
        "[if present on form, else \"Not recorded\"]",
        "[describe clothing]",
    ])
    def test_bracketed_gemini_template_leak_is_dropped(self, value):
        """The bracket test is still required ON TOP of the literal test: the
        JPEG path echoes Gemini's own instruction text when it extracts nothing.
        That leak reached responders once already, on #755's welcome."""
        assert not any(l.startswith("Wearing:") for l in self._lines(wearing=value))

    def test_sentinel_match_is_exact_equality_not_a_prefix(self):
        """Corpus-discovered near-miss: "UNKNOWN, WHITE, 5\'2, 110 LBS" begins
        with a sentinel word but is real physical description. A
        `startswith("unknown")` rewrite would drop it."""
        raw = "UNKNOWN, WHITE, 5'2, 110 LBS"
        assert f"Wearing: {raw}" in self._lines(wearing=raw)

    def test_a_real_value_containing_brackets_mid_string_survives(self):
        """Only a LEADING bracket is a sentinel — an officer's parenthetical is
        data. Guards against a naive `"[" in value` rewrite."""
        raw = "blue jacket [dark], jeans"
        assert f"Wearing: {raw}" in self._lines(wearing=raw)

    def test_wearing_is_mrkdwn_escaped(self):
        """Clothing is free text and can carry & < > — unescaped, Slack renders
        it as broken markup."""
        assert "Wearing: T-shirt &amp; shorts" in self._lines(wearing="T-shirt & shorts")

    def test_wearing_does_not_disturb_the_other_optional_rows(self):
        lines = self._lines(wearing="red jacket", notes="oxygen tank",
                            last_seen="2026-09-09 14:20", request="K9",
                            officer_contact="Ofc. Lee; 408-555-0100")
        assert [l.split(":")[0] for l in lines[2:]] == [
            "Wearing", "Notes", "Last seen", "Request", "Contact"
        ]


class TestWelcomeRequestProductionParity:
    """Tie the mirror above to backend/slack.py.

    This file mirrors production because slack_sdk is not installed, and it
    carried NO production-reading pin at all until 2026-08-01 — three params
    were deleted from the real function and all 1841 tests stayed green.
    """

    @staticmethod
    def _prod():
        return (Path(__file__).parent / "slack.py").read_text(encoding="utf-8")

    @classmethod
    def _fn(cls, name):
        m = re.search(
            rf"^def {name}\(.*?(?=\n\n(?:def |async def |# -{{10,}}))",
            cls._prod(), re.DOTALL | re.MULTILINE,
        )
        assert m, f"{name} not found in slack.py"
        return m.group(0)

    @staticmethod
    def _code_only(text):
        """Strip docstring and comments. The measurement justifying the
        no-cap/no-filter choices is written in the docstring and names the
        same identifiers, so a raw-source search passes with the code gone."""
        body = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_production_accepts_a_request_parameter(self):
        sig = self._fn("format_pinned_welcome").split(") -> str:")[0]
        assert re.search(r"^\s*request: str = \"\",\s*$", sig, re.M), (
            "format_pinned_welcome no longer takes `request` — the welcome "
            "silently stopped carrying the agency's resource ask."
        )

    def test_production_renders_the_request_line(self):
        prod = self._code_only(self._fn("format_pinned_welcome"))
        assert 'lines.append(f"Request: {request_clean}")' in prod, (
            "the Request line is gone from the pinned welcome"
        )

    def test_production_drops_the_not_recorded_sentinel(self):
        prod = self._code_only(self._fn("format_pinned_welcome"))
        assert 'request_clean.lower() != "[not recorded]"' in prod, (
            "the sentinel guard is gone — 30% of PDF dispatches would show "
            "'Request: [not recorded]' to every responder"
        )

    def test_production_orders_request_between_notes_and_contact(self):
        """The FULL append sequence, not pairwise indices.

        Pairwise `index()` comparisons cannot see an EXTRA append: adding a
        second `Request:` row above Notes leaves every pair still ordered and
        the pin green, while responders read the tasking line twice and in the
        wrong place. Caught by mutation on 2026-08-01. Asserting the exact
        sequence makes an insertion, a deletion and a swap all fail.
        """
        prod = self._code_only(self._fn("format_pinned_welcome"))
        appended = re.findall(r'lines\.append\(f"([A-Za-z ]+):', prod)
        assert appended == ["Wearing", "Notes", "Last seen", "Request", "Contact"], (
            f"the welcome's optional rows are now {appended!r}; expected "
            f"subject → tasking → logistics "
            f"(Wearing, Notes, Last seen, Request, Contact)"
        )

    def test_production_does_not_echo_filter_or_cap_the_request(self):
        """Both omissions are the decision, so both need a pin — an omission
        cannot fail loudly on its own."""
        prod = self._code_only(self._fn("format_pinned_welcome"))
        stanza = prod[prod.index("request_clean = request.strip()"):
                      prod.index('f"Request: {request_clean}"')]
        assert "novel_notes" not in stanza, (
            "the Request line was routed through novel_notes() — a resource "
            "ask that reuses a risk word would now be dropped"
        )
        assert "[:" not in stanza and "textwrap" not in stanza, (
            "a length cap was introduced on the Request line; the 482-char "
            "corpus entry carried its coordinates at the END"
        )

    def test_pdf_extract_collapses_the_multiline_request(self):
        """1 of 14 populated corpus forms wraps this AcroForm value across
        physical lines. Without the collapse, `^Request:\\s*(.+)$` keeps only
        the first — on that form it would have dropped 'SPANISH SPEAKING
        ONLY' from the tail."""
        src = (Path(__file__).parent / "pdf_extract.py").read_text(encoding="utf-8")
        code = "\n".join(l.split("#")[0] for l in src.splitlines())
        assert 'request    = " ".join(_f("request").split())' in code, (
            "pdf_extract.py no longer collapses the multi-line Request value, "
            "so a wrapped entry is silently truncated at the first newline."
        )


class TestWelcomeWearingProductionParity:
    """#845 — tie the mirror above to backend/slack.py.

    Every behavioural test in this file runs against the hand-written mirror,
    because slack_sdk is not installed locally. Reverting only slack.py would
    leave them all green — the exact hole that let three params be deleted from
    the real function in 2026-08-01 with 1841 tests passing.
    """

    @staticmethod
    def _prod():
        return (Path(__file__).parent / "slack.py").read_text(encoding="utf-8")

    @classmethod
    def _fn(cls):
        m = re.search(
            r"^def format_pinned_welcome\(.*?(?=\n\n(?:def |async def |# -{10,}))",
            cls._prod(), re.DOTALL | re.MULTILINE,
        )
        assert m, "format_pinned_welcome not found in slack.py"
        return m.group(0)

    @classmethod
    def _code_only(cls):
        """Strip the docstring AND comments before any assertion. The docstring
        explains the unbracketed-sentinel trap using the very literals asserted
        below, so a raw-source search passes with the guard deleted."""
        body = re.sub(r'""".*?"""', '', cls._fn(), flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_production_accepts_a_worn_parameter(self):
        sig = self._fn().split(") -> str:")[0]
        assert re.search(r"^\s*wearing: str = \"\",\s*$", sig, re.M), (
            "format_pinned_welcome no longer takes `wearing` — the welcome "
            "silently stopped carrying the subject's clothing description."
        )

    def test_production_renders_the_wearing_line(self):
        assert 'lines.append(f"Wearing: {wearing_clean}")' in self._code_only(), (
            "the Wearing line is gone from the pinned welcome — back to the "
            "state that made the dispatcher paste it in by hand on 2026-09-09"
        )

    def test_production_drops_the_unbracketed_not_recorded_sentinel(self):
        """The guard that a copy-paste from the last_seen shape would omit.
        Without it every blank-clothing dispatch publishes 'Wearing: Not
        recorded' to every responder."""
        prod = self._code_only()
        stanza = prod[prod.index("wearing_clean = wearing.strip()"):
                      prod.index('f"Wearing: {wearing_clean}"')]
        assert "casefold() not in (" in stanza, (
            "the unbracketed-sentinel guard is gone from the Wearing line; both "
            "intake paths emit the literal 'Not recorded' for a blank box"
        )
        # Each literal pinned separately, not the tuple: adding a newly
        # observed sentinel must not break this pin, but dropping one must.
        # "n/a" and "unknown" are not guesses — they are 2 of the 7 populated
        # clothing boxes across the 20 v2 corpus PDFs, i.e. 29% of the values
        # an officer actually wrote are contentless.
        for literal in ('"not recorded"', '"unknown"', '"n/a"'):
            assert literal in stanza, (
                f"the Wearing line stopped rejecting {literal} — a measured "
                f"contentless value now reaches every responder"
            )

    def test_production_also_drops_the_bracketed_template_leak(self):
        """Required ON TOP of the literal test — the JPEG path echoes Gemini's
        own bracketed instruction text when it extracts nothing."""
        prod = self._code_only()
        stanza = prod[prod.index("wearing_clean = wearing.strip()"):
                      prod.index('f"Wearing: {wearing_clean}"')]
        assert 'not wearing_clean.startswith("[")' in stanza, (
            "the bracketed-leak guard is gone from the Wearing line"
        )

    def test_production_escapes_the_worn_value(self):
        prod = self._code_only()
        stanza = prod[prod.index("wearing_clean = wearing.strip()"):
                      prod.index('lines.append(f"Wearing:')]
        assert "_mrkdwn_escape(wearing_clean)" in stanza, (
            "the Wearing value is no longer mrkdwn-escaped; clothing is free "
            "text and an & or < renders as broken markup"
        )

    def test_production_does_not_echo_filter_or_cap_the_worn_value(self):
        """Both omissions are the decision, so both need a pin — an omission
        cannot fail loudly on its own. Same rule as at-risk: the distinctive
        detail is commonly the last item the officer wrote."""
        prod = self._code_only()
        stanza = prod[prod.index("wearing_clean = wearing.strip()"):
                      prod.index('f"Wearing: {wearing_clean}"')]
        assert "novel_notes" not in stanza, (
            "the Wearing line was routed through novel_notes() — clothing that "
            "reuses an at-risk word would now be dropped"
        )
        assert "[:" not in stanza and "textwrap" not in stanza, (
            "a length cap was introduced on the Wearing line"
        )


class TestWelcomeLastSeenLine:
    """#755 — the subject's last-seen date/time in the pinned welcome.

    Elapsed time is the strongest urgency signal a responder gets off this
    message: hours versus days changes the whole response posture. It was on
    the intake form all along and reached nobody.

    Bill's placement constraint (2026-08-18) was "after the MP: line and before
    the Contact: line". It sits below Notes rather than above so that Notes
    keeps the adjacency its own Locked Decision justifies — it is risk detail
    qualifying the at-risk list directly above it.
    """

    _BASE = dict(
        event_name="2026-08-18 SJPD CAPITOL",
        mp_name="Jane Doe",
        age=74,
        gender="female",
        at_risk="dementia, alone",
    )

    def _welcome(self, **kw):
        return format_pinned_welcome(**{**self._BASE, **kw})

    def test_renders_the_last_seen_line(self):
        out = self._welcome(last_seen="2026-08-16 21:30")
        assert "Last seen: 2026-08-16 21:30" in out

    def test_sits_after_notes_and_before_request_and_contact(self):
        out = self._welcome(
            notes="walking toward the creek trail",
            last_seen="2026-08-16 21:30",
            request="K9, 2 ground teams",
            officer_contact="Ofc. Nguyen; 408-555-0134",
        ).splitlines()
        labels = [l.split(":")[0] for l in out[2:]]
        assert labels == ["Notes", "Last seen", "Request", "Contact"], (
            f"welcome rows are {labels!r}; expected subject → tasking → logistics"
        )

    def test_still_after_mp_when_notes_is_absent(self):
        """Notes is dropped on most dispatches, so this is the common shape."""
        out = self._welcome(
            last_seen="2026-08-16 21:30",
            officer_contact="Ofc. Nguyen",
        ).splitlines()
        assert out[1].startswith("MP: ")
        assert out[2] == "Last seen: 2026-08-16 21:30"

    @pytest.mark.parametrize("value", [
        "", "   ", "[not recorded]", "[NOT RECORDED]", "[time not recorded]",
        # The one that actually shipped past review: gemini.py:225 instructs
        # Gemini to emit this bracket text, and it echoes the instruction
        # verbatim when the officer left the field blank. Both other surfaces
        # dropped it; only the responder-facing pin published it.
        '[date/time only from form — e.g., "1/6/26 2300". Do NOT include '
        'address here; that goes in Last Known Position below]',
    ])
    def test_blank_and_sentinel_omit_the_line(self, value):
        out = self._welcome(last_seen=value, officer_contact="Ofc. Nguyen")
        assert "Last seen" not in out, (
            f"{value!r} reached responders. Both intake paths render an unfilled "
            f"field as '[not recorded]'; publishing that is worse than no line."
        )

    def test_time_only_value_renders_verbatim(self):
        """No date is inferred — the officer wrote a time, responders see a time."""
        out = self._welcome(last_seen="21:30")
        assert "Last seen: 21:30" in out

    def test_long_value_is_not_truncated(self):
        """Same rule as at-risk and Request: this field is never tail-cut."""
        value = "2026-08-16 21:30 (per reporting party; last confirmed sighting)"
        assert f"Last seen: {value}" in self._welcome(last_seen=value)


class TestWelcomeLastSeenProductionParity:
    """Ties the mirror above to backend/slack.py.

    This file mirrors production because slack_sdk is not installed locally, and
    a mirror with no production-reading pin is not a test of anything — three
    params were once deleted from the real function with all 1841 tests green.
    """

    @staticmethod
    def _prod():
        return (Path(__file__).parent / "slack.py").read_text(encoding="utf-8")

    @classmethod
    def _fn(cls):
        m = re.search(
            r"^def format_pinned_welcome\(.*?(?=\n\n(?:def |async def |# -{10,}))",
            cls._prod(), re.DOTALL | re.MULTILINE,
        )
        assert m, "format_pinned_welcome not found in slack.py"
        return m.group(0)

    @classmethod
    def _code_only(cls):
        body = re.sub(r'""".*?"""', "", cls._fn(), flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_production_accepts_a_last_seen_parameter(self):
        sig = self._fn().split(") -> str:")[0]
        assert re.search(r"^\s*last_seen: str = \"\",\s*$", sig, re.M), (
            "format_pinned_welcome no longer takes `last_seen` — the welcome "
            "silently stopped carrying the subject's last-seen time"
        )

    def test_production_renders_the_last_seen_line(self):
        assert 'lines.append(f"Last seen: {last_seen_clean}")' in self._code_only(), (
            "the Last seen line is gone from the pinned welcome"
        )

    def test_production_drops_every_bracketed_sentinel(self):
        """Broader than an exact-literal check, and that breadth is the fix.

        An exact `!= "[not recorded]"` comparison passed Gemini's own bracketed
        instruction text straight to responders while the Event Log and the D4H
        record both omitted it — the field exists on BOTH intake paths, unlike
        Request, which is PDF-only and so cannot carry a Gemini leak.
        """
        prod = self._code_only()
        assert 'not last_seen_clean.startswith("[")' in prod, (
            "the last-seen sentinel guard narrowed. Any bracketed value is a "
            "sentinel: '[not recorded]' from both intake paths, and Gemini's "
            "own '[date/time only from form …]' instruction text on the JPEG path"
        )
        assert 'last_seen_clean.lower() != "[not recorded]"' not in prod, (
            "the exact-literal check is back — it misses every sentinel except one"
        )

    def test_production_does_not_cap_the_last_seen_value(self):
        """The omission IS the decision, so it needs its own pin.

        Same reasoning as at-risk and Request: an officer may qualify the value
        ("per reporting party"), and a tail-truncation would cut the qualifier
        that tells a responder how much to trust it.
        """
        prod = self._code_only()
        stanza = prod[prod.index("last_seen_clean = last_seen.strip()"):
                      prod.index('f"Last seen: {last_seen_clean}"')]
        assert "[:" not in stanza and "…" not in stanza, (
            f"a length cap appeared in the last-seen stanza: {stanza!r}"
        )
