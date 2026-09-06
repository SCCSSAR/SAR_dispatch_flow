"""test_send_notification.py — pure-logic tests for the /send-notification
orchestration helpers (Task 1.10b).

Per CLAUDE.md test file pattern: mirror the pure-logic functions from
`backend/main.py` locally rather than importing the module directly.
`backend/main.py` has heavyweight GCP / Vertex AI / httpx / google-api
dependencies not installed in local pytest.

When updating the helpers in main.py, ALSO update the mirror here. The
mirror IS the test contract — drift surfaces in production behavior,
and these tests are the regression boundary.

Test coverage (Task 1.10b):
  - _decide_collision() — channel reuse vs. collision logic
  - _compose_active_incidents_tally() — design Section 5 tally rendering,
    Item 6 multi-team indent rule, Phase 0 Task 8 last-non-empty cache fallback

NOT exercised here (covered at live-test time on personal-dev):
  - /send-notification orchestration sequence (FastAPI TestClient + heavyweight
    imports — deferred to end-of-Phase-1 venv test env follow-up)
  - _create_or_collide_channel() — wraps _decide_collision + Slack SDK + Firestore
  - _enqueue_poll_task() / _enqueue_template_delete_task() — STUB until Task 1.11
"""
import pytest


# ---------------------------------------------------------------------------
# Mirrored helpers — must be kept in sync with backend/main.py
# ---------------------------------------------------------------------------

def _decide_collision(
    *,
    this_event_id: str,
    existing_channel_owner_event_id: str | None,
) -> str:
    """Mirror of backend/main.py::_decide_collision()."""
    if existing_channel_owner_event_id is None:
        return "reuse"
    if existing_channel_owner_event_id == this_event_id:
        return "reuse"
    return "collide"


# Mirror of slack.py format_tally_responder_line / format_tally_multi_team_line.
# Pinned in test_slack.py too; replicated here so this test file stands alone.
def _format_tally_responder_line(group: str, names: list[str]) -> str:
    return f"• {group} ({len(names)}): {', '.join(names)}"


def _format_tally_multi_team_line(name: str, groups: list[str]) -> str:
    return f"  ↳ {name}: {', '.join(groups)}"


def _compose_active_incidents_tally(doc: dict, header: str) -> str:
    """Mirror of backend/main.py::_compose_active_incidents_tally().

    The mirror inlines format_tally_*_line rather than importing slack.py
    (slack.py imports slack_sdk which isn't in the local env). Both call
    sites format the same — see test_slack.py for direct tests of the
    line-format helpers themselves.
    """
    responders = doc.get("last_non_empty_responders") or doc.get("responders") or []
    by_group: dict[str, list[str]] = {}
    member_groups: dict[str, list[str]] = {}
    for r in responders:
        name = r["name"]
        for group in r.get("groups", []) or []:
            by_group.setdefault(group, []).append(name)
            member_groups.setdefault(name, []).append(group)

    # Tri-count line (issue #592) — mirror of main.py.
    yes_count = len(responders)
    decline_count = doc.get("decline_count", 0)
    no_response_count = doc.get("no_response_count", 0)
    lines = [
        f"*{header} — {doc['event_name_human']}*",
        f"*✅ {yes_count} confirmed   ❌ {decline_count} declined   "
        f"⏳ {no_response_count} no response*",
    ]
    for group_name, names in sorted(by_group.items()):
        lines.append(_format_tally_responder_line(group_name, sorted(names)))

    multi_team = sorted(
        (name, sorted(groups))
        for name, groups in member_groups.items()
        if len(groups) > 1
    )
    for name, groups in multi_team:
        lines.append(_format_tally_multi_team_line(name, groups))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# _decide_collision()
# ---------------------------------------------------------------------------

class TestDecideCollision:
    def test_no_existing_owner_means_reuse(self):
        # Channel exists in Slack but no Firestore incident references it.
        # Most likely a manually-created channel (operator pre-staged it) or
        # a leftover from a deleted incident doc. Reuse rather than block
        # the dispatcher.
        decision = _decide_collision(
            this_event_id="2026-04-25_mpd_calaveras_1430",
            existing_channel_owner_event_id=None,
        )
        assert decision == "reuse"

    def test_owner_matches_this_event_means_reuse(self):
        # Same incident retrying after a transient partial-failure (e.g.
        # Firestore write succeeded but the response timed out and the
        # dispatcher hit Send again).
        decision = _decide_collision(
            this_event_id="2026-04-25_mpd_calaveras_1430",
            existing_channel_owner_event_id="2026-04-25_mpd_calaveras_1430",
        )
        assert decision == "reuse"

    def test_owner_different_means_collide(self):
        # Real same-day same-street collision — different incident already
        # owns the bare channel name. Use the collision-suffixed channel.
        decision = _decide_collision(
            this_event_id="2026-04-25_mpd_calaveras_1430",
            existing_channel_owner_event_id="2026-04-25_mpd_calaveras_0930",
        )
        assert decision == "collide"

    def test_decision_strings_are_only_reuse_or_collide(self):
        # Belt-and-braces — every input combination returns one of the two
        # known strings. A future PR that introduces a third state would
        # need a corresponding test class added here.
        for owner in (None, "x", "y"):
            decision = _decide_collision(
                this_event_id="x",
                existing_channel_owner_event_id=owner,
            )
            assert decision in ("reuse", "collide"), \
                f"unexpected decision {decision!r} for owner={owner!r}"


# ---------------------------------------------------------------------------
# _compose_active_incidents_tally()
# ---------------------------------------------------------------------------

class TestComposeActiveIncidentsTally:
    def test_initial_post_with_no_responders(self):
        # /send-notification step 10 — initial tally has no responders yet.
        # The polling chain fills them in over time.
        doc = {
            "event_name_human": "2026-04-25 MPD CALAVERAS",
            "responders": [],
            "last_non_empty_responders": [],
        }
        out = _compose_active_incidents_tally(doc, "🔔 Everbridge ACTIVE")
        assert "*🔔 Everbridge ACTIVE — 2026-04-25 MPD CALAVERAS*" in out
        assert "✅ 0 confirmed" in out
        # No per-group or multi-team lines.
        assert "•" not in out
        assert "↳" not in out
        assert "🔸" not in out

    def test_safe_mode_draft_header(self):
        # Initial post for safe-mode draft path uses a different header —
        # tally is "📋 Awaiting dispatcher send" until /confirm-draft-sent
        # or auto-discovery flips it to ACTIVE.
        doc = {
            "event_name_human": "2026-04-25 MPD CALAVERAS",
            "responders": [],
            "last_non_empty_responders": [],
        }
        out = _compose_active_incidents_tally(doc, "📋 Awaiting dispatcher send")
        assert "*📋 Awaiting dispatcher send — 2026-04-25 MPD CALAVERAS*" in out

    def test_single_group_no_multi_team(self):
        doc = {
            "event_name_human": "2026-04-25 MPD CALAVERAS",
            "responders": [
                {"name": "Burns", "groups": ["K9"]},
                {"name": "Black", "groups": ["K9"]},
            ],
        }
        out = _compose_active_incidents_tally(doc, "🔔 Everbridge ACTIVE")
        assert "*🔔 Everbridge ACTIVE — 2026-04-25 MPD CALAVERAS*" in out
        assert "✅ 2 confirmed" in out
        # Names sorted alphabetically per group line for stable rendering.
        assert "• K9 (2): Black, Burns" in out
        assert "↳" not in out
        assert "🔸" not in out      # Item 6 — must NEVER recur

    def test_multiple_groups_sorted_alphabetically(self):
        # Group lines should appear in alphabetical group-name order so
        # successive edits produce stable diffs in the Slack message
        # history (which dispatchers sometimes scroll back through).
        doc = {
            "event_name_human": "X",
            "responders": [
                {"name": "Burns", "groups": ["UAS"]},
                {"name": "Lee",   "groups": ["Drivers"]},
                {"name": "Black", "groups": ["K9"]},
            ],
        }
        out = _compose_active_incidents_tally(doc, "H")
        # Drivers, K9, UAS — alphabetical
        idx_drivers = out.index("Drivers")
        idx_k9      = out.index("K9")
        idx_uas     = out.index("UAS")
        assert idx_drivers < idx_k9 < idx_uas

    def test_multi_team_uses_indent_arrow(self):
        # Item 6 regression — verifies _format_tally_multi_team_line is
        # invoked for responders in 2+ groups.
        doc = {
            "event_name_human": "2026-04-25 MPD CALAVERAS",
            "responders": [
                {"name": "Burns", "groups": ["K9", "Drivers"]},
            ],
        }
        out = _compose_active_incidents_tally(doc, "🔔 Everbridge ACTIVE")
        # Multi-team line uses the ↳ indent (Item 6 — replaced PoC's 🔸 emoji
        # which Slack rendered too prominently on big incidents)
        assert "  ↳ Burns: Drivers, K9" in out
        assert "🔸" not in out

    def test_multi_team_only_emitted_for_responders_in_multiple_groups(self):
        # Single-group responders MUST NOT generate a ↳ line.
        doc = {
            "event_name_human": "X",
            "responders": [
                {"name": "Burns", "groups": ["K9", "Drivers"]},     # multi-team
                {"name": "Black", "groups": ["K9"]},                # single
            ],
        }
        out = _compose_active_incidents_tally(doc, "H")
        assert "↳ Burns" in out
        # No ↳ line for Black
        for line in out.splitlines():
            if "↳" in line:
                assert "Black" not in line

    def test_multi_team_lines_sorted_alphabetically(self):
        # Stable rendering — same reason group lines are sorted.
        doc = {
            "event_name_human": "X",
            "responders": [
                {"name": "Burns", "groups": ["K9", "Drivers"]},
                {"name": "Black", "groups": ["UAS", "Drivers"]},
            ],
        }
        out = _compose_active_incidents_tally(doc, "H")
        idx_black = out.index("↳ Black")
        idx_burns = out.index("↳ Burns")
        assert idx_black < idx_burns

    def test_uses_last_non_empty_responders_when_present(self):
        # Phase 0 Task 8 cache regression — terminal state with empty
        # `responders` must use the cached `last_non_empty_responders` so
        # the final tally shows the correct YES count.
        doc = {
            "event_name_human": "X",
            "responders": [],
            "last_non_empty_responders": [{"name": "Burns", "groups": ["K9"]}],
        }
        out = _compose_active_incidents_tally(doc, "⏹ Everbridge STOPPED — Completed")
        assert "✅ 1 confirmed" in out
        assert "• K9 (1): Burns" in out

    def test_prefers_last_non_empty_over_responders_when_both_set(self):
        # Live state — both fields populated. last_non_empty_responders
        # wins (it's the cache that the poll handler maintains; responders
        # is the per-cycle scratch). The two will normally agree, but if
        # they disagree the cache is authoritative for the rendered tally.
        doc = {
            "event_name_human": "X",
            "responders":                [{"name": "Black", "groups": ["UAS"]}],
            "last_non_empty_responders": [{"name": "Burns", "groups": ["K9"]}],
        }
        out = _compose_active_incidents_tally(doc, "H")
        assert "• K9 (1): Burns" in out
        assert "Black" not in out

    def test_falls_back_to_responders_when_cache_empty(self):
        # Initial-post case from /send-notification — neither field is
        # populated yet but `responders` is at least a defined empty list.
        # Verified to render N=0 cleanly.
        doc = {
            "event_name_human": "X",
            "responders": [{"name": "Burns", "groups": ["K9"]}],
            # no last_non_empty_responders key
        }
        out = _compose_active_incidents_tally(doc, "H")
        assert "✅ 1 confirmed" in out
        assert "• K9 (1): Burns" in out

    def test_responder_with_no_groups_appears_in_count_but_no_group_line(self):
        # Edge case — a responder whose group membership is unknown still
        # contributes to the headline count but doesn't produce a
        # per-group line. Defensive against partial PoC data.
        doc = {
            "event_name_human": "X",
            "responders": [
                {"name": "Burns", "groups": []},
                {"name": "Black", "groups": ["K9"]},
            ],
        }
        out = _compose_active_incidents_tally(doc, "H")
        assert "✅ 2 confirmed" in out
        assert "K9 (1): Black" in out
        # Burns (no groups) contributes to the confirmed count but produces no
        # group line — and the count line is numeric only, so the name Burns
        # must not appear anywhere in the rendered tally.
        assert "Burns" not in out

    def test_terminal_stopped_header(self):
        doc = {
            "event_name_human": "X",
            "responders": [],
            "last_non_empty_responders": [{"name": "Burns", "groups": ["K9"]}],
        }
        out = _compose_active_incidents_tally(doc, "⏹ Everbridge STOPPED — idle")
        assert "*⏹ Everbridge STOPPED — idle — X*" in out
        assert "✅ 1 confirmed" in out

    def test_tri_count_line_renders_decline_and_no_response(self):
        # Issue #592 — the tri-count line surfaces the explicit-NO count (the
        # mutual-aid go/no-go signal) alongside confirmed + no-response.
        # Numbers are the real La Verne MA callout (1 YES / 50 NO / 17 no-resp).
        doc = {
            "event_name_human": "2026-07-18 LACSO LA VERNE",
            "responders": [{"name": "Burns", "groups": ["All Members"]}],
            "decline_count": 50,
            "no_response_count": 17,
        }
        out = _compose_active_incidents_tally(doc, "🔔 Everbridge ACTIVE")
        assert "*✅ 1 confirmed   ❌ 50 declined   ⏳ 17 no response*" in out

    def test_tri_count_defaults_to_zero_when_fields_absent(self):
        # Initial send-time post and pre-#592 Firestore docs carry no
        # decline_count / no_response_count — both must default to 0.
        doc = {"event_name_human": "X", "responders": []}
        out = _compose_active_incidents_tally(doc, "🔔 Everbridge ACTIVE")
        assert "*✅ 0 confirmed   ❌ 0 declined   ⏳ 0 no response*" in out


# ---------------------------------------------------------------------------
# D4H Phase 2 PR 5 — /send-notification body contract
# ---------------------------------------------------------------------------
# The frontend forwards `ocr_text` (= _rawOcrText) and `map_data` (= _rawMapData)
# in the POST body so the backend can build the structured ocr_data dict that
# d4h.create_incident_with_subject expects.
#
# These tests pin the read pattern that lives in main.py at /send-notification
# body-parse. The mirror IS the contract — if main.py drifts away from
# `body.get("ocr_text", "") or ""` / `body.get("map_data", {}) or {}`, this
# test surfaces the drift.

def _extract_d4h_phase2_fields(body: dict) -> tuple[str, dict]:
    """Mirror of the body-parse pattern in main.py /send-notification.
    Reads ocr_text + map_data with safe defaults."""
    ocr_text = body.get("ocr_text", "") or ""
    map_data = body.get("map_data", {}) or {}
    return ocr_text, map_data


class TestSendNotificationD4HPhase2BodyFields:
    def test_typical_dispatch_body_with_textarea_and_map_data(self):
        body = {
            "ocr_text": "Event Name: 2026-05-15 SJPD Tradan\nLast Known Position: 1000 Tradan Dr",
            "map_data": {
                "lkp": {"lat": 37.40087, "lng": -121.88387, "label": "LKP — 1000 Tradan Dr"},
                "event_name": "2026-05-15 SJPD Tradan",
            },
        }
        ocr_text, map_data = _extract_d4h_phase2_fields(body)
        assert ocr_text.startswith("Event Name: 2026-05-15")
        assert map_data["lkp"]["lat"] == 37.40087
        assert map_data["event_name"] == "2026-05-15 SJPD Tradan"

    def test_missing_keys_default_to_safe_empty(self):
        """Backward-compat — if the frontend somehow omits these fields
        (e.g., during a partial rollout), the dispatch must not crash."""
        ocr_text, map_data = _extract_d4h_phase2_fields({})
        assert ocr_text == ""
        assert map_data == {}

    def test_explicit_null_values_default_to_safe_empty(self):
        """Defensive against `null` / `None` slipping through frontend
        nullability. The `or ""` / `or {}` fallbacks must catch them."""
        ocr_text, map_data = _extract_d4h_phase2_fields({"ocr_text": None, "map_data": None})
        assert ocr_text == ""
        assert map_data == {}


# ---------------------------------------------------------------------------
# D4H Phase 2 PR 5 Task 5.2 — _build_ocr_data_for_d4h() textarea re-parser
# ---------------------------------------------------------------------------
# Mirror of the production helper in main.py. Re-parses the dispatcher-edited
# textarea + structured map_data into the ocr_data dict that
# d4h.create_incident_with_subject expects.
#
# Mirror policy: identical pure-logic to main.py. When updating either, update
# both. The mirror IS the contract.

import re as _re_d4h

# Regex patterns mirrored from main.py — extract structured fields from the
# Initial Incident Summary section of the textarea.
_D4H_RE_LKP_FULL    = _re_d4h.compile(r"^Last Known Position:\s*(.+)$", _re_d4h.MULTILINE)
_D4H_RE_DOB         = _re_d4h.compile(r"^DOB:\s*([^(\n]+?)\s*(?:\((\d+)\s*[^)]*\))?\s*$", _re_d4h.MULTILINE)
_D4H_RE_MP_NAME     = _re_d4h.compile(r"^Missing Person:\s*([^;\n]+?)(?:\s*;|\s*$)", _re_d4h.MULTILINE)
_D4H_RE_AT_RISK     = _re_d4h.compile(r"^Missing Person:.*?;\s*at-risk:\s*([^\n]+)$", _re_d4h.MULTILINE)
_D4H_RE_CONTACT     = _re_d4h.compile(r"^Contact:\s*(.+)$", _re_d4h.MULTILINE)
_D4H_RE_EVENT_NUM   = _re_d4h.compile(r"^Event #:\s*(.+)$", _re_d4h.MULTILINE)
_D4H_RE_EVENT_NUM_PLACEHOLDER = _re_d4h.compile(
    r"^(?:\[.*\]|not\s+recorded|not\s+provided|unknown|n/?a)$",
    _re_d4h.IGNORECASE,
)
_D4H_RE_LPB_LINE    = _re_d4h.compile(r"^Q(\d{1,2})\s*-\s*([^\-\n]+?)\s*-\s*([^\n]+)$", _re_d4h.MULTILINE)
_D4H_RE_KOESTER     = _re_d4h.compile(
    r"^LPB Range Ring Analysis[^\n]*:\n+(.+?)(?=\n---\n|\Z)",
    _re_d4h.MULTILINE | _re_d4h.DOTALL,
)

# Two-summary-layout markers — mirror of main.py.
_D4H_RE_FULL_SECTION_OPEN  = _re_d4h.compile(r"━━+\s*FULL\s+INCIDENT\s+SUMMARY\s*━━+\s*\n", _re_d4h.IGNORECASE)
_D4H_RE_CLOSING_DIVIDER    = _re_d4h.compile(r"\n━━+\s*$")
_D4H_RE_INITIAL_SUMMARY_H  = _re_d4h.compile(r"^Initial Incident Summary:\s*\n")

# Event Log section delimiter regex — mirror of main.py.
_D4H_RE_EVENT_LOG_BLOCK = _re_d4h.compile(
    r"(^Event Log:\s*\n(?:.+\n)*?)(^---\s*$)",
    _re_d4h.MULTILINE
)


def _inject_dispatch_milestones_into_event_log(text: str, milestones: list) -> str:
    """Mirror of main.py._inject_dispatch_milestones_into_event_log.

    Insert dispatch-time milestone lines just before the closing `---` of
    the Event Log section in the IIS body. Pre-formatted timestamped strings.

    Fallback: text returned unchanged when no Event Log section exists.
    """
    if not text or not milestones:
        return text
    addition = "\n".join(milestones) + "\n"
    def _sub(m):
        return f"{m.group(1)}{addition}{m.group(2)}"
    return _D4H_RE_EVENT_LOG_BLOCK.sub(_sub, text, count=1)


def _extract_iis_body_for_d4h(text: str) -> str:
    """Mirror of main.py._extract_iis_body_for_d4h.

    Extract the canonical IIS body for the D4H description field.
    Drops the `━━━ WHATSAPP DISPATCH ━━━` block, the `━━━ FULL INCIDENT
    SUMMARY ━━━` section markers, and the leading `Initial Incident Summary:`
    title so the body starts directly with `Event Name:`.

    Fallback for missing marker: returns the text trimmed-as-is.
    """
    if not text:
        return ""
    m = _D4H_RE_FULL_SECTION_OPEN.search(text)
    if not m:
        return text.strip()
    body = text[m.end():].rstrip()
    body = _D4H_RE_CLOSING_DIVIDER.sub("", body)
    body = _D4H_RE_INITIAL_SUMMARY_H.sub("", body, count=1)
    return body.strip()


def _split_officer_contact(contact_line: str) -> tuple[str, str]:
    """Split 'John Smith 408-555-1234' into ('John Smith', '408-555-1234').
    Heuristic: phone is the trailing whitespace-delimited token that contains
    a digit; everything before is the name. Returns ('', '') for empty input.
    """
    contact_line = (contact_line or "").strip()
    if not contact_line:
        return "", ""
    # Phone heuristic — find a trailing token with at least 3 digits
    m = _re_d4h.match(r"^(.+?)\s+([\d\-\(\)\.\s]+\d[\d\-\(\)\.\s]*)$", contact_line)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return contact_line, ""


def _build_ocr_data_for_d4h(*, ocr_text: str, map_data: dict,
                            event_name_human: str = "",
                            fallback_mp_at_risk: str = "",
                            fallback_mp_full_name: str = "") -> dict:
    """Re-parse the dispatcher-edited textarea + structured map_data into the
    ocr_data dict d4h.create_incident_with_subject expects.

    The textarea is the source of truth — dispatcher edits land here.
    map_data supplies geocoded coords that aren't reliably extractable from
    textarea prose.

    event_name_human is the live textarea `Event Name:` value (issue #87).
    map_data["event_name"] is frozen at OCR time; the fallback exists only
    for direct callers that omit event_name_human.

    Comma-splits the at-risk segment so each indicator becomes its own bullet
    in the D4H INVOLVED tab. fallback_mp_at_risk / fallback_mp_full_name are
    used only when the textarea parse yields nothing (extra safety — the
    /send-notification payload also carries flat mp_at_risk and mp_name).

    Safe defaults on missing/empty input — never raises. Downstream
    d4h.create_incident_with_subject may itself fail on missing required
    keys (e.g., empty event_name or mp_full_name), which is caught at the
    call site.
    """
    ocr_text = ocr_text or ""
    map_data = map_data or {}
    lkp = map_data.get("lkp") or {}

    # ---- MP full name — required by D4H involved-person POST (spike 06:159).
    mp_full_name = ""
    nm = _D4H_RE_MP_NAME.search(ocr_text)
    if nm:
        mp_full_name = nm.group(1).strip()
    if not mp_full_name:
        mp_full_name = (fallback_mp_full_name or "").strip()

    # ---- LKP address (from textarea — dispatcher edits flow through) ----
    lkp_address = ""
    m = _D4H_RE_LKP_FULL.search(ocr_text)
    if m:
        lkp_address = m.group(1).strip()

    # ---- Event name (from map_data; preserves CalTopo-built canonical form) ----
    event_name = (event_name_human or map_data.get("event_name") or "").strip()

    # ---- LKP coords ----
    try:
        lkp_lat = float(lkp.get("lat", 0.0) or 0.0)
    except (TypeError, ValueError):
        lkp_lat = 0.0
    try:
        lkp_lng = float(lkp.get("lng", 0.0) or 0.0)
    except (TypeError, ValueError):
        lkp_lng = 0.0

    # ---- DOB + age (from "DOB: [date] (NN years old)" line) ----
    mp_dob = ""
    mp_age: object = None
    dm = _D4H_RE_DOB.search(ocr_text)
    if dm:
        mp_dob = (dm.group(1) or "").strip()
        if dm.group(2):
            try:
                mp_age = int(dm.group(2))
            except (TypeError, ValueError):
                mp_age = None

    # ---- at-risk indicators (comma-split per Locked Decision PR 5) ----
    raw_at_risk = ""
    am = _D4H_RE_AT_RISK.search(ocr_text)
    if am:
        raw_at_risk = am.group(1).strip()
    if not raw_at_risk:
        raw_at_risk = (fallback_mp_at_risk or "").strip()
    at_risk_indicators = [x.strip() for x in raw_at_risk.split(",") if x.strip()]

    # ---- Officer contact (Contact: <name> <phone>) ----
    officer_name = ""
    officer_phone = ""
    cm = _D4H_RE_CONTACT.search(ocr_text)
    if cm:
        officer_name, officer_phone = _split_officer_contact(cm.group(1))

    # ---- Q1-Q12 LPB questionnaire ----
    # Format per Locked Decision: "Q# - ANSWER - QUESTION"
    qn: dict[str, str] = {}
    q1 = ""
    q9 = ""
    for line_m in _D4H_RE_LPB_LINE.finditer(ocr_text):
        n = line_m.group(1)
        answer = line_m.group(2).strip()
        question = line_m.group(3).strip()
        qn[f"q{n}_question"] = question
        qn[f"q{n}_answer"] = answer
        if n == "1":
            # Q1 = "Familiar with area" — strip "NOT ANSWERED" parenthetical
            # to leave bare "Yes" / "No"
            q1 = answer.split(" ")[0]
        if n == "9":
            # Q9 = "Mental health component" — d4h.py maps "Yes" → INTENTIONAL_SELF cause
            q9 = answer.split(" ")[0]
    qn["q1_familiar_with_area"] = q1
    qn["q9_intentional_self_harm"] = q9

    # ---- Koester narrative ----
    koester_narrative = ""
    km = _D4H_RE_KOESTER.search(ocr_text)
    if km:
        koester_narrative = km.group(1).strip()

    # ---- Agency Event # (#676) ----
    event_number = ""
    em = _D4H_RE_EVENT_NUM.search(ocr_text)
    if em:
        candidate = em.group(1).strip()
        if (candidate
                and any(ch.isdigit() for ch in candidate)
                and not _D4H_RE_EVENT_NUM_PLACEHOLDER.match(candidate)):
            event_number = candidate

    return {
        "event_name":                  event_name,
        "event_number":                event_number,
        "mp_full_name":                mp_full_name,
        "lkp_lat":                     lkp_lat,
        "lkp_lng":                     lkp_lng,
        "lkp_address":                 lkp_address,
        "mp_dob":                      mp_dob,
        "mp_age":                      mp_age,
        "mp_sex":                      "",   # not exposed in current textarea output
        "officer_name":                officer_name,
        "officer_phone":               officer_phone,
        "at_risk_indicators":          at_risk_indicators,
        "koester_narrative":           koester_narrative,
        # Canonical IIS body for D4H description — see main.py mirror.
        "full_summary":                _extract_iis_body_for_d4h(ocr_text),
        **qn,
    }


# Sample textarea — minimal but realistic, mirrors what /ocr produces.
_SAMPLE_TEXTAREA = """\
Initial Incident Summary:

Event Name: 2026-05-15 SJPD Tradan
Event #: 26-12345
Agency: SJPD
Contact: John Smith 408-555-1234
Missing Person: Doe, Jane; at-risk: diabetic, no insulin, low temp
DOB: 06/15/1980 (45 years old)
Last Seen At: 5/14/26 2300
Last Known Position: 1000 Tradan Dr, San Jose, CA
Residence Address: same
Last Seen Wearing: red jacket, jeans
Staging Area for Resources: 1000 Tradan Dr, San Jose
CalTopo Map ID:
Dispatcher:

---

Event Log:
v1 Intake form processed; Initial Incident Summary created

---

LPB Questionnaire:
Q1 - Yes - Familiar with area
Q2 - Yes - Has phone — number: 408-555-9999
Q3 - No - Uses public transit (VTA)
Q9 - Yes - Mental health component — detail: dementia
Q10 - No - Prior missing
Q11 - No - Hospitals checked
Q12 - No - Surveillance cameras / CCTV

---

LPB Range Ring Analysis (Robert Koester — "Lost Person Behavior"):
Category: Dementia. 50% containment at 1.2 mi (1.9 km).

---
"""


class TestEventNumberMirrorParity:
    """#676 — every test in TestBuildOcrDataForD4H runs against the mirror
    above, so reverting main.py alone would leave them all green. These read
    production source instead.

    main.py cannot be imported in the local pytest env, so the pins are
    source-text. Imports are function-local to match the file's style.
    """

    @staticmethod
    def _main_source() -> str:
        from pathlib import Path
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    def test_production_regexes_match_the_mirror(self):
        src = self._main_source()
        assert r'_D4H_RE_EVENT_NUM   = re.compile(r"^Event #:\s*(.+)$", re.MULTILINE)' in src, (
            "_D4H_RE_EVENT_NUM drifted from the mirror in this file — the "
            "Event # tests above are exercising a stale copy"
        )
        assert "_D4H_RE_EVENT_NUM_PLACEHOLDER" in src, (
            "the placeholder filter is gone from main.py — '[not recorded]' "
            "would reach D4H's trackingNumber as a literal agency reference"
        )
        assert r'r"^(?:\[.*\]|not\s+recorded|not\s+provided|unknown|n/?a)$"' in src, (
            "the placeholder pattern drifted from the mirror in this file"
        )

    def test_production_returns_event_number(self):
        """The key must be in the returned dict, or d4h.py's
        ocr_data.get("event_number", "") silently reads "" forever and
        trackingNumber is omitted on every dispatch."""
        src = self._main_source()
        start = src.find("def _build_ocr_data_for_d4h(")
        assert start != -1, "_build_ocr_data_for_d4h not found in main.py"
        # Bound on the next top-level decorator, not a char count.
        end = src.find('\n@app.post("/send-notification")', start)
        assert end != -1 and end > start, "could not bound _build_ocr_data_for_d4h"
        body = src[start:end]
        assert '"event_number":' in body, (
            "_build_ocr_data_for_d4h no longer returns event_number — D4H's "
            "trackingNumber goes back to being omitted on every dispatch (#676)"
        )
        assert "_D4H_RE_EVENT_NUM.search(ocr_text)" in body, (
            "the Event # is no longer parsed from the textarea — a dispatcher "
            "correcting a misread number would not reach D4H (same failure "
            "shape as issue #87 for event_name)"
        )
        # Existence of the pattern is not the same as applying it: assert the
        # guard runs INSIDE the parse. Caught by mutation-testing this pin —
        # the version that only checked module-level presence passed while
        # production accepted "[not recorded]" as a real agency reference.
        assert "_D4H_RE_EVENT_NUM_PLACEHOLDER.match(candidate)" in body, (
            "the placeholder guard is declared but no longer applied — "
            "'[not recorded]' would be filed as the agency's incident number"
        )
        assert "any(ch.isdigit() for ch in candidate)" in body, (
            "the digit shape test is gone — free-texted prose from the OCR "
            "path would be filed as the agency's incident number"
        )


class TestBuildOcrDataForD4H:
    def test_extracts_event_name_from_map_data(self):
        result = _build_ocr_data_for_d4h(
            ocr_text="",
            map_data={"event_name": "2026-05-15 SJPD Tradan"},
        )
        assert result["event_name"] == "2026-05-15 SJPD Tradan"

    # -- issue #676: the agency Event # reaches D4H's trackingNumber --------
    def test_extracts_agency_event_number_from_textarea(self):
        """Both intake paths have always written this line — pdf_extract.py
        from the AcroForm `event_number` field, gemini.py from the form's
        `Event #:` box — and nothing downstream ever read it."""
        result = _build_ocr_data_for_d4h(
            ocr_text=_SAMPLE_TEXTAREA,
            map_data={"event_name": "2026-05-15 SJPD Tradan"},
        )
        assert result["event_number"] == "26-12345"

    def test_event_number_read_from_textarea_not_map_data(self):
        """Same reasoning as issue #87 for event_name: the textarea is the
        source of truth, so a dispatcher correcting a misread Event # gets
        the correction into D4H."""
        edited = _SAMPLE_TEXTAREA.replace("Event #: 26-12345",
                                          "Event #: 2026-LAW-54902292")
        result = _build_ocr_data_for_d4h(
            ocr_text=edited,
            map_data={"event_name": "2026-05-15 SJPD Tradan",
                      "event_number": "26-12345"},
        )
        assert result["event_number"] == "2026-LAW-54902292"

    def test_placeholder_event_number_reads_as_absent(self):
        """pdf_extract.py writes "[not recorded]" for a blank AcroForm field
        and Gemini can echo its own "[from form]" prompt token. A bracketed
        value is never a real agency number, so the bracket shape is the
        test rather than an enumeration of placeholder strings."""
        for placeholder in ("[not recorded]", "[from form]", "[Not Recorded]",
                            "unknown", "N/A"):
            edited = _SAMPLE_TEXTAREA.replace("Event #: 26-12345",
                                              f"Event #: {placeholder}")
            result = _build_ocr_data_for_d4h(
                ocr_text=edited,
                map_data={"event_name": "2026-05-15 SJPD Tradan"},
            )
            assert result["event_number"] == "", f"placeholder={placeholder!r}"

    def test_digit_free_prose_is_rejected(self):
        """gemini.py's prompt gives this field no explicit "if not on form"
        fallback (unlike Residence Address), so Gemini free-texting an
        apology is unspecified upstream behaviour. Every real agency number
        carries digits; prose does not. Raised by code review on #676."""
        for prose in ("Not visible on form", "none given", "See attached",
                      "Not provided by agency"):
            edited = _SAMPLE_TEXTAREA.replace("Event #: 26-12345",
                                              f"Event #: {prose}")
            result = _build_ocr_data_for_d4h(
                ocr_text=edited,
                map_data={"event_name": "2026-05-15 SJPD Tradan"},
            )
            assert result["event_number"] == "", f"prose={prose!r}"

    def test_digit_bearing_numbers_still_pass(self):
        """The guard must not reject the real formats. Both of these are
        verbatim from real intake forms."""
        for real in ("26-212-071", "2026-LAW-54902292", "26-00193", "SO 25-1194"):
            edited = _SAMPLE_TEXTAREA.replace("Event #: 26-12345",
                                              f"Event #: {real}")
            result = _build_ocr_data_for_d4h(
                ocr_text=edited,
                map_data={"event_name": "2026-05-15 SJPD Tradan"},
            )
            assert result["event_number"] == real

    def test_missing_event_number_line_is_empty_not_an_error(self):
        result = _build_ocr_data_for_d4h(
            ocr_text="Event Name: 2026-05-15 SJPD Tradan\n",
            map_data={"event_name": "2026-05-15 SJPD Tradan"},
        )
        assert result["event_number"] == ""

    def test_event_number_never_falls_back_to_the_event_name(self):
        """The whole point of #676. An absent Event # must produce an absent
        trackingNumber, not our event name wearing the agency's field."""
        result = _build_ocr_data_for_d4h(
            ocr_text="",
            map_data={"event_name": "2026-05-15 SJPD Tradan"},
            event_name_human="2026-05-15 SJPD Tradan",
        )
        assert result["event_number"] == ""
        assert result["event_name"] == "2026-05-15 SJPD Tradan"

    # -- issue #87: dispatcher Event Name edits must reach D4H --------------
    def test_event_name_human_wins_over_stale_map_data(self):
        """The 2026-07-24 failure: dispatcher corrects the Event Name in the
        textarea, /create-map picks it up via freshMapData, but D4H kept the
        OCR-time value from map_data and filed the pre-edit name."""
        result = _build_ocr_data_for_d4h(
            ocr_text="",
            map_data={"event_name": "2026-05-15 SJPD Tradan"},
            event_name_human="2026-05-15 SCPD Moreland",
        )
        assert result["event_name"] == "2026-05-15 SCPD Moreland"

    def test_event_name_falls_back_to_map_data_when_human_blank(self):
        result = _build_ocr_data_for_d4h(
            ocr_text="",
            map_data={"event_name": "2026-05-15 SJPD Tradan"},
            event_name_human="",
        )
        assert result["event_name"] == "2026-05-15 SJPD Tradan"

    def test_event_name_human_used_when_map_data_has_none(self):
        result = _build_ocr_data_for_d4h(
            ocr_text="",
            map_data={},
            event_name_human="2026-05-15 SCPD Moreland",
        )
        assert result["event_name"] == "2026-05-15 SCPD Moreland"

    def test_at_risk_indicators_comma_split(self):
        result = _build_ocr_data_for_d4h(ocr_text=_SAMPLE_TEXTAREA, map_data={})
        assert result["at_risk_indicators"] == ["diabetic", "no insulin", "low temp"]

    def test_at_risk_empty_yields_empty_list(self):
        result = _build_ocr_data_for_d4h(ocr_text="", map_data={})
        assert result["at_risk_indicators"] == []

    def test_at_risk_fallback_used_when_textarea_silent(self):
        """Defense in depth — if dispatcher deleted the Missing Person line
        but the flat mp_at_risk field still carries the value, use it."""
        result = _build_ocr_data_for_d4h(
            ocr_text="",
            map_data={},
            fallback_mp_at_risk="diabetic, alone",
        )
        assert result["at_risk_indicators"] == ["diabetic", "alone"]

    def test_mp_full_name_extracted_from_missing_person_line(self):
        """Spike 06:159 — D4H REQUIRES the "name" field on involved-person POST.
        Source is the "Missing Person: <name>; at-risk: ..." line of the textarea."""
        result = _build_ocr_data_for_d4h(ocr_text=_SAMPLE_TEXTAREA, map_data={})
        assert result["mp_full_name"] == "Doe, Jane"

    def test_mp_full_name_handles_no_semicolon(self):
        """If the dispatcher edited the textarea to remove the "; at-risk: ..."
        suffix, the regex must still capture the full name to end-of-line."""
        textarea = "Missing Person: Smith, John\n"
        result = _build_ocr_data_for_d4h(ocr_text=textarea, map_data={})
        assert result["mp_full_name"] == "Smith, John"

    def test_mp_full_name_fallback_used_when_textarea_silent(self):
        """If the textarea Missing Person line is missing, fall back to the
        flat mp_name field from the /send-notification POST body."""
        result = _build_ocr_data_for_d4h(
            ocr_text="",
            map_data={},
            fallback_mp_full_name="Backup, Name",
        )
        assert result["mp_full_name"] == "Backup, Name"

    def test_mp_full_name_textarea_wins_over_fallback(self):
        """Textarea-as-source-of-truth — dispatcher edit takes precedence."""
        result = _build_ocr_data_for_d4h(
            ocr_text="Missing Person: Edit, Dispatcher; at-risk: x\n",
            map_data={},
            fallback_mp_full_name="Should, NotBeUsed",
        )
        assert result["mp_full_name"] == "Edit, Dispatcher"

    def test_lkp_address_from_textarea(self):
        result = _build_ocr_data_for_d4h(ocr_text=_SAMPLE_TEXTAREA, map_data={})
        assert result["lkp_address"] == "1000 Tradan Dr, San Jose, CA"

    def test_full_summary_fallback_when_no_section_marker(self):
        """Per Bill 2026-05-20 — when the textarea lacks the `━━━ FULL
        INCIDENT SUMMARY ━━━` marker (older format or test fixture), the
        full_summary falls back to the trimmed-as-is text. The fixture
        textarea above is intentionally minimal and has no two-summary
        layout markers, so the fallback path applies."""
        result = _build_ocr_data_for_d4h(ocr_text=_SAMPLE_TEXTAREA, map_data={})
        # Fixture has no `━━━ FULL` marker — fallback returns trimmed text
        assert result["full_summary"] == _SAMPLE_TEXTAREA.strip()

    def test_full_summary_empty_when_textarea_empty(self):
        """Empty textarea yields empty full_summary — d4h.py treats empty
        identically to missing (no blank-line + body block in description)."""
        result = _build_ocr_data_for_d4h(ocr_text="", map_data={})
        assert result["full_summary"] == ""


# ---------------------------------------------------------------------------
# _extract_iis_body_for_d4h() — two-summary layout extraction
# ---------------------------------------------------------------------------
# Mirror tests for the section-marker stripping logic. Per Bill 2026-05-20,
# the D4H description should carry only the FULL INCIDENT SUMMARY block,
# starting at "Event Name:" — no WhatsApp dispatch duplicate, no section
# markers, no "Initial Incident Summary:" header.

_TWO_SUMMARY_TEXTAREA = """\
━━━ WHATSAPP DISPATCH ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Initial Incident Summary:
Event Name: 2026-02-20 MILPITAS Calaveras
Missing Person: TORRES; at-risk: alone, age
DOB: 8/17/12 (13 years old)
Staging Area for Resources: 2 South Park Victoria Drive
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━ FULL INCIDENT SUMMARY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Initial Incident Summary:
Event Name: 2026-02-20 MILPITAS Calaveras
Event #: 26-051-009
Agency: MILPITAS
Missing Person: TORRES; at-risk: alone, age
Last Known Position: 1200 East Calaveras Blvd, Milpitas, CA 95035

LPB Questionnaire:
Q1 - No - Familiar with area
Q9 - NOT ANSWERED - Mental health component
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


class TestExtractIISBodyForD4H:
    def test_strips_whatsapp_section(self):
        """WhatsApp Dispatch block is mobile-copy convenience that
        duplicates the Full Incident Summary — D4H should never see it."""
        body = _extract_iis_body_for_d4h(_TWO_SUMMARY_TEXTAREA)
        assert "━━━ WHATSAPP DISPATCH" not in body
        # The WhatsApp section's distinctive "Staging Area for Resources"
        # line should be absent (it's not in the Full section above)
        assert "Staging Area for Resources" not in body

    def test_strips_section_markers(self):
        """All `━━━` divider lines stripped from the result."""
        body = _extract_iis_body_for_d4h(_TWO_SUMMARY_TEXTAREA)
        assert "━" not in body

    def test_strips_initial_summary_header(self):
        """The 'Initial Incident Summary:' title line is dropped so the body
        starts directly with 'Event Name:' (per Bill 2026-05-20 — dispatcher
        deletes the TODO block and the field then begins with our canonical
        format)."""
        body = _extract_iis_body_for_d4h(_TWO_SUMMARY_TEXTAREA)
        assert not body.startswith("Initial Incident Summary:")
        assert body.startswith("Event Name: 2026-02-20 MILPITAS Calaveras")

    def test_preserves_full_section_content(self):
        """The Full section content (LPB Q&A, agency, event #, etc.) is
        preserved verbatim — only the surrounding markers + header drop."""
        body = _extract_iis_body_for_d4h(_TWO_SUMMARY_TEXTAREA)
        assert "Event #: 26-051-009" in body
        assert "Agency: MILPITAS" in body
        assert "Last Known Position: 1200 East Calaveras Blvd" in body
        assert "Q1 - No - Familiar with area" in body
        assert "Q9 - NOT ANSWERED - Mental health component" in body

    def test_fallback_when_marker_missing(self):
        """Backward-compat — textarea without the `━━━ FULL` marker returns
        as trimmed text. Used by older fixtures + by mutual-aid forms that
        skip the two-summary layout."""
        raw = "Event Name: 2026-05-20 SJPD Foo\nLPB Q1 - No - Familiar"
        body = _extract_iis_body_for_d4h(raw)
        assert body == raw

    def test_fallback_when_empty(self):
        assert _extract_iis_body_for_d4h("") == ""
        assert _extract_iis_body_for_d4h(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _inject_dispatch_milestones_into_event_log()
# ---------------------------------------------------------------------------
# Mirror tests for the dispatch-milestone injector. Per Bill 2026-05-19 live
# test: D4H's incident description was missing 3 event-log entries (EB sent,
# Slack channel created, D4H incident filed) because those entries were added
# to the textarea AFTER the D4H POST returned. The injector closes the gap by
# inserting them into the textarea content BEFORE D4H sees it.

_EVENT_LOG_FIXTURE = """\
━━━ FULL INCIDENT SUMMARY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Initial Incident Summary:
Event Name: 2026-02-01 MILPITAS Calaveras
Last Known Position: 1200 East Calaveras Blvd, Milpitas, CA 95035

---
Event Log:
2026-02-01 21:30 - Request received from MILPITAS/SGT TOTH
2026-05-19 11:31 - v1 Intake form processed; Initial Incident Summary created
2026-05-19 11:36 - CalTopo map created: https://caltopo.com/m/C629TD8
---
LPB Questionnaire:
Q1 - No - Familiar with area
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


class TestInjectDispatchMilestonesIntoEventLog:
    def test_inserts_before_closing_divider(self):
        """The classic happy path — three milestones land between the last
        existing entry and the closing `---` of the Event Log section."""
        out = _inject_dispatch_milestones_into_event_log(_EVENT_LOG_FIXTURE, [
            "2026-05-19 11:37 - Everbridge notification sent (DRAFT) — Test — groups: UAS — individuals: 0",
            "2026-05-19 11:37 - Slack incident channel created: #test_chan",
            "2026-05-19 11:37 - D4H incident request filed at dispatch time",
        ])
        # All 3 milestones present
        assert "Everbridge notification sent (DRAFT)" in out
        assert "Slack incident channel created: #test_chan" in out
        assert "D4H incident request filed at dispatch time" in out
        # Original entries preserved
        assert "Request received from MILPITAS/SGT TOTH" in out
        assert "CalTopo map created" in out
        # Order: existing entry comes BEFORE injected; injected comes BEFORE
        # the closing divider that introduces LPB Questionnaire
        assert out.index("CalTopo map created") < out.index("Everbridge notification sent")
        assert out.index("D4H incident request filed") < out.index("LPB Questionnaire:")

    def test_empty_milestones_returns_unchanged(self):
        assert _inject_dispatch_milestones_into_event_log(_EVENT_LOG_FIXTURE, []) == _EVENT_LOG_FIXTURE

    def test_empty_text_returns_unchanged(self):
        assert _inject_dispatch_milestones_into_event_log("", ["foo"]) == ""

    def test_no_event_log_section_returns_unchanged(self):
        """Fallback for textareas without the standard two-summary layout
        (mutual-aid forms, dispatcher-edited content). Pass-through, no-op —
        D4H gets the original text and the milestones are lost; this is
        acceptable graceful degradation for an unstructured input."""
        raw = "Some unstructured text\nwith no Event Log section\nhere"
        assert _inject_dispatch_milestones_into_event_log(raw, ["x"]) == raw

    def test_only_first_event_log_section_injected(self):
        """If multiple Event Log:/--- patterns somehow appear in the same
        textarea (unlikely but possible after dispatcher edits), only the
        first one receives the injection — regex count=1 sentinel."""
        text = (
            "Event Log:\n"
            "A\n"
            "---\n"
            "Event Log:\n"
            "B\n"
            "---"
        )
        out = _inject_dispatch_milestones_into_event_log(text, ["2026-05-19 12:00 - SENTINEL_LINE"])
        assert out.count("SENTINEL_LINE") == 1

    def test_preserves_section_dividers(self):
        """The closing `---` divider must survive the injection — the
        consumer (_extract_iis_body_for_d4h) depends on it for section
        boundaries."""
        out = _inject_dispatch_milestones_into_event_log(_EVENT_LOG_FIXTURE, [
            "2026-05-19 11:37 - new entry",
        ])
        # Both `---` separators present in the result
        assert out.count("\n---\n") >= 2

    def test_round_trip_with_extractor(self):
        """End-to-end: injection then _extract_iis_body_for_d4h should
        produce a body containing the injected milestones, since the
        extractor strips section markers + 'Initial Incident Summary:' but
        preserves the Event Log content."""
        out_text = _inject_dispatch_milestones_into_event_log(_EVENT_LOG_FIXTURE, [
            "2026-05-19 11:37 - Everbridge notification sent (LIVE) — Test — groups: UAS — individuals: 1",
            "2026-05-19 11:37 - Slack incident channel created: #chan",
            "2026-05-19 11:37 - D4H incident request filed at dispatch time",
        ])
        body = _extract_iis_body_for_d4h(out_text)
        # Body starts with Event Name (per the extractor's contract)
        assert body.startswith("Event Name:")
        # Milestones present in the extracted body
        assert "Everbridge notification sent (LIVE)" in body
        assert "Slack incident channel created: #chan" in body
        assert "D4H incident request filed at dispatch time" in body

    def test_lkp_coords_from_map_data(self):
        result = _build_ocr_data_for_d4h(
            ocr_text="",
            map_data={"lkp": {"lat": 37.40087, "lng": -121.88387}},
        )
        assert result["lkp_lat"] == 37.40087
        assert result["lkp_lng"] == -121.88387

    def test_dob_and_age_extraction(self):
        result = _build_ocr_data_for_d4h(ocr_text=_SAMPLE_TEXTAREA, map_data={})
        assert result["mp_dob"] == "06/15/1980"
        assert result["mp_age"] == 45

    def test_dob_without_age_parenthetical(self):
        textarea = "DOB: 06/15/1980\n"
        result = _build_ocr_data_for_d4h(ocr_text=textarea, map_data={})
        assert result["mp_dob"] == "06/15/1980"
        assert result["mp_age"] is None

    def test_dob_with_yo_short_form(self):
        """The PDF-path synthetic summary historically emitted "(N yo)".
        A real dispatch silently dropped DOB + age
        from the D4H payload because the regex only matched "(N years old)".
        pdf_extract.py was changed in the same PR to emit the canonical
        "(N years old)" form, but the extraction regex remains tolerant of
        ANY parenthetical containing a digit — defensive against future
        format drift."""
        textarea = "DOB: 09/30/2010 (15 yo)\n"
        result = _build_ocr_data_for_d4h(ocr_text=textarea, map_data={})
        assert result["mp_dob"] == "09/30/2010"
        assert result["mp_age"] == 15

    def test_dob_with_yrs_old_variant(self):
        """Another common shorthand — must extract date + age."""
        textarea = "DOB: 06/15/1980 (45 yrs old)\n"
        result = _build_ocr_data_for_d4h(ocr_text=textarea, map_data={})
        assert result["mp_dob"] == "06/15/1980"
        assert result["mp_age"] == 45

    def test_dob_with_yo_dotted_variant(self):
        """y.o. variant."""
        textarea = "DOB: 06/15/1980 (45 y.o.)\n"
        result = _build_ocr_data_for_d4h(ocr_text=textarea, map_data={})
        assert result["mp_dob"] == "06/15/1980"
        assert result["mp_age"] == 45

    def test_dob_with_canonical_years_old_still_works(self):
        """Canonical Gemini-emitted form must continue to extract correctly."""
        textarea = "DOB: 09/30/2010 (15 years old)\n"
        result = _build_ocr_data_for_d4h(ocr_text=textarea, map_data={})
        assert result["mp_dob"] == "09/30/2010"
        assert result["mp_age"] == 15

    def test_dob_reconstructed_scenario(self):
        """Exact line SHAPE from the dispatch that surfaced this bug.
        Pin to prevent regression."""
        textarea = (
            "Missing Person: DOE, JANE; at-risk: First time runaway, depression\n"
            "DOB: 09/30/2010 (15 yo)\n"
            "Last Seen At: 2026-05-17 21:15\n"
        )
        result = _build_ocr_data_for_d4h(ocr_text=textarea, map_data={})
        assert result["mp_dob"] == "09/30/2010"
        assert result["mp_age"] == 15

    def test_officer_contact_split(self):
        result = _build_ocr_data_for_d4h(ocr_text=_SAMPLE_TEXTAREA, map_data={})
        assert result["officer_name"]  == "John Smith"
        assert result["officer_phone"] == "408-555-1234"

    def test_lpb_questionnaire_parsed(self):
        result = _build_ocr_data_for_d4h(ocr_text=_SAMPLE_TEXTAREA, map_data={})
        # Q1 + Q9 special-cased keys for d4h.py
        assert result["q1_familiar_with_area"]    == "Yes"
        assert result["q9_intentional_self_harm"] == "Yes"
        # Generic qN_question / qN_answer pairs
        assert result["q1_answer"]   == "Yes"
        assert result["q1_question"] == "Familiar with area"
        assert result["q11_answer"]  == "No"
        assert "Hospitals checked" in result["q11_question"]

    def test_koester_narrative_extracted(self):
        result = _build_ocr_data_for_d4h(ocr_text=_SAMPLE_TEXTAREA, map_data={})
        koester = result["koester_narrative"]
        assert "Category: Dementia" in koester
        assert "1.2 mi" in koester

    def test_missing_map_data_returns_safe_defaults(self):
        """Never raise — d4h.create_incident_with_subject may itself fail on
        missing required keys, which is caught at the call site."""
        result = _build_ocr_data_for_d4h(ocr_text="", map_data={})
        assert result["event_name"]         == ""
        assert result["lkp_lat"]            == 0.0
        assert result["lkp_lng"]            == 0.0
        assert result["lkp_address"]        == ""
        assert result["mp_dob"]             == ""
        assert result["mp_age"]             is None
        assert result["mp_sex"]             == ""
        assert result["officer_name"]       == ""
        assert result["officer_phone"]      == ""
        assert result["at_risk_indicators"] == []
        assert result["koester_narrative"]  == ""

    def test_none_inputs_handled_gracefully(self):
        result = _build_ocr_data_for_d4h(ocr_text=None, map_data=None)
        assert result["event_name"]         == ""
        assert result["at_risk_indicators"] == []


# Removed: TestFormatBulkAbsentSummary + _format_bulk_absent_summary mirror.
# Selective mode (fullTeam: false) eliminates the bulk-ABSENT step entirely
# — no async work to summarize. See project-d4h-selective-mode-decision.md.
