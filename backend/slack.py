"""slack.py — Slack API client + pure-logic helpers.

Phase 1 surface area:
- Pure logic (testable in local pytest):
    - Channel naming: incident_channel_name(),
      incident_channel_name_with_collision_suffix()
    - Text shaping: _gender_display(),
      format_pinned_welcome(), format_would_invite_message(),
      format_groups_requested(), format_tally_responder_line(),
      format_tally_multi_team_line()
- SDK wrappers (covered at live-test time on personal-dev):
    - find_or_create_private_channel(), create_collision_channel(),
      invite_user(), lookup_user_by_email(), post_message(),
      edit_message(), pin_message(), get_active_incident_management_members(),
      get_incident_channel_initial_members()

Test architecture (Task 1.6 design decision, with Bill 2026-04-26):
    Tests mirror the pure-logic helpers locally and assert on string shape.
    SDK wrappers are NOT exercised in local pytest because slack_sdk isn't
    installed in the local Python env (PEP 668 externally-managed).
    Revisit at end of Phase 1 (after Task 1.11) for a requirements-test.txt
    + venv-based pytest env.

CRITICAL — channel privacy:
    All channels created here use is_private=True. The Slack app uses
    `groups:write` scope only — NO `channels:*`. See design Section 5 +
    CLAUDE.md locked decision (#active-incidents private rule).

CRITICAL — channel naming:
    Default channel name strips the Everbridge HHMM uniqueness suffix so
    cross-app names (Slack ↔ CalTopo ↔ D4H) match for the dispatcher and
    field teams. On a real same-day same-street collision (different
    incident already owns the bare name), fall back to
    incident_channel_name_with_collision_suffix(). See "Naming Policy
    Across Surfaces" in the integration plan.
"""
import logging
import os
import re
from typing import Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Trailing " HHMM" Everbridge uniqueness suffix. The pre-comma space + 4 digits
# anchored to end of string is unambiguous given the canonical Everbridge event
# name format ("YYYY-MM-DD AGENCY STREET HHMM").
_HHMM_SUFFIX_RE = re.compile(r"\s\d{4}$")

# Item 4 (design Section 5) — gender long-form mapping.
# Pinned welcome message renders human-readable forms; OCR short-forms
# (M/F/NB) are mapped here. Unknown/empty → "Unknown".
_GENDER_LONG_FORM = {
    "M":  "Male",
    "F":  "Female",
    "NB": "Non-Binary",
}


# ---------------------------------------------------------------------------
# Pure-logic helpers
# ---------------------------------------------------------------------------

def _strip_hhmm_suffix(event_name: str) -> str:
    """Strip the trailing ' HHMM' Everbridge uniqueness suffix, if present."""
    return _HHMM_SUFFIX_RE.sub("", event_name)


# Slack channel-name character policy. `conversations.create` accepts ONLY
# lowercase letters, digits, hyphens, and underscores (≤ 80 chars); anything
# else returns 'invalid_name_specials'. incident_channel_name() previously
# only lowercased + swapped whitespace for underscores, so punctuation in the
# canonical event name reached the API unfiltered. The geocoder canonicalizes
# a park LKP to its official name (e.g. "Joseph D. Grant County Park"), and the
# "." in "D." made Slack reject the channel — crashing /send-notification at
# Step 6 AFTER Everbridge had already fired, orphaning a half-incident
# (2026-07-16 SJ Grant mock). Strip everything Slack disallows here so no
# punctuation can reach the API again — no matter how the event name was built
# (officer text OR geocoder-canonicalized place names like Mt./St./apostrophes).
_SLACK_CHANNEL_DISALLOWED_RE = re.compile(r"[^a-z0-9_-]+")
_SLACK_CHANNEL_MAX_LEN = 80


def _slugify_channel_name(text: str, *, max_len: int = _SLACK_CHANNEL_MAX_LEN) -> str:
    """Lowercase, whitespace→'_', drop Slack-illegal chars, collapse, cap.

    Produces a string satisfying Slack's channel-name grammar
    (^[a-z0-9_-]{1,80}$) for any input carrying the canonical YYYY-MM-DD
    event-name prefix. Hyphens (e.g. in the date) are preserved; a run of
    disallowed characters (".", "'", "&", ",", "/", "#", …) collapses to a
    single underscore rather than being deleted, so tokens don't fuse.

    ``max_len`` defaults to Slack's 80-char limit. The collision-suffix
    variant passes a smaller value to reserve room for the trailing "_HHMM"
    uniqueness token it appends afterward (see that function).
    """
    slug = "_".join(text.lower().split())
    # Drop apostrophes so intra-word marks don't split a token ("Joseph's" →
    # "josephs", not "joseph_s"). Other disallowed runs collapse to one "_"
    # so space-separated tokens stay separated ("5th & Main" → "5th_main").
    slug = slug.replace("'", "").replace("’", "")
    slug = _SLACK_CHANNEL_DISALLOWED_RE.sub("_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_-")
    return slug[:max_len].rstrip("_-")


def incident_channel_name(event_name: str) -> str:
    """'2026-04-25 MPD CALAVERAS 1430' → '2026-04-25_mpd_calaveras' (HHMM stripped).

    Naming Policy Across Surfaces (integration plan):
    Slack channel names exclude the Everbridge HHMM uniqueness suffix by
    default to keep cross-app names (Slack ↔ CalTopo ↔ D4H) consistent for
    the dispatcher and field teams. On collision, see
    incident_channel_name_with_collision_suffix().

    Slack-illegal characters are stripped via _slugify_channel_name so
    punctuation in a geocoded place name (e.g. the "." in "Joseph D. Grant
    County Park") can never reach conversations.create.
    """
    bare = _strip_hhmm_suffix(event_name)
    return _slugify_channel_name(bare)


def incident_channel_name_with_collision_suffix(event_name_with_hhmm: str) -> str:
    """'2026-04-25 MPD CALAVERAS 1430' → '2026-04-25_mpd_calaveras_1430'.

    Called only by find_or_create_private_channel after a name lookup
    confirms the bare channel name is owned by a DIFFERENT active incident.
    Keeps the Slack channel 1:1 with the Everbridge event in the rare
    same-day-same-street collision case.

    Raises ValueError if the input does not end with a HHMM suffix —
    caller is required to pass the canonical Everbridge event name.

    The HHMM token IS the uniqueness key, so it must survive the 80-char cap.
    We slugify the base with room reserved for "_HHMM" and re-append it, rather
    than slugifying the whole string and risking right-truncation dropping the
    suffix on a long street name (Aikido review, PR #576) — which would let two
    same-day-same-street collisions collapse to the same channel name.
    """
    m = _HHMM_SUFFIX_RE.search(event_name_with_hhmm)
    if not m:
        raise ValueError(
            "incident_channel_name_with_collision_suffix requires the HHMM suffix "
            "from the Everbridge event name"
        )
    hhmm = m.group().strip()  # 4-digit uniqueness token, e.g. "1904"
    base = _slugify_channel_name(
        _strip_hhmm_suffix(event_name_with_hhmm),
        max_len=_SLACK_CHANNEL_MAX_LEN - len(hhmm) - 1,  # -1 for the "_" joiner
    )
    return f"{base}_{hhmm}"


def _gender_display(gender: str) -> str:
    """Map OCR short-form gender to long-form display string.

    Item 4 (design Section 5): unknown or empty input renders 'Unknown' so
    the MP line stays well-formed. Whitespace tolerated and case-insensitive.
    """
    return _GENDER_LONG_FORM.get((gender or "").strip().upper(), "Unknown")


def novel_notes(raw_notes: str, at_risk: str) -> str:
    """Drop intake free-text snippets the at-risk line already says (issue #670).

    The responder-facing at-risk list is built from CHECKBOX state only. Free
    text written beside a risk-factor question never reached the field — on the
    2026-07-31 mutual-aid callout the mental-health question carried a
    description of a severe, search-strategy-altering cognitive impairment, the
    box was blank, and nobody in the field ever saw it.

    But most of that free text is NOT new information. Measured across the
    40-form corpus: 48 free-text snippets, and **26 of them merely restate the
    at-risk line** — a form whose at-risk reads "Dementia" also carries
    "Q6 — reason: DEMENTIA" and "Q9 — detail: DEMENTIA". Echoing those into the
    pinned welcome would spend the channel's readability (measurably improved by
    the #660/#661 threading) on text responders have already read one line above.

    A snippet is dropped when EVERY one of its substantial words (4+ letters)
    already appears in the at-risk text.

    The 4-letter floor exists so filler words cannot BLOCK echo detection. The
    condition is `all(word in at_risk)`, so every extra word makes a drop HARDER
    — a lower floor means FEWER drops, not more. "the DEMENTIA" is still an echo
    of an at-risk line reading "Dementia"; counting "the" would fail the match
    and let the echo through. Conversely a snippet holding any substantial word
    the at-risk line does not have is always kept, which is what protects the
    22 corpus snippets carrying genuinely novel detail.

    NO LENGTH CAP, deliberately — same rule as the at-risk segment. The text is
    dispatcher-curated and may carry the one fact that changes search strategy,
    so a tail-truncation could hide it. Measured max is 48 characters, so a cap
    would be dead code anyway.

    This function only ever REMOVES redundancy. It never infers a risk factor,
    never sets a checkbox, and never edits the at-risk list — per Bill,
    2026-07-31: a tool that infers risk from prose will eventually infer a wrong
    one, and a wrong risk factor is worse than a missing one.
    """
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
            continue  # pure echo of the at-risk line
        kept.append(snippet)
    return "; ".join(kept)


def format_pinned_welcome(
    *,
    event_name: str,
    mp_name: str,
    age: Optional[int],
    gender: str,
    at_risk: str,
    notes: str = "",
    last_seen: str = "",
    request: str = "",
    officer_contact: str = "",
) -> str:
    """Compose the pinned welcome message for the per-incident channel.

    PII policy (revised 2026-04-29): MP full name is INCLUDED. Searchers
    need the name to call out for the subject and ask neighbors. The
    CalTopo map (which all responders access) already exposes name + DOB +
    residence; WhatsApp dispatch already exposes the same. This message
    intentionally still excludes DOB, residence, LKP, and the Google Doc
    working-notes link — those are search-management context, not field-
    responder context. See CLAUDE.md "Slack welcome — MP name policy".

    Format rendered (lines in brackets appear only when non-empty):

        *{event_name}*
        MP: {mp_name} – {age}yo {gender_display}, {at_risk_clean}
        [Notes: {novel intake free text}]   ← issue #670
        [Last seen: {intake Last Seen At, verbatim}]   ← issue #755
        [Request: {intake Request box, verbatim}]
        [Contact: {officer_contact}]

    Notes sits directly under the MP line ON PURPOSE: it is risk detail and
    belongs beside the at-risk list it qualifies, whereas Contact is logistics.
    It is filtered through novel_notes() first, so a snippet that merely
    restates the at-risk line one row above is dropped rather than echoed.

    Last seen (issue #755, Kris/Ops) follows Notes, keeping every fact about the
    subject contiguous before the message turns to tasking and logistics. Bill's
    constraint (2026-08-18) was "after the MP: line and before the Contact: line";
    placing it below Notes rather than above satisfies that without displacing
    Notes from the at-risk list it qualifies.

    Elapsed time is the responder's strongest urgency signal — hours versus days
    changes the response — and it is rendered EXACTLY as the officer recorded it.
    A time with no date stays a bare time: the value reaches here already
    normalized (main._subject_last_seen_value), and an ambiguous field is never
    completed by inference.

    Request is the intake form's own "Request:" box — what the requesting
    agency is asking SAR to bring. It sits BELOW Notes and ABOVE Contact so the
    message reads subject → tasking → logistics.

    It is NOT filtered through novel_notes() and NOT capped, both deliberate
    (Bill, 2026-08-01), measured across the 20 v2 fillable PDFs in
    experiments/test_forms (14 populated; min 2 / median 16.5 / max 482 chars):

    - No echo filter. The #670 filter earns its place because 26 of 48 at-risk
      snippets were pure echoes; here exactly ONE of 14 ("SEARCH / RESCUE") is
      contentless. A filter would fire once in fourteen and buy nothing.
    - No length cap. Seven of the 14 name a dog resource, and "CADAVER DOGS"
      tells responders this is a recovery rather than a rescue. The longest
      entry (482 chars) is the one that mattered most: it carried a located
      vehicle at 37.33669, -121.71486 plus a possible sighting, coordinates
      that appear NOWHERE else on the form — and it carried them at the END,
      exactly where a tail-truncation would have cut. Same reasoning as
      "Slack at-risk — no truncation".

    PDF PATH ONLY, accepted knowingly: only pdf_extract.py emits a "Request:"
    line; the Gemini JPEG prompt has no such field, so JPEG-path dispatches
    never carry this line. Not worth adding to the ~50–80%-accuracy vision
    track — the standing direction is to route forms to the PDF path instead.

    STAGING IS NO LONGER IN THIS MESSAGE (issue #673). It is posted as its
    own pinned message by format_staging_message() — see that function for
    why. Do not fold it back in: the whole point is that staging can be
    deleted and re-posted on its own when a dispatch goes out with the wrong
    location, without destroying the subject and contact information here.

    The CalTopo URL is NOT in this message — it's posted as a separate
    follow-up message (also pinned) by /send-notification so Slack
    unfurls the map preview card. Posting the URL twice in the channel
    causes Slack to dedupe and skip the unfurl on the second occurrence
    (verified empirically 2026-04-29) — even when the welcome was posted
    with unfurl_links=False, Slack still records the URL as "seen". See
    CLAUDE.md "Slack welcome — MP name policy reversal + dual-pin layout".

    Notes:
    - Separator between mp_name and age is an EN-DASH (U+2013, " – "),
      not a hyphen-minus. Slack renders the en-dash with visual padding
      that distinguishes the name from the age field at a glance.
    - mp_name is rendered as-is. If the OCR captured "Lastname, Firstname"
      the comma is preserved verbatim (officer convention, not normalized).
    - Caller passes event_name with the HHMM suffix already stripped.
    - gender renders long-form (Male/Female/Non-Binary).
    - at_risk empty → 'no risk factors' literal (NOT a missing trailing comma).
    """
    gender_display = _gender_display(gender)
    # No length cap: at-risk indicators are dispatcher-curated and arrive
    # in non-deterministic order, so a tail-truncation could silently hide
    # a critical factor. See CLAUDE.md "Slack at-risk — no truncation".
    at_risk_clean = at_risk.strip()
    if not at_risk_clean:
        at_risk_clean = "no risk factors"
    # Issue #374: defensive — if frontend age parser failed (or any future
    # caller passes None), render "?yo" so the welcome never shows "Noneyo".
    # Cluster F (Slack-M6, 2026-05-26): also treat empty-string as "no age."
    # Frontend sends `mp_age: ""` (not null) when OCR fails to extract age;
    # the prior `age is not None` guard let "" through and produced "yo".
    age_str = f"{age}yo" if age not in (None, "") else "?yo"
    mp_line = f"MP: {mp_name} – {age_str} {gender_display}, {at_risk_clean}"

    contact_clean = officer_contact.strip()
    lines = [
        f"*{event_name}*",
        mp_line,
    ]
    notes_clean = novel_notes(notes, at_risk_clean)
    if notes_clean:
        lines.append(f"Notes: {notes_clean}")
    # pdf_extract.py renders an empty Request box as the literal "[not
    # recorded]" sentinel, and 6 of 20 corpus forms leave it blank. Dropping the
    # sentinel is done HERE rather than in the frontend regex for the same
    # reason novel_notes() is server-side: this path is pytest-covered and that
    # regex is not.
    # ANY bracketed value is absent, which is deliberately BROADER than the
    # exact-literal check Request uses below. Request is PDF-path-only by
    # Locked Decision — gemini.py has no such field — so the only sentinel it
    # can ever receive is pdf_extract's literal "[not recorded]". Last Seen At
    # exists on BOTH paths, and the JPEG path can echo Gemini's own bracketed
    # instruction text ("[date/time only from form — e.g. …]") when it extracts
    # nothing; that is a documented recurring leak, already guarded for Event #
    # by _D4H_RE_EVENT_NUM_PLACEHOLDER in main.py.
    #
    # This must agree with main._subject_last_seen_value, which feeds the Event
    # Log and the D4H record. It reads the same field but via a different route
    # (frontend parse → payload, versus a server-side re-parse), so the rule has
    # to be stated twice; a narrower rule here published a template leak to
    # responders while both other surfaces correctly omitted it.
    last_seen_clean = last_seen.strip()
    if last_seen_clean and not last_seen_clean.startswith("["):
        lines.append(f"Last seen: {last_seen_clean}")
    request_clean = request.strip()
    if request_clean and request_clean.lower() != "[not recorded]":
        lines.append(f"Request: {request_clean}")
    if contact_clean:
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
    """Compose the standalone pinned staging message (issue #673).

    Staging used to be the last line of the pinned welcome. It was split out
    because a dispatch that goes out with the WRONG staging location has no
    correction path: only a message's author can edit a Slack message, and the
    bot is the author, so no human can fix the pin. Splitting staging into its
    own message makes the wrong part independently deletable — a workspace
    admin CAN delete a bot message even though they cannot edit one — without
    destroying the subject and contact information in the welcome.

    A bot-side chat.update on the stored welcome_ts was considered and REJECTED
    (Bill, 2026-08-01). It only works while something is still driving the app,
    and by the time staging is discovered to be wrong the dispatcher has closed
    Dispatch Turbo and is driving to staging themselves. The same assumption is
    what made the #612 follow-up fail on its first real use (#455, expired token,
    dispatcher mid-stand-down). A correction path that requires the dispatcher's
    session to still be alive is not a correction path.

    Being its own pin is also the point, not a side effect: someone opening the
    channel's pinned-items list sees staging at a glance instead of having to
    read to the bottom of the welcome.

    Rendered:

        Staging: <apple_url|staging_address> (<google_url|G>)

    THE TEXT IS THE QUERY. staging_apple_url / staging_google_url are built by
    the frontend as maps.apple.com/?q=<TEXT> and google.com/maps/search/<TEXT>
    from this same staging text, never from coordinates — see the Locked
    Decision "Staging line text IS the responders' maps-link query". Any
    replacement message must regenerate the links, not only the words.

    Caller MUST post this with unfurl_links=False / unfurl_media=False, exactly
    as the welcome was. These are maps URLs; letting them unfurl adds two large
    preview cards to a channel whose readability was measurably improved by the
    #660/#661 threading work.

    `unverified` appends STAGING_UNVERIFIED_WARNING. It is set when the
    officer-coordinate-vs-LKP distance guard fired, i.e. the two location
    sources we were given disagree by more than _MAX_STAGING_DIST_M and one of
    them is wrong.

    `unmapped` appends STAGING_UNMAPPED_WARNING. It is set when the staging POI
    lookup returned ZERO candidates, which means Gemini generated the entire
    recommendation list from training data with nothing to check it against.
    That output is formatted identically to a real one — same addresses, same
    confident parking estimates — so without this marker a responder cannot
    tell the difference. Observed live 2026-08-01: a remote anchor produced two
    entries at the SAME invented address, which no dedup could catch because
    there were no candidates to dedup.

    Why it belongs on THIS message and not its own (Bill, 2026-08-01): the
    staging pin is already the thing an admin deletes and re-posts when staging
    turns out to be wrong (#673), so a warning attached to it is self-cleaning —
    the corrected replacement simply carries no warning. A separate message
    would have to be remembered and removed by hand, and would spend more of
    the channel readability #660/#661 delivered.

    Deliberately does NOT name which source is wrong. On the 2026-08-01
    Humboldt run the LKP was the wrong one and the officer's coordinate was
    right; asking a responder to adjudicate that is not actionable. Telling
    them not to trust it until Dispatch confirms is.
    """
    lines = [f"Staging: <{staging_apple_url}|{staging_address}> (<{staging_google_url}|G>)"]
    # Two INDEPENDENT conditions, deliberately not merged into one sentence.
    # They co-occur (a remote LKP that also disagrees with the officer's
    # coordinate — live on 2026-08-01), and they call for different scepticism:
    # a conflict means the REGION may be wrong, unmapped means the ADDRESS may
    # not exist. Conflict first: it is the larger error.
    if unverified:
        lines.append(STAGING_UNVERIFIED_WARNING)
    if unmapped:
        lines.append(STAGING_UNMAPPED_WARNING)
    return "\n".join(lines)


def format_would_invite_message(
    *,
    already_member_names: list[str],
    resolvable_names: list[str],
    unresolvable_names: list[str],
) -> str:
    """Shadow-mode per-cycle message — three-bucket partition.

    'already_member_names' = safe-list pre-invitees who replied YES. They
    are already in the channel from send-time pre-invite, so we post the
    timeline marker '<Name> added' (matching full-mode's per-arrival line).
    Posting 'Would invite: <Name>' for these would be misleading because
    they ARE invited and present — bug surfaced 2026-04-30 in live test.

    'resolvable_names' = NOT pre-invited but their EB record carries an
    email; under FULL mode we'd be able to lookup_user_by_email + invite.
    Shadow mode emits 'Would invite: <Name>' WITHOUT actually inviting,
    so non-safe-list responders never receive a Slack contact (the
    dispatcher-confidence build-up before Slack rollout to all responders).

    'unresolvable_names' = no email known anywhere (ack, contact_email_map,
    safe-list name-match). Even full mode couldn't invite. Diagnostic line
    surfaces the EB-side email gap to the dispatcher.

    Wording of that last line (2026-07-19): it read "Cannot invite (no
    matching Slack account)", which was false on two counts — shadow mode
    performs NO Slack lookup at all, and the actual condition is a missing
    EMAIL, not a missing Slack account. It misfired on the 2026-07-19 live
    test against a responder who has both a Slack account and an @sccssar.org
    address in Everbridge; she was simply never in `target_contact_ids` (added
    to the send from the Everbridge UI under EVERBRIDGE_MODE=safe), so no
    email was ever resolved for her. Now names the real condition, matching
    format_invite_failed_message's INVITE_FAIL_NO_EB_EMAIL wording so shadow
    and full modes describe the same situation the same way.

    Empty buckets render no lines. Empty all-around → empty string; caller
    decides whether to post anything.
    """
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


# Reasons a YES responder could not be auto-invited to the incident channel
# (full mode). Each maps to a DIFFERENT dispatcher remedy — see
# format_invite_failed_message.
INVITE_FAIL_NO_EB_EMAIL   = "no_eb_email"
INVITE_FAIL_NO_SLACK_USER = "no_slack_user"
INVITE_FAIL_ERROR         = "error"


def format_invite_failed_message(*, name: str, reason: str) -> str:
    """Full-mode per-arrival line when a YES responder could NOT be invited.

    Counterpart to the '<Name> added' line. That line used to be posted
    UNCONDITIONALLY — it sat outside main.py's `if uid:` guard — so a
    responder who was never invited still produced a positive confirmation in
    the incident channel. Found 2026-07-19 on personal-dev: a responder whose
    email was unresolvable at poll time read as "added" while being absent
    from the channel. Full mode had no other signal either: the G.5 ERROR
    only fires when a lookup RAISED, so the zero-email case logged nothing.
    Net effect was a false positive on the one surface the dispatcher reads.

    The dispatcher is the person who can fix each cause, so the reason is
    named rather than collapsed into a generic failure:
      no_eb_email    — no email path on the Everbridge contact; remedy is to
                       add their @sccssar.org address in EB
      no_slack_user  — email(s) known but no Slack account matched; remedy is
                       to invite them to the Slack workspace
      error          — a lookup or invite call failed; remedy is to add them
                       manually (diagnostics are in the Cloud Run logs)

    Deliberately does NOT include the email address itself: incident channels
    carry PHI, and the address adds nothing the dispatcher cannot already see
    in Everbridge. An unknown reason falls back to the generic error wording
    rather than raising — a poll cycle must never die in a message-formatting
    branch (Failure-mode rubric Q2).
    """
    who = to_conversational_name(name)
    if reason == INVITE_FAIL_NO_EB_EMAIL:
        why = "has no SAR email in Everbridge"
    elif reason == INVITE_FAIL_NO_SLACK_USER:
        why = "has no Slack account"
    else:
        why = "could not be added automatically"
    return f"⚠️ {who} replied YES but {why} — add to channel manually"


def format_groups_requested(group_names: list[str]) -> str:
    """Item 7 — channel-creation message listing the EB groups dispatcher selected.

    Wording is 'Groups requested by dispatcher' (NOT 'Would invite groups')
    because Slack is people-only — we never invite EB groups into Slack
    channels. This message is informational, posted once at channel
    creation, regardless of slack_mode.
    """
    return f"Groups requested by dispatcher: {', '.join(group_names)}"


def to_conversational_name(name: str) -> str:
    """Convert a 'Last, First' name string to 'First Last' for human display.

    `_shape_responder_name()` in main.py returns names as 'Last, First' so that
    `sorted(names)` produces an alphabetical-by-last-name list (the order
    dispatchers and search managers expect when scanning the tally). But once
    you join multiple 'Last, First' strings with commas the result is
    ambiguous — 'Burns, Bill, Black, Kris' could be 4 first-names or 2 people.

    This helper converts at DISPATY TIME ONLY: storage stays 'Last, First',
    sorting still works, but the rendered Slack line shows 'Bill Burns;
    Kris Black' (with semicolon separators) for unambiguous reading.

    Multi-word last names (e.g. EB ack `last_name="Cadena Resendez"`,
    `first_name="Priscilla"` → `_shape_responder_name` returns
    'Cadena Resendez, Priscilla') are handled by splitting on the FIRST ', '
    only: result is 'Priscilla Cadena Resendez', not 'Priscilla Cadena
    Resendez' (no, wait — that's the same; the bad outcome we're avoiding is
    'Priscilla Cadena, Resendez', which would happen with split(', ')[0:1]).

    Pass-through when no ', ' is present (single-name fallback from
    `_shape_responder_name` when only first OR last name is known).

    Bug history: 2026-05-08 SJSU live-incident review surfaced the ambiguity
    in #active-incidents tallies and the per-incident Would-invite lines.
    """
    if not name or ", " not in name:
        return name  # Empty / None / single-name → pass-through
    last, first = name.split(", ", 1)   # First split only — preserve multi-word last names
    return f"{first} {last}"


def format_tally_responder_line(group: str, names: list[str]) -> str:
    """One per-group line in the live tally: '• K9 (3): Bill Burns; Kris Black; Janae Cubeiro'.

    `names` arrives as 'Last, First' strings (from `_shape_responder_name` +
    `sorted(names)` upstream → alphabetical by last name). We convert each
    to 'First Last' here for display and use '; ' as the separator so the
    list is unambiguous regardless of multi-word last names.
    """
    converted = [to_conversational_name(n) for n in names]
    return f"• {group} ({len(names)}): {'; '.join(converted)}"


def format_tally_multi_team_line(name: str, groups: list[str]) -> str:
    """Item 6 — multi-team callout line for the live tally.

    Was '🔸 Burns: K9, Drivers' in the PoC. Slack renders 🔸 (small orange
    diamond) larger than expected, dominating the tally on big incidents.
    Replaced with two-space indent + ↳ (down-and-right arrow) which Slack
    renders small and visually subordinate to the per-group listing above.

    `name` arrives as 'Last, First' — convert to 'First Last' for display.
    `groups` are single-token group names (e.g. 'Canine Team') with no
    embedded commas, so comma-separation stays unambiguous.
    """
    return f"  ↳ {to_conversational_name(name)}: {', '.join(groups)}"


def format_incident_dm_text(*, channel_id: str, test_label: str = "") -> str:
    """Compose the responder DM sent when they're added to an incident channel.

    Pairs with the VIP-breakthrough-DM feature — PRD at
    SAR Slack Onboarding/PRD_DispatchTurbo_Responder_DM_VIP_Breakthrough.

    The DM's purpose is a phone-notification breakthrough: on a locked
    phone under Do Not Disturb / notification schedule, a DM from a bot
    the recipient has added to their VIP list will break through the
    silence (unlike an @channel post). Onboarding doc handles the member-
    side VIP setup separately; this formatter is the sender side.

    Format rendered (2-line when test_label set; 1-line otherwise):

        {test_label}          ← first line; survives push-preview truncation
                              ← blank-line separator
        You've been added to incident <#CHANNEL_ID>. Tap to open ...

    When test_label is empty (production shape), only the incident sentence
    is emitted. `<#CHANNEL_ID>` is Slack channel deep-link mrkdwn — Slack
    auto-renders it as #channel-name at display time and opens the channel
    on tap. Empirically confirmed 2026-07-10 spike: unknown channel IDs
    fall back to "🔒 private channel" (fine — callers pass IDs from
    find_or_create_private_channel() which are always real).

    test_label first-line placement is LOAD-BEARING — see
    feedback-test-messages-to-humans-need-explicit-test-label memory. A
    locked-screen notification preview truncates; the marker must be
    visible before content is cut off.

    Raises ValueError if channel_id is empty/whitespace-only — a broken
    deep link is a silent bug worse than a loud fail. Mirrors the
    incident_channel_name_with_collision_suffix pattern.
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


# ===========================================================================
# SDK wrappers — NOT covered by local pytest. See test architecture note in
# module docstring. Each wrapper is minimal (build args → SDK call → return);
# logic-bearing changes go in the pure helpers above, not here.
# ===========================================================================

def _client() -> WebClient:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN env var not set")
    return WebClient(token=token)


def send_incident_dm(user_id: str, dm_text: str) -> str:
    """Send the VIP-breakthrough responder DM. Return the message ts.

    Two-step per Slack docs:
      1. conversations.open with the target user → returns IM channel ID.
         Idempotent + silent (no notification). Slack maintains 1:1 IM
         channels persistently — repeated opens return the same channel.
      2. chat.postMessage to that IM channel with the pre-formatted text.
         Unfurl is suppressed — the DM's <#CHANNEL_ID> is Slack channel
         mrkdwn (not a URL), so unfurl_links doesn't apply to it. Kept
         off defensively in case dm_text ever grows to include URLs.

    Caller composes dm_text via format_incident_dm_text() (pure helper
    tested in test_slack.py::TestFormatIncidentDmText).

    Raises SlackApiError on failure — caller catches and routes to
    UC4 undeliverable path (incident-channel notice + ERROR log).
    Common failure modes: user_not_found (bot can't see this user),
    cannot_dm_bot (attempting to DM another bot), user_disabled,
    account_inactive, missing_scope (im:write not granted).

    Empirical verification 2026-07-10 (spike_dm_prerequisites.py) —
    multi-channel guests CAN receive DMs from this bot with im:write
    granted; single-channel guests cannot (silent block at the
    conversations.open step).
    """
    client = _client()
    im = client.conversations_open(users=user_id)["channel"]["id"]
    resp = client.chat_postMessage(
        channel=im, text=dm_text,
        unfurl_links=False, unfurl_media=False,
    )
    return resp["ts"]


def lookup_user_by_email(email: str) -> Optional[str]:
    """Return Slack user_id for the email, or None if unmatched.

    `users_not_found` is a real-and-expected outcome for SAR members who
    haven't joined the SCCSSAR Slack workspace yet — caller treats it as
    'cannot invite' rather than an error.
    """
    client = _client()
    try:
        resp = client.users_lookupByEmail(email=email)
        return resp["user"]["id"]
    except SlackApiError as e:
        if e.response.get("error") == "users_not_found":
            return None
        raise


def get_active_incident_management_members() -> list[str]:
    """Return Slack user_ids in the @active_incident_management user group.

    Admin-curated source for who gets pre-added to every incident channel
    (search-management / coordination staff). Replaces the previous
    "everyone in #active-incidents" source: #active-incidents is an OPEN
    watch-channel, so sourcing invites from its membership pulled curious
    lurkers into every incident (issue #579).

    Reads the numeric group ID from ACTIVE_INCIDENT_MGMT_USERGROUP_ID (set
    via Terraform). usergroups.users.list returns member user_ids directly
    (no email->uid resolution needed) and is NOT paginated — the full
    membership comes back in one call. Guests are excluded by Slack from
    user groups, so the SO Coordinator (a guest) is NOT here; the caller
    adds them separately via SLACK_SO_COORDINATOR_EMAIL.
    """
    usergroup_id = os.environ.get("ACTIVE_INCIDENT_MGMT_USERGROUP_ID", "")
    if not usergroup_id:
        raise RuntimeError("ACTIVE_INCIDENT_MGMT_USERGROUP_ID env var not set")
    client = _client()
    resp = client.usergroups_users_list(usergroup=usergroup_id)
    return resp.get("users", [])


def get_incident_channel_initial_members(
    *,
    dispatcher_email: str,
    so_coordinator_email: str,
) -> list[str]:
    """IDENTICAL invitee set in both slack_mode values (full + shadow).

    Returns the dedup'd union of:
      - dispatcher (looked up from their Google ID token email)
      - SO Coordinator (from slack-so-coordinator-email secret; a guest, so
        NOT in the user group — added here by email)
      - all @active_incident_management user-group members (issue #579 —
        admin-curated pre-add source, replacing #active-incidents membership)

    Deliberately NOT here: #active-incidents members (the open watch-channel
    whose leak #579 fixes) and dispatch-safe-list members (that secret is now
    ONLY the VIP-DM cohort, read separately at send Step 7b). Affirmative
    responders are invited later at poll time, not from this set.
    """
    user_ids: set[str] = set()
    for email in [dispatcher_email, so_coordinator_email]:
        if not email:
            continue
        uid = lookup_user_by_email(email)
        if uid:
            user_ids.add(uid)
    # User-group membership is best-effort. This runs at main.py Step 7,
    # AFTER EB has already fired, so NO failure here may crash dispatch —
    # an unguarded raise would leave a live notification with no Firestore
    # doc and no closure path. Catch broadly (matches the per-user invite
    # try/except in main.py Step 7): SlackApiError (rate-limit/transient)
    # AND RuntimeError (ACTIVE_INCIDENT_MGMT_USERGROUP_ID unset — e.g. code
    # deployed before the Terraform apply that sets it). Degrades to
    # dispatcher + SO coordinator for this dispatch only.
    try:
        user_ids.update(get_active_incident_management_members())
    except Exception as e:
        detail = (e.response.get("error", "unknown")
                  if isinstance(e, SlackApiError) else str(e))
        logger.warning(
            "get_active_incident_management_members failed (%s) — initial-invite "
            "list will exclude the user group for this dispatch", detail,
        )
    return sorted(user_ids)


def find_or_create_private_channel(
    *, event_id: str, event_name_with_hhmm: str,
) -> tuple[str, str]:
    """Create the private incident channel; return (channel_id, channel_name_used).

    First attempt: bare name (HHMM stripped) so cross-app names match.
    On `name_taken`: look up the existing channel and return it. Caller
    (main.py /Task 1.10) is responsible for the Firestore collision check —
    decides whether to reuse the existing channel (same incident, transient
    re-entry) or call create_collision_channel() (different incident).

    Issue #381: when the existing channel is archived (prior test, or
    auto-archived by Slack), unarchive it BEFORE returning so the caller's
    invite_user / post_message / pin_message calls succeed. Without this,
    Slack rejects every subsequent operation with `is_archived` and the
    dispatch flow 500s after the EB notification has already fired.
    Unarchiving (vs. falling back to a suffix-named new channel) preserves
    channel continuity — members, pinned messages, history.
    """
    bare_name = incident_channel_name(event_name_with_hhmm)
    client = _client()
    try:
        resp = client.conversations_create(name=bare_name, is_private=True)
        return resp["channel"]["id"], bare_name
    except SlackApiError as e:
        if e.response.get("error") != "name_taken":
            raise
    existing_id = _lookup_private_channel_id(bare_name)
    _unarchive_if_needed(client, existing_id)
    return existing_id, bare_name


def _unarchive_if_needed(client, channel_id: str) -> None:
    """Unarchive the channel if it's archived; idempotent (no-op when active).

    Issue #381: conversations.list returns archived channels by default,
    so when find_or_create_private_channel hits name_taken on an archived
    channel, the lookup returns its ID — but every subsequent Slack API
    call (invite, post, pin) then fails with is_archived. Surface here so
    the channel is usable before any caller-side work begins.

    Slack's `not_archived` error means the channel is already active —
    swallow it so this function is safe to call unconditionally.
    """
    try:
        client.conversations_unarchive(channel=channel_id)
    except SlackApiError as e:
        if e.response.get("error") == "not_archived":
            return
        raise


def _lookup_private_channel_id(name: str) -> str:
    client = _client()
    cursor = None
    while True:
        lst = client.conversations_list(
            types="private_channel", cursor=cursor, limit=200,
        )
        for ch in lst["channels"]:
            if ch["name"] == name:
                return ch["id"]
        cursor = lst.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    raise RuntimeError(f"Channel {name} reported name_taken but not found in list")


def create_collision_channel(event_name_with_hhmm: str) -> tuple[str, str]:
    """Create the HHMM-suffixed channel after main.py confirms a real collision.

    Mirrors find_or_create_private_channel's name_taken handling: if the
    HHMM-suffixed name already exists (archived prior test, manual rename,
    or rare same-minute same-suffix re-entry), look it up and unarchive
    rather than crashing dispatch after EB has fired. Without this, an
    archived prior-test channel at the same suffix orphans the EB
    notification with no Firestore doc and no closure path.
    """
    coll_name = incident_channel_name_with_collision_suffix(event_name_with_hhmm)
    client = _client()
    try:
        resp = client.conversations_create(name=coll_name, is_private=True)
        return resp["channel"]["id"], coll_name
    except SlackApiError as e:
        if e.response.get("error") != "name_taken":
            raise
    existing_id = _lookup_private_channel_id(coll_name)
    _unarchive_if_needed(client, existing_id)
    return existing_id, coll_name


# Slack errors from conversations.invite that we treat as success (the
# desired end state — user is in the channel — is already reached):
#   already_in_channel — user is already a member (retry after partial
#     failure, or member happened to join via another path).
#   cant_invite_self — Slack rejects the bot inviting its own user_id.
#     Could fire if the bot's user_id ever appears in the initial-invite
#     list (e.g. the bot is added to @active_incident_management). The bot
#     is also the channel creator of every incident channel (auto-member),
#     so this is the no-op equivalent of already_in_channel.
_SWALLOWED_INVITE_ERRORS = frozenset({
    "already_in_channel",
    "cant_invite_self",
})


def _should_swallow_invite_error(err: str | None) -> bool:
    """True iff the Slack invite error means the desired end state is
    already reached (user in channel, or bot trying to invite itself)."""
    return err in _SWALLOWED_INVITE_ERRORS


def invite_user(channel_id: str, user_id: str) -> None:
    """Idempotent — see _SWALLOWED_INVITE_ERRORS for the swallow set."""
    client = _client()
    try:
        client.conversations_invite(channel=channel_id, users=user_id)
    except SlackApiError as e:
        if _should_swallow_invite_error(e.response.get("error")):
            return
        raise


def post_message(
    channel_id: str, text: str, blocks: Optional[list] = None,
    *,
    unfurl_links: bool = True,
    unfurl_media: bool = True,
    thread_ts: Optional[str] = None,
) -> str:
    """Returns ts of posted message.

    Defaults preserve existing Slack behavior (auto-unfurl all links/media).
    The pinned welcome message uses unfurl_links=False, unfurl_media=False to
    suppress the noisy Google/Apple Maps unfurl cards — a separate follow-up
    message containing only the CalTopo URL is posted with default unfurl,
    so responders see exactly one map preview card (CalTopo) for zero-click
    situational awareness.

    thread_ts posts the message as a reply under that parent instead of at
    the top level of the channel. Empirically verified 2026-07-28 via
    experiments/everbridge_slack/spike_thread_notify.py on a real device:
    a bot-authored threaded reply produced NO notification on either mobile
    or desktop for a member with the channel set to "All new posts" (the
    untouched default), while a top-level control message posted seconds
    earlier notified both. Slack only notifies thread repliers, the thread
    STARTER, @-mentioned users, and anyone who opted into "Follow every
    thread" — and the starter here is the bot, not a human.

    Two things MUST stay true for that silence to hold, and both are
    properties of the CALLER's text, not of this wrapper:
      - reply_broadcast is never set (it would mirror the reply into the
        channel timeline and notify everyone — the exact noise this exists
        to remove). It is deliberately not exposed as a parameter.
      - the text contains no <@UID> mention. Rendering a responder's name
        as a mention instead of plain text reads nicer and is the obvious
        future "improvement" — it would also ping that person on every
        arrival. See _POLL_ARRIVAL_THREAD_SENTINELS in test_main_regression.
    """
    client = _client()
    resp = client.chat_postMessage(
        channel=channel_id, text=text, blocks=blocks,
        unfurl_links=unfurl_links, unfurl_media=unfurl_media,
        thread_ts=thread_ts,
    )
    return resp["ts"]


def edit_message(
    channel_id: str, ts: str, text: str, blocks: Optional[list] = None,
) -> None:
    client = _client()
    client.chat_update(channel=channel_id, ts=ts, text=text, blocks=blocks)


def pin_message(channel_id: str, ts: str) -> None:
    """Idempotent — `already_pinned` is silently swallowed."""
    client = _client()
    try:
        client.pins_add(channel=channel_id, timestamp=ts)
    except SlackApiError as e:
        if e.response.get("error") == "already_pinned":
            return
        raise
