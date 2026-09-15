"""test_everbridge.py — pure-logic tests for everbridge.py helpers.

Per CLAUDE.md test file pattern: mirror the constants + pure-logic functions
from `backend/everbridge.py` locally rather than importing the module
directly. `everbridge.py` imports `httpx`, which is not installed in the
local pytest environment (it lives in the Cloud Run container only).

When updating helpers in everbridge.py, ALSO update the mirror here. The
mirror IS the test contract — if everbridge.py drifts away from this
mirror, that drift will surface in production behavior, and these tests
are the regression boundary.

Test architecture (Task 1.6 design decision, with Bill 2026-04-26):
  - Pure-logic helpers (constants, payload builders, response parsers,
    deep-link builders) ARE mirrored here and exercised under pytest.
  - Thin httpx wrappers (list_groups, list_contacts, create_*, send_*,
    poll_*, discover_*, delete_*) are NOT exercised here — they're 3-5
    line glue (build URL → httpx call → parse via the helper above).
    They're covered at live-test time on personal-dev. Revisit at end of
    Phase 1 (after Task 1.11) for a requirements-test.txt + venv-based
    pytest env.

Specifically tested (Tasks 1.5 + 1.6):
  - strip_sar_prefix() — group-name normalization for the dispatcher UI
  - SUPPRESSED_GROUP_IDS — ALERTSCC ADMIN filter constant is intact
  - compose_notification_body() — the canonical body string format
  - DELIVER_PATHS / CATEGORY_* / CALLER_ID — Phase 0 org-wide constants
  - _build_create_event_payload() — Phase 0 Step 1 shape
  - _build_send_notification_payload() — Phase 0 Step 2 shape (live + template)
  - _build_template_payload() — confirms launchtype omitted (Key Discovery #6)
  - _build_discovery_query_params() — pins `notificationEventId` literal
  - _parse_groups_response() — filter + normalize
  - _parse_contacts_response() — PII-minimal projection
  - _parse_group_members_response() — contact IDs for per-group tally map
  - _sort_emails_sccssar_first() — @sccssar.org-preferred ordering for Slack lookup
  - _extract_paths_emails() — extract email strings from a contact's paths field
  - _parse_group_member_contacts_response() — [{contact_id, emails, external_id,
    ocean, display_name}] for the email map + off-call exclusion
  - _parse_discovery_response() — totalCount=0 → None semantics
  - _parse_poll_response() — terminal-status detection + YES filter + empty cache signal
  - review_draft_url() / monitor_active_url() — deep link formats
  - _log_and_raise_for_status() — body-truncation + log-level selection (mirror)
"""
import ast
import logging
import os
from pathlib import Path
from typing import Optional

import pytest


# ---------------------------------------------------------------------------
# Mirrored constants and helpers — must be kept in sync with backend/everbridge.py
# ---------------------------------------------------------------------------

# Synthetic. Production reads EVERBRIDGE_SUPPRESSED_GROUP_IDS (comma-separated).
SUPPRESSED_GROUP_IDS: frozenset[str] = frozenset({"700000000000037"})

_NOTIFICATION_BODY_TEMPLATE = (
    "Need {resources_phrase} for a missing {age_phrase} in {city}. "
    "Please respond below with your availability.\n"
    "-##, {dispatcher_last_name}"
)

# Everbridge org record IDs are CONFIGURATION now, not source -- they are of no
# use to another EB customer and, published, describe this org's Everbridge
# estate. The values below are SYNTHETIC test fixtures; the real ones come from
# EVERBRIDGE_* env vars (terraform.tfvars, gitignored). These tests exercise
# SHAPE and LOGIC, not the values -- which is why swapping every real ID for a
# synthetic one left all 2191 tests passing.
#
# Production is pinned by TestEverbridgeOrgIdsAreConfiguration in
# test_main_regression.py, which reads everbridge.py's source. Do NOT paste a
# real ID back in to make a fixture "realistic" -- that habit is what put a
# missing person's details in this suite (#786).
#
# CATEGORY_* no longer exist in everbridge.py at all (they were declared there
# but never used; main.py::_category_for is the live consumer). Kept here only
# as fixture values for the payload-shape tests below.
CATEGORY_INCOUNTY  = 7000000000000003
CATEGORY_MUTUALAID = 7000000000000004
# Mirrors everbridge.py: the caller ID is CONFIGURATION (EVERBRIDGE_CALLER_ID),
# not a source constant -- it is a live dialable number. Pinned against
# production by TestEverbridgeCallerIdIsConfiguration in test_main_regression.py.
CALLER_ID = os.environ.get("EVERBRIDGE_CALLER_ID", "").strip()
# Synthetic. Production parses EVERBRIDGE_DELIVER_PATHS as a JSON array and
# _require_deliver_paths() rejects any entry missing `id` or `pathId`.
DELIVER_PATHS: list[dict] = [
    {"id": 7000000000000024, "pathId": 700000000000015, "prompt": "SMS-Work Cell"},
    {"id": 700000000000034,  "pathId": 700000000000009, "prompt": "SMS-Personal Cell"},
    {"id": 7000000000000025, "pathId": 700000000000010, "prompt": "Email-Work"},
    {"id": 700000000000032,  "pathId": 700000000000011, "prompt": "Email-Personal"},
    {"id": 700000000000035,  "pathId": 700000000000012, "prompt": "Email-Work Alt"},
    {"id": 7000000000000023, "pathId": 700000000000013, "prompt": "Voice-Work Cell"},
    {"id": 700000000000029,  "pathId": 700000000000014, "prompt": "Voice-Personal Cell"},
    {"id": 700000000000030,  "pathId": 700000000000008, "prompt": "Voice-Work Desk"},
    {"id": 700000000000031,  "pathId": 700000000000005, "prompt": "Voice-Home Phone"},
]

DISCOVERY_QUERY_PARAM = "notificationEventId"


def strip_sar_prefix(name: str) -> str:
    """Mirror of backend/everbridge.py::strip_sar_prefix()."""
    return name[len("SAR - "):] if name.startswith("SAR - ") else name


def compose_notification_body(
    *,
    resource_types,
    age,
    city: str,
    dispatcher_last_name: str,
    template_type: str = "incounty",
) -> str:
    """Mirror of backend/everbridge.py::compose_notification_body()."""
    age_phrase = f"{age} year old" if age is not None else "unknown age person"
    if not resource_types:
        resources_phrase = "[select group(s)] teams"
    elif len(resource_types) == 1 and resource_types[0] == "All Members":
        resources_phrase = "All Members"
    else:
        resources_phrase = ", ".join(resource_types) + " teams"
    ma_prefix = "(Mutual aid request) " if template_type == "mutualaid" else ""
    return ma_prefix + _NOTIFICATION_BODY_TEMPLATE.format(
        resources_phrase=resources_phrase,
        age_phrase=age_phrase,
        city=city,
        dispatcher_last_name=dispatcher_last_name,
    )


def _build_create_event_payload(org_id, event_name):
    return {"organizationId": int(org_id), "name": event_name}


def _build_send_notification_payload(
    *,
    org_id, event_id, event_name, title, body,
    target_contact_ids, target_group_ids, category_id,
    include_launchtype,
):
    payload = {
        "organizationId": int(org_id),
        "notificationEventId": event_id,
        "notificationName": event_name,
        "type": "Polling",
        "priority": "Priority",
        "categoryId": category_id,
        "message": {
            "title": title,
            "textMessage": body,
            "contentType": "Text",
            "categoryId": category_id,
            "questionaire": {
                "inputOptionAllowable": False,
                "multipleSelected": False,
                "answers": [
                    {"quotaNum": 0, "name": "Yes"},
                    {"quotaNum": 0, "name": "No"},
                ],
                "required": False,
            },
            "useCustomEmail": False,
            "useCustomSms": False,
            "accountId": 0, "organizationId": 0, "conferenceBridgeId": 0,
            "resourceBundleId": 0, "lastModifiedId": 0, "createdId": 0,
        },
        "broadcastContacts": {
            "contactIds": list(target_contact_ids),
            "groupIds":   list(target_group_ids),
            "contactSearchType": "AllOr",
            "sequenceGroupEnabled": False,
        },
        "broadcastSettings": {
            "deliverPaths": list(DELIVER_PATHS),
            "senderCallerInfos": [{
                "callerId": CALLER_ID,
                "isDefault": True,
                "countryCode": "US",
                "countryName": "United States",
                "accountId": 0, "resourceBundleId": 0, "organizationId": 0,
                "id": 0, "createdId": 0, "lastModifiedId": 0,
            }],
            "duration": 1,
            "durationTimeUnit": "HOURS",
            "contactCycles": 2,
            "cycleInterval": 2,
            "deliveryMethodInterval": 1,
            "confirm": True,
            "voiceMailOption": "MESSAGE_WITH_CONFIRMATION",
            "deliveryPathOrder": "Organization",
            "validateContacts": True,
            "smsCallBack": False,
            "throttle": False,
            "requirePinForMessage": False,
            "enableSecureNotification": False,
            "enableRecord": False,
            "quietTimeOverride": False,
            "recipientApp": False,
            "language": "en_US",
            "senderEmail": "County of Santa Clara",
        },
    }
    if include_launchtype:
        payload["launchtype"] = "SendNow"
    return payload


def _build_template_payload(**kwargs):
    return _build_send_notification_payload(**kwargs, include_launchtype=False)


def _build_discovery_query_params(event_id):
    return {DISCOVERY_QUERY_PARAM: event_id}


def _parse_groups_response(json_body):
    raw = json_body.get("page", {}).get("data", []) or []
    out = []
    for g in raw:
        gid = str(g.get("id", ""))
        if gid in SUPPRESSED_GROUP_IDS:
            continue
        out.append({"id": gid, "name": strip_sar_prefix(g.get("name", ""))})
    return out


def _parse_contacts_response(json_body):
    raw = json_body.get("page", {}).get("data", []) or []
    out = []
    for c in raw:
        first = (c.get("firstName") or "").strip()
        last = (c.get("lastName") or "").strip()
        display = f"{first} {last}".strip()
        if not display:
            continue
        out.append({"contact_id": str(c.get("id", "")), "display_name": display})
    return out


def _parse_ocean_from_external_id(external_id):
    """Mirror of everbridge.py::_parse_ocean_from_external_id."""
    if not external_id:
        return None
    s = str(external_id).strip()
    if len(s) < 3:
        return None
    tail = s[-3:]
    if not tail.isdigit():
        return None
    return tail


def _extract_dispatcher_ocean(json_body, dispatcher_email):
    """Mirror of everbridge.py::_extract_dispatcher_ocean."""
    if not dispatcher_email:
        return None
    needle = dispatcher_email.strip().lower()
    if not needle:
        return None
    raw = json_body.get("page", {}).get("data", []) or []
    for c in raw:
        for p in (c.get("paths") or []):
            if str(p.get("value", "")).strip().lower() == needle:
                return _parse_ocean_from_external_id(c.get("externalId"))
    return None


def _parse_group_members_response(json_body):
    """Mirror of everbridge.py::_parse_group_members_response — contact IDs only."""
    raw = json_body.get("page", {}).get("data", []) or []
    return [str(c["id"]) for c in raw if c.get("id")]


def _sort_emails_sccssar_first(emails):
    """Mirror of everbridge.py::_sort_emails_sccssar_first."""
    sccssar = [e for e in emails if e.lower().endswith("@sccssar.org")]
    others  = [e for e in emails if not e.lower().endswith("@sccssar.org")]
    return sccssar + others


def _extract_paths_emails(contact):
    """Mirror of everbridge.py::_extract_paths_emails."""
    raw = [
        str(p.get("value", ""))
        for p in (contact.get("paths") or [])
        if "@" in str(p.get("value", ""))
    ]
    return _sort_emails_sccssar_first(raw)


def _parse_group_member_contacts_response(json_body):
    """Mirror of everbridge.py::_parse_group_member_contacts_response."""
    raw = json_body.get("page", {}).get("data", []) or []
    out = []
    for c in raw:
        if not c.get("id"):
            continue
        contact_id = str(c["id"])
        first = (c.get("firstName") or "").strip()
        last = (c.get("lastName") or "").strip()
        name = f"{first} {last}".strip()
        out.append({
            "contact_id":   contact_id,
            "emails":       _extract_paths_emails(c),
            "external_id":  c.get("externalId") or "",
            "ocean":        _parse_ocean_from_external_id(c.get("externalId")),
            "display_name": name or f"contact {contact_id}",
        })
    return out


def _extract_id_from_post_response(json_body):
    """Mirror of backend/everbridge.py::_extract_id_from_post_response()."""
    if not isinstance(json_body, dict):
        raise RuntimeError(
            f"Everbridge POST response is not a dict (got {type(json_body).__name__})"
        )
    inner = json_body.get("result", json_body)
    nid = inner.get("id") if isinstance(inner, dict) else None
    # Cluster E (EB-L8) — falsy-zero-safe check. {"id": 0} is "missing"
    # for our purposes (EB IDs are always positive; 0 indicates an API bug
    # or an error envelope with status 200).
    if nid is None or nid == 0:
        raise RuntimeError(
            f"Everbridge POST response missing or zero 'id' (top-level or "
            f"under 'result'); keys={list(json_body.keys())}"
        )
    return str(nid)


def _parse_discovery_response(json_body):
    page = json_body.get("page", {}) or {}
    total = page.get("totalCount", 0)
    if not total:
        return None
    data = page.get("data", []) or []
    if not data:
        return None
    return str(data[0].get("id", "")) or None


def _parse_poll_response(json_body):
    # Mirror of backend/everbridge.py::_parse_poll_response (source of truth
    # is everbridge.py). decline_count / no_response_count added for issue #592.
    notif = json_body.get("result", json_body)
    notif_status = str(notif.get("notificationStatus", ""))
    nr = notif.get("notificationResult") or {}
    all_details = nr.get("allDetails") or []
    ack_contacts = []
    decline_count = 0
    no_response_count = 0
    for c in all_details:
        confirmed = bool(c.get("confirmed"))
        response_text = str(c.get("responseTextMessage", "")).strip()
        is_yes = confirmed and response_text.lower() == "yes"
        if not confirmed:
            no_response_count += 1
        elif not is_yes and response_text:
            decline_count += 1
        if not is_yes:
            continue
        emails = _sort_emails_sccssar_first([
            p["pathText"]
            for p in (c.get("callResultByPaths") or [])
            if "@" in str(p.get("pathText", ""))
        ])
        ack_contacts.append({
            "contact_id": str(c.get("contactId", "")),
            "first_name": c.get("firstName", ""),
            "last_name":  c.get("lastName", ""),
            "emails":     emails,
        })
    return {
        "is_terminal":       notif_status in ("Completed", "Stopped"),
        "notif_status":      notif_status,
        "ack_contacts":      ack_contacts,
        "decline_count":     decline_count,
        "no_response_count": no_response_count,
        "all_details_empty": len(all_details) == 0,
    }


def review_draft_url(template_id):
    return f"https://manager.everbridge.net/bcTemplates/edit/{template_id}"


def monitor_active_url(notification_id):
    return f"https://manager.everbridge.net/histories/report/{notification_id}"


# ---------------------------------------------------------------------------
# _log_and_raise_for_status() — mirror with a fake httpx.Response
# ---------------------------------------------------------------------------

_EB_ERROR_BODY_LOG_CAP = 800   # mirror of everbridge.py constant


class _FakeResponse:
    """Minimal duck-typed stand-in for httpx.Response for the helper test.

    Matches the attributes the helper actually reads: status_code, text,
    request.url, plus a raise_for_status() that raises a stand-in
    HTTPStatusError when status_code >= 400. We don't import httpx so this
    has to be self-contained — same constraint as the rest of this file.
    """
    class _Req:
        def __init__(self, url): self.url = url

    def __init__(self, status_code, text, url="https://api.everbridge.net/rest/test"):
        self.status_code = status_code
        self.text = text
        self.request = self._Req(url)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTPStatusError {self.status_code}")


def _log_and_raise_for_status_mirror(resp, op_label, _logger):
    """Mirror of backend/everbridge.py::_log_and_raise_for_status.

    Takes an explicit logger arg so tests can introspect captured records.
    Production code uses the module-level `logger`.
    """
    sc = resp.status_code
    if sc < 400:
        return
    raw = resp.text or ""
    body_fragment = raw[:_EB_ERROR_BODY_LOG_CAP]
    truncated = "(truncated)" if len(raw) > _EB_ERROR_BODY_LOG_CAP else ""
    log_fn = _logger.error if sc >= 500 else _logger.warning
    log_fn(
        "Everbridge %s failed: status=%d url=%s body=%r%s",
        op_label, sc, str(resp.request.url), body_fragment, truncated,
    )
    resp.raise_for_status()


class TestLogAndRaiseForStatus:
    """Body capture + log-level selection — fixes the 'EB returned 400 but we
    don't know why' problem. Pre-PR-#325 our wrappers raised HTTPStatusError
    with no body in logs."""

    def _logger(self):
        # A fresh logger per test so caplog isolation is clean.
        return logging.getLogger("test_eb_helper")

    def test_2xx_is_noop(self, caplog):
        resp = _FakeResponse(200, "ok")
        # No raise, no log.
        with caplog.at_level(logging.WARNING, logger="test_eb_helper"):
            _log_and_raise_for_status_mirror(resp, "list_groups", self._logger())
        assert caplog.records == []

    def test_3xx_is_noop(self, caplog):
        # 3xx is not raised by httpx.raise_for_status; the helper passes through.
        resp = _FakeResponse(301, "redirect")
        with caplog.at_level(logging.WARNING, logger="test_eb_helper"):
            _log_and_raise_for_status_mirror(resp, "list_groups", self._logger())
        assert caplog.records == []

    def test_4xx_logs_warning_and_raises(self, caplog):
        body = '{"message":"Event name already exists","status":400}'
        resp = _FakeResponse(400, body)
        with caplog.at_level(logging.WARNING, logger="test_eb_helper"):
            with pytest.raises(RuntimeError):
                _log_and_raise_for_status_mirror(
                    resp, "create_notification_event", self._logger()
                )
        assert len(caplog.records) == 1
        rec = caplog.records[0]
        assert rec.levelno == logging.WARNING
        assert "create_notification_event" in rec.getMessage()
        assert "400" in rec.getMessage()
        assert "Event name already exists" in rec.getMessage()

    def test_5xx_logs_error_not_warning(self, caplog):
        # 5xx → ERROR level so ops alerting can distinguish "EB had an outage"
        # from "we sent a bad payload".
        resp = _FakeResponse(503, "Service Unavailable")
        with caplog.at_level(logging.WARNING, logger="test_eb_helper"):
            with pytest.raises(RuntimeError):
                _log_and_raise_for_status_mirror(
                    resp, "poll_notification", self._logger()
                )
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.ERROR

    def test_body_truncated_at_cap(self, caplog):
        long_body = "x" * 2000
        resp = _FakeResponse(400, long_body)
        with caplog.at_level(logging.WARNING, logger="test_eb_helper"):
            with pytest.raises(RuntimeError):
                _log_and_raise_for_status_mirror(
                    resp, "list_groups", self._logger()
                )
        msg = caplog.records[0].getMessage()
        # Body fragment limited to cap; "(truncated)" marker present.
        assert "(truncated)" in msg
        # The full 2000-char body must NOT appear (only the first 800).
        # We verify by counting xs after stripping the surrounding metadata —
        # the repr() form wraps in quotes, so look for the cap.
        assert "x" * 2000 not in msg

    def test_short_body_not_marked_truncated(self, caplog):
        resp = _FakeResponse(400, "tiny error body")
        with caplog.at_level(logging.WARNING, logger="test_eb_helper"):
            with pytest.raises(RuntimeError):
                _log_and_raise_for_status_mirror(
                    resp, "list_groups", self._logger()
                )
        msg = caplog.records[0].getMessage()
        assert "(truncated)" not in msg
        assert "tiny error body" in msg

    def test_empty_body_handled_gracefully(self, caplog):
        resp = _FakeResponse(400, "")
        with caplog.at_level(logging.WARNING, logger="test_eb_helper"):
            with pytest.raises(RuntimeError):
                _log_and_raise_for_status_mirror(
                    resp, "list_groups", self._logger()
                )
        # Should not crash on empty body; should still log status + url.
        msg = caplog.records[0].getMessage()
        assert "400" in msg

    def test_op_label_is_in_log(self, caplog):
        resp = _FakeResponse(400, "x")
        with caplog.at_level(logging.WARNING, logger="test_eb_helper"):
            with pytest.raises(RuntimeError):
                _log_and_raise_for_status_mirror(
                    resp, "create_notification_template", self._logger()
                )
        # op_label is the searchable hook for ops triage — must be in the
        # logged message verbatim.
        assert "create_notification_template" in caplog.records[0].getMessage()

    def test_url_is_in_log(self, caplog):
        resp = _FakeResponse(
            400, "err",
            url="https://api.everbridge.net/rest/notificationTemplates/12345",
        )
        with caplog.at_level(logging.WARNING, logger="test_eb_helper"):
            with pytest.raises(RuntimeError):
                _log_and_raise_for_status_mirror(
                    resp, "create_notification_template", self._logger()
                )
        msg = caplog.records[0].getMessage()
        assert "/notificationTemplates/12345" in msg

    def test_log_cap_constant_pinned(self):
        # Pin the constant — a future "let's just log everything" PR that
        # removes the cap would cause unbounded log growth on HTML error
        # pages and break this assertion immediately.
        assert _EB_ERROR_BODY_LOG_CAP == 800


# ---------------------------------------------------------------------------
# strip_sar_prefix() — group-name normalization
# ---------------------------------------------------------------------------

class TestStripSarPrefix:
    def test_strips_sar_dash_prefix(self):
        assert strip_sar_prefix("SAR - K9") == "K9"

    def test_strips_sar_all_members_prefix(self):
        assert strip_sar_prefix("SAR - All Members") == "All Members"

    def test_no_prefix_passes_through(self):
        assert strip_sar_prefix("Foo") == "Foo"

    def test_empty_string_passes_through(self):
        assert strip_sar_prefix("") == ""

    def test_partial_match_passes_through(self):
        # "SAR-" without the surrounding spaces is NOT the SCCSSAR convention
        # and must not be stripped — only the exact "SAR - " prefix fires.
        assert strip_sar_prefix("SAR-K9") == "SAR-K9"

    def test_only_first_occurrence_stripped(self):
        # If a name somehow starts "SAR - SAR - K9" the function should only
        # strip ONE prefix; the second "SAR - " is preserved as-is.
        assert strip_sar_prefix("SAR - SAR - K9") == "SAR - K9"


# ---------------------------------------------------------------------------
# SUPPRESSED_GROUP_IDS — ALERTSCC ADMIN must remain in the constant
# ---------------------------------------------------------------------------

class TestSuppressedGroups:
    def test_alertscc_admin_suppressed(self):
        # ALERTSCC ADMIN (group ID 700000000000037) is filtered server-side from
        # /everbridge-groups. It is an org-admin group, not a SAR paging target.
        # See CLAUDE.md DESIGN DECISION block above SUPPRESSED_GROUP_IDS.
        assert "700000000000037" in SUPPRESSED_GROUP_IDS

    def test_constant_is_frozen(self):
        # Must be a frozenset so the suppression list can't be mutated at runtime.
        assert isinstance(SUPPRESSED_GROUP_IDS, frozenset)


# ---------------------------------------------------------------------------
# compose_notification_body() — canonical body format
# ---------------------------------------------------------------------------

class TestComposeNotificationBody:
    def test_basic_body(self):
        body = compose_notification_body(
            resource_types=["K9", "UAS", "Drivers"],
            age=79,
            city="Milpitas",
            dispatcher_last_name="Burns",
        )
        assert "Need K9, UAS, Drivers teams" in body
        assert "missing 79 year old in Milpitas" in body
        assert "-##, Burns" in body

    def test_single_resource(self):
        body = compose_notification_body(
            resource_types=["K9"],
            age=15,
            city="San Jose",
            dispatcher_last_name="Vance",
        )
        assert "Need K9 teams for a missing 15 year old in San Jose" in body
        assert body.endswith("-##, Vance")

    def test_age_unknown(self):
        body = compose_notification_body(
            resource_types=["K9"],
            age=None,
            city="San Jose",
            dispatcher_last_name="Vance",
        )
        # Body must still be valid even if age missing — fall through with
        # "unknown age person" so the dispatcher can edit before sending.
        assert "Need K9 teams" in body
        assert "missing unknown age person in San Jose" in body
        assert "year old" not in body

    def test_response_prompt_present(self):
        # The response-prompt sentence is part of the canonical template — it
        # tells responders what to do. Pin it here so a future template tweak
        # that drops it fails this test.
        body = compose_notification_body(
            resource_types=["K9"],
            age=42,
            city="Cupertino",
            dispatcher_last_name="Cubeiro",
        )
        assert "Please respond below with your availability." in body

    def test_dispatcher_signoff_on_own_line(self):
        # "-##, LastName" must be on its own line (newline before it). The
        # WhatsApp/Slack consumers read this as the signoff and depend on the
        # newline boundary.
        body = compose_notification_body(
            resource_types=["K9"],
            age=42,
            city="Cupertino",
            dispatcher_last_name="Romard",
        )
        assert "\n-##, Romard" in body

    def test_all_members_no_teams_suffix(self):
        # "All Members" is the everyone-channel; "teams" suffix is awkward
        # when it's the only selection. Frontend mirrors this special case.
        body = compose_notification_body(
            resource_types=["All Members"],
            age=55,
            city="Los Gatos",
            dispatcher_last_name="Burns",
        )
        assert "Need All Members for a missing" in body
        assert "teams" not in body

    def test_mutual_aid_prefix(self):
        # Mutual-aid notifications require the "(Mutual aid request) " prefix
        # per county policy. In-county sends must NOT have this prefix.
        body_ma = compose_notification_body(
            resource_types=["K9"],
            age=30,
            city="Santa Cruz",
            dispatcher_last_name="Burns",
            template_type="mutualaid",
        )
        assert body_ma.startswith("(Mutual aid request) ")

        body_ic = compose_notification_body(
            resource_types=["K9"],
            age=30,
            city="Santa Cruz",
            dispatcher_last_name="Burns",
            template_type="incounty",
        )
        assert not body_ic.startswith("(")


# ===========================================================================
# Task 1.6 — Phase 0 constants + payload shapes + response parsers
# ===========================================================================

# ---------------------------------------------------------------------------
# Phase 0 Org-Wide Constants (DELIVER_PATHS, categories, caller ID)
# ---------------------------------------------------------------------------

class TestPhase0Constants:
    """Pin the verbatim Phase 0 values. A future PR that "cleans up" these
    constants without re-running the live Everbridge spike will fail here."""

    def test_category_incounty(self):
        assert CATEGORY_INCOUNTY == 7000000000000003

    def test_category_mutualaid(self):
        assert CATEGORY_MUTUALAID == 7000000000000004

    def test_caller_id(self):
        assert CALLER_ID == os.environ.get("EVERBRIDGE_CALLER_ID", "").strip()

    def test_deliver_paths_count(self):
        # Phase 0: 9 paths confirmed across all SOSAR templates. Adding or
        # removing a path requires a new live test and explicit team OK.
        assert len(DELIVER_PATHS) == 9

    def test_deliver_paths_have_id_and_pathid(self):
        # Phase 0 Key Discovery #2: each entry needs BOTH `id` (record ID)
        # and `pathId` (path type). Without `id`, Everbridge returns HTTP 400
        # "No DeliveryPath for notification".
        for p in DELIVER_PATHS:
            assert "id" in p and "pathId" in p, f"missing id/pathId: {p}"
            assert isinstance(p["id"], int)
            assert isinstance(p["pathId"], int)

    def test_deliver_paths_verbatim_first_record(self):
        # Spot-pin the first record so a re-ordering or accidental
        # reformatting fails immediately.
        assert DELIVER_PATHS[0] == {
            "id": 7000000000000024,
            "pathId": 700000000000015,
            "prompt": "SMS-Work Cell",
        }


# ---------------------------------------------------------------------------
# _build_create_event_payload() — Phase 0 Step 1
# ---------------------------------------------------------------------------

class TestBuildCreateEventPayload:
    def test_shape(self):
        p = _build_create_event_payload("700000000000026", "2026-04-25 MPD CALAVERAS 1430")
        assert p == {
            "organizationId": 700000000000026,
            "name": "2026-04-25 MPD CALAVERAS 1430",
        }

    def test_organization_id_is_int(self):
        # Phase 0 verbatim payload uses int, not string. Everbridge will accept
        # string but the spike used int; pin it so the production payload
        # matches the spike byte-for-byte.
        p = _build_create_event_payload("700000000000026", "X")
        assert isinstance(p["organizationId"], int)


# ---------------------------------------------------------------------------
# _build_send_notification_payload() — Phase 0 Step 2 (live + template share shape)
# ---------------------------------------------------------------------------

class TestBuildSendNotificationPayload:
    """Each Phase 0 Key Discovery has at least one assertion here so a future
    PR that drifts the payload is caught at pre-flight."""

    def _kwargs(self):
        return dict(
            org_id="700000000000026",
            event_id="EVT_456",
            event_name="2026-04-25 MPD CALAVERAS 1430",
            title="SOSAR — K9 + UAS needed",
            body="Need K9, UAS, for a missing 79 year old in Milpitas. ...",
            target_contact_ids=["c1"],
            target_group_ids=["g1"],
            category_id=CATEGORY_INCOUNTY,
        )

    def test_live_includes_launchtype_send_now(self):
        p = _build_send_notification_payload(**self._kwargs(), include_launchtype=True)
        assert p["launchtype"] == "SendNow"

    def test_template_omits_launchtype(self):
        # Phase 0 Key Discovery #6: /notificationTemplates payload differs
        # from /notifications by ONLY the launchtype key.
        p = _build_template_payload(**self._kwargs())
        assert "launchtype" not in p

    def test_source_template_id_absent(self):
        # Phase 0 Key Discovery #1: including sourceTemplateId triggers HTTP
        # 400 "Broadcast template is not complete". Must be absent in BOTH
        # live and template payloads.
        live = _build_send_notification_payload(**self._kwargs(), include_launchtype=True)
        tpl  = _build_template_payload(**self._kwargs())
        assert "sourceTemplateId" not in live
        assert "sourceTemplateId" not in tpl

    def test_notification_event_id_wired(self):
        # Phase 0 Key Discovery #5 + #6: notificationEventId is sent in BOTH
        # payloads (live inherits natively; template stores it).
        live = _build_send_notification_payload(**self._kwargs(), include_launchtype=True)
        tpl  = _build_template_payload(**self._kwargs())
        assert live["notificationEventId"] == "EVT_456"
        assert tpl["notificationEventId"] == "EVT_456"

    def test_deliver_paths_carries_id_field(self):
        # Phase 0 Key Discovery #2: every deliverPaths entry must have `id`.
        p = _build_send_notification_payload(**self._kwargs(), include_launchtype=True)
        deliver = p["broadcastSettings"]["deliverPaths"]
        assert all("id" in entry for entry in deliver)
        assert len(deliver) == len(DELIVER_PATHS)

    def test_type_polling_priority_priority(self):
        # Phase 0 verbatim — both templates use type=Polling, priority=Priority.
        p = _build_send_notification_payload(**self._kwargs(), include_launchtype=True)
        assert p["type"] == "Polling"
        assert p["priority"] == "Priority"

    def test_message_title_and_body_wired(self):
        kw = self._kwargs()
        p = _build_send_notification_payload(**kw, include_launchtype=True)
        assert p["message"]["title"] == kw["title"]
        assert p["message"]["textMessage"] == kw["body"]
        assert p["message"]["categoryId"] == CATEGORY_INCOUNTY

    def test_questionnaire_yes_no_two_answers(self):
        # Phase 0 verbatim: Yes/No polling questionnaire.
        p = _build_send_notification_payload(**self._kwargs(), include_launchtype=True)
        answers = p["message"]["questionaire"]["answers"]
        assert answers == [
            {"quotaNum": 0, "name": "Yes"},
            {"quotaNum": 0, "name": "No"},
        ]

    def test_target_contacts_and_groups_wired(self):
        p = _build_send_notification_payload(**self._kwargs(), include_launchtype=True)
        assert p["broadcastContacts"]["contactIds"] == ["c1"]
        assert p["broadcastContacts"]["groupIds"]   == ["g1"]

    def test_caller_info_uses_org_caller_id(self):
        p = _build_send_notification_payload(**self._kwargs(), include_launchtype=True)
        sender = p["broadcastSettings"]["senderCallerInfos"][0]
        assert sender["callerId"] == CALLER_ID
        assert sender["isDefault"] is True

    def test_template_payload_otherwise_identical_to_live(self):
        # The ONLY difference between live and template is launchtype.
        live = _build_send_notification_payload(**self._kwargs(), include_launchtype=True)
        tpl  = _build_template_payload(**self._kwargs())
        live.pop("launchtype")
        assert live == tpl

    def test_lists_are_copies_not_shared_references(self):
        # Mutating a returned payload must NOT mutate the module-level
        # DELIVER_PATHS constant. (Caller-side defensive — the wrappers
        # don't currently mutate, but a future caller might.)
        kw = self._kwargs()
        p1 = _build_send_notification_payload(**kw, include_launchtype=True)
        p1["broadcastSettings"]["deliverPaths"].append({"id": 0, "pathId": 0})
        assert len(DELIVER_PATHS) == 9


# ---------------------------------------------------------------------------
# _build_discovery_query_params() — pin the literal `notificationEventId`
# ---------------------------------------------------------------------------

class TestBuildDiscoveryQueryParams:
    def test_uses_notification_event_id_literal(self):
        # Phase 0 Key Discovery #8: the lookalikes (`?eventId=`, `?event=`)
        # are silently ignored. ONLY `?notificationEventId=` filters.
        params = _build_discovery_query_params("EVT_123")
        assert params == {"notificationEventId": "EVT_123"}

    def test_param_name_constant_is_pinned(self):
        # Belt-and-braces: even if someone changes the function body, the
        # constant must stay literal. Regression test in
        # test_main_regression.py also pins this from a different module.
        assert DISCOVERY_QUERY_PARAM == "notificationEventId"


# ---------------------------------------------------------------------------
# _parse_groups_response() — filter SUPPRESSED_GROUP_IDS, normalize names
# ---------------------------------------------------------------------------

class TestParseGroupsResponse:
    def test_filters_suppressed_and_strips_prefix(self):
        body = {"page": {"data": [
            {"id": "111", "name": "SAR - K9"},
            {"id": "700000000000037", "name": "ALERTSCC ADMIN"},
            {"id": "222", "name": "SAR - All Members"},
        ]}}
        out = _parse_groups_response(body)
        ids = {g["id"] for g in out}
        names = {g["name"] for g in out}
        assert "700000000000037" not in ids
        assert ids == {"111", "222"}
        assert names == {"K9", "All Members"}

    def test_handles_missing_page_key(self):
        # Defensive — Everbridge API returns 200 with `{"message": "OK"}` and
        # no `page` key on some empty-list responses.
        assert _parse_groups_response({"message": "OK"}) == []

    def test_handles_missing_data_array(self):
        assert _parse_groups_response({"page": {}}) == []


# ---------------------------------------------------------------------------
# _parse_contacts_response() — PII-minimal projection
# ---------------------------------------------------------------------------

class TestParseContactsResponse:
    def test_returns_only_id_and_display_name(self):
        body = {"page": {"data": [
            {"id": "c1", "firstName": "Bill", "lastName": "Burns",
             "paths": [{"value": "bill@example.com"}]},
        ]}}
        out = _parse_contacts_response(body)
        assert out == [{"contact_id": "c1", "display_name": "Bill Burns"}]

    def test_email_does_not_leak_into_output(self):
        # Design Section 1 PII boundary — email and phone MUST NOT cross
        # this seam.
        import json
        body = {"page": {"data": [
            {"id": "c1", "firstName": "Bill", "lastName": "Burns",
             "paths": [{"value": "bill@example.com"}],
             "homePhone": "555-1212"},
        ]}}
        out = _parse_contacts_response(body)
        s = json.dumps(out)
        assert "bill@example.com" not in s
        assert "555-1212" not in s

    def test_skips_blank_display_name(self):
        # A contact with no firstName AND no lastName is unusable in the
        # typeahead — skip rather than render an empty pill.
        body = {"page": {"data": [
            {"id": "c1", "firstName": "", "lastName": ""},
            {"id": "c2", "firstName": "A", "lastName": ""},
        ]}}
        out = _parse_contacts_response(body)
        assert len(out) == 1
        assert out[0]["contact_id"] == "c2"


# ---------------------------------------------------------------------------
# OCEAN# parsing — issue #363 (spike 2026-05-01)
#
# SCCSSAR encodes the dispatcher's 3-digit OCEAN# in EB's `externalId` field
# as "1O" + 3 digits (e.g. "1O305"). The pure helpers below resolve the
# OCEAN# from a list-contacts response by matching the caller's auth-token
# email against the contact's `paths` field; only the parsed 3-digit tail
# is returned (PII boundary preserved — email never leaves the function).
# ---------------------------------------------------------------------------

class TestParseOceanFromExternalId:
    def test_parses_5_char_sccssar_format(self):
        # The shape SCCSSAR actually uses today.
        assert _parse_ocean_from_external_id("1O305") == "305"

    def test_parses_just_3_digits(self):
        # If a future record has only the 3-digit tail with no prefix.
        assert _parse_ocean_from_external_id("305") == "305"

    def test_parses_longer_prefix_robust(self):
        # Defensive: if the prefix grows in a future record, still return
        # the trailing 3 digits via the [-3:] slice rule.
        assert _parse_ocean_from_external_id("XYZ-305") == "305"

    def test_none_when_missing(self):
        assert _parse_ocean_from_external_id(None) is None
        assert _parse_ocean_from_external_id("") is None

    def test_none_when_too_short(self):
        # Anything < 3 chars cannot yield a 3-digit OCEAN#.
        assert _parse_ocean_from_external_id("1O") is None
        assert _parse_ocean_from_external_id("12") is None

    def test_none_when_tail_non_numeric(self):
        # "1Oabc" → tail "abc" → fails ^\d{3}$ → None (defensive fallback).
        assert _parse_ocean_from_external_id("1Oabc") is None
        assert _parse_ocean_from_external_id("1O30A") is None
        assert _parse_ocean_from_external_id("1O 05") is None

    def test_strips_whitespace(self):
        # An admin entering "  1O305  " in the EB UI shouldn't break the lookup.
        assert _parse_ocean_from_external_id("  1O305  ") == "305"

    def test_accepts_int(self):
        # str() coerces — defensive against EB returning a numeric externalId.
        assert _parse_ocean_from_external_id(305) == "305"


class TestExtractDispatcherOcean:
    @pytest.fixture
    def body(self):
        # Three contacts: caller (1O305), an unrelated dispatcher (1O185),
        # and one with a non-numeric externalId. None of these sample emails
        # match real production accounts.
        return {"page": {"data": [
            {"id": "c1", "firstName": "Bill", "lastName": "Burns",
             "externalId": "1O305",
             "paths": [{"value": "bill@example.com"},
                       {"value": "bill.alt@example.com"}]},
            {"id": "c2", "firstName": "Dana", "lastName": "Vance",
             "externalId": "1O185",
             "paths": [{"value": "dana@example.com"}]},
            {"id": "c3", "firstName": "Test", "lastName": "User",
             "externalId": "MALFORMED",
             "paths": [{"value": "test@example.com"}]},
        ]}}

    def test_finds_dispatcher_by_email(self, body):
        assert _extract_dispatcher_ocean(body, "bill@example.com") == "305"

    def test_email_match_is_case_insensitive(self, body):
        # Auth tokens lowercase the email at the auth.py boundary, but a
        # mismatched-case payload from EB shouldn't lose the match either.
        assert _extract_dispatcher_ocean(body, "Bill@Example.com") == "305"

    def test_finds_via_secondary_path(self, body):
        # Contacts have multiple `paths` entries (work + personal email).
        # Lookup must try every path, not only the first.
        assert _extract_dispatcher_ocean(body, "bill.alt@example.com") == "305"

    def test_other_dispatcher_returns_their_ocean(self, body):
        assert _extract_dispatcher_ocean(body, "dana@example.com") == "185"

    def test_non_numeric_external_id_returns_none(self, body):
        # Match found, but parser rejects "MALFORMED" — never substitute
        # garbage into the body or summary lines.
        assert _extract_dispatcher_ocean(body, "test@example.com") is None

    def test_unknown_email_returns_none(self, body):
        # Dispatcher has no matching EB contact (admin-side gap).
        assert _extract_dispatcher_ocean(body, "stranger@example.com") is None

    def test_empty_email_returns_none(self):
        assert _extract_dispatcher_ocean({}, "") is None
        assert _extract_dispatcher_ocean({}, "   ") is None

    def test_empty_body_returns_none(self):
        assert _extract_dispatcher_ocean({}, "bill@example.com") is None
        assert _extract_dispatcher_ocean(
            {"page": {"data": []}}, "bill@example.com",
        ) is None

    def test_does_not_return_email_or_paths_in_result(self):
        # Regression pin: result is a 3-char string OR None — never a dict
        # that could leak email back to the caller.
        body = {"page": {"data": [
            {"id": "c1", "firstName": "Bill", "lastName": "Burns",
             "externalId": "1O305",
             "paths": [{"value": "bill@example.com"}]},
        ]}}
        result = _extract_dispatcher_ocean(body, "bill@example.com")
        assert result == "305"
        assert "@" not in (result or "")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# /everbridge-contacts envelope — PII regression pin (issue #363, PR-B)
#
# After the issue #363 envelope change, the route returns
#   { "contacts": [...], "dispatcher_ocean": "xxx" | null }
# The contacts list MUST stay PII-minimal (id + display_name only) and the
# dispatcher_ocean field MUST be a 3-char string (or null) — never the
# email itself, never a dict, never paths.
# ---------------------------------------------------------------------------

class TestEverbridgeContactsEnvelopePII:
    def test_envelope_contacts_list_excludes_email(self):
        # Mirror the projection that /everbridge-contacts returns.
        body = {"page": {"data": [
            {"id": "c1", "firstName": "Bill", "lastName": "Burns",
             "externalId": "1O305",
             "paths": [{"value": "bill@example.com"}],
             "homePhone": "555-1212"},
        ]}}
        envelope = {
            "contacts": _parse_contacts_response(body),
            "dispatcher_ocean": _extract_dispatcher_ocean(body, "bill@example.com"),
        }
        import json
        s = json.dumps(envelope)
        # PII boundary: no email, no phone in the wire payload.
        assert "bill@example.com" not in s
        assert "555-1212" not in s
        # OCEAN# IS allowed — it's a non-PII radio call sign.
        assert envelope["dispatcher_ocean"] == "305"

    def test_envelope_dispatcher_ocean_is_null_when_unmatched(self):
        body = {"page": {"data": [
            {"id": "c1", "firstName": "Bill", "lastName": "Burns",
             "externalId": "1O305",
             "paths": [{"value": "bill@example.com"}]},
        ]}}
        # Dispatcher has no matching EB contact — envelope stays valid.
        envelope = {
            "contacts": _parse_contacts_response(body),
            "dispatcher_ocean": _extract_dispatcher_ocean(body, "stranger@example.com"),
        }
        assert envelope["dispatcher_ocean"] is None
        assert len(envelope["contacts"]) == 1


# ---------------------------------------------------------------------------
# _parse_group_members_response() — contact IDs for group membership map
# ---------------------------------------------------------------------------

class TestParseGroupMembersResponse:
    def test_returns_contact_id_strings(self):
        body = {"page": {"data": [
            {"id": "c1", "firstName": "Bill", "lastName": "Burns"},
            {"id": "c2", "firstName": "Dana", "lastName": "Vance"},
        ]}}
        out = _parse_group_members_response(body)
        assert out == ["c1", "c2"]

    def test_skips_entries_without_id(self):
        body = {"page": {"data": [
            {"firstName": "NoId"},
            {"id": "c2", "firstName": "Dana"},
        ]}}
        out = _parse_group_members_response(body)
        assert out == ["c2"]

    def test_pii_not_in_output(self):
        import json
        body = {"page": {"data": [
            {"id": "c1", "firstName": "Bill", "lastName": "Burns",
             "paths": [{"value": "bill@example.com"}]},
        ]}}
        out = _parse_group_members_response(body)
        s = json.dumps(out)
        assert "bill@example.com" not in s
        assert "Burns" not in s

    def test_empty_page(self):
        assert _parse_group_members_response({"page": {"data": []}}) == []
        assert _parse_group_members_response({"message": "OK"}) == []

    def test_id_coerced_to_string(self):
        # EB sends numeric IDs as integers; we normalize to str.
        body = {"page": {"data": [{"id": 12345678}]}}
        out = _parse_group_members_response(body)
        assert out == ["12345678"]
        assert isinstance(out[0], str)


# ---------------------------------------------------------------------------
# _extract_paths_emails() — email extraction from contact paths field
# ---------------------------------------------------------------------------

class TestExtractPathsEmails:
    def test_returns_email_values(self):
        contact = {"paths": [
            {"value": "bill@example.com", "pathId": 1},
            {"value": "4085551234", "pathId": 2},        # phone — excluded
            {"value": "dana@example.com", "pathId": 3},
        ]}
        assert _extract_paths_emails(contact) == ["bill@example.com", "dana@example.com"]

    def test_excludes_non_email_paths(self):
        contact = {"paths": [{"value": "5551234567"}, {"value": "no-at-sign"}]}
        assert _extract_paths_emails(contact) == []

    def test_empty_paths(self):
        assert _extract_paths_emails({"paths": []}) == []
        assert _extract_paths_emails({}) == []

    def test_none_paths(self):
        assert _extract_paths_emails({"paths": None}) == []

    def test_value_coerced_to_string(self):
        # Defensive: value should be a string, but guard against numeric IDs
        contact = {"paths": [{"value": "bill@example.com"}]}
        out = _extract_paths_emails(contact)
        assert all(isinstance(e, str) for e in out)

    def test_sccssar_email_sorted_first_when_personal_path_comes_first(self):
        # Empirical scenario from live EB 2026-04-30: dispatcher reorders the
        # delivery-method "Order" column in EB UI so personal email is path
        # index [2] (Email-Work Alt) and sccssar.org is path index [3]
        # (Email-Personal). emails[0] for Slack lookup MUST still be sccssar.
        contact = {"paths": [
            {"value": "4085550142", "pathId": 700000000000009},
            {"value": "4085550142", "pathId": 700000000000014},
            {"value": "bill@example.com", "pathId": 700000000000012},
            {"value": "Bill.burns@sccssar.org",        "pathId": 700000000000011},
        ]}
        out = _extract_paths_emails(contact)
        assert out[0] == "Bill.burns@sccssar.org"
        # Personal email is preserved, just demoted.
        assert "bill@example.com" in out

    def test_no_sccssar_email_preserves_original_order(self):
        # SAR coordinator case (sheriff-dept email, no @sccssar.org): the
        # extraction MUST fall through to original path order without
        # introducing arbitrary reordering.
        contact = {"paths": [
            {"value": "first@sheriff-dept.example", "pathId": 1},
            {"value": "second@personal.example",   "pathId": 2},
        ]}
        out = _extract_paths_emails(contact)
        assert out == ["first@sheriff-dept.example", "second@personal.example"]


class TestSortEmailsSccssarFirst:
    def test_sccssar_promoted_to_index_0(self):
        out = _sort_emails_sccssar_first([
            "personal@gmail.com", "dana.vance@sccssar.org",
        ])
        assert out[0] == "dana.vance@sccssar.org"

    def test_case_insensitive_match(self):
        # Bill's actual EB record uses mixed case "Bill.burns@sccssar.org".
        out = _sort_emails_sccssar_first([
            "alt@personal.com", "Bill.Burns@SCCSSAR.ORG",
        ])
        assert out[0] == "Bill.Burns@SCCSSAR.ORG"

    def test_no_sccssar_preserves_relative_order(self):
        # SAR-coordinator case — no @sccssar.org email; preserve as-is.
        emails = ["b@example.com", "a@example.com", "c@example.com"]
        assert _sort_emails_sccssar_first(emails) == emails

    def test_multiple_sccssar_preserves_relative_order_among_them(self):
        # If a contact ever has two @sccssar.org emails, the one earlier in
        # the input wins (no further re-sorting between them).
        emails = ["personal@x.com", "second@sccssar.org", "first@sccssar.org"]
        out = _sort_emails_sccssar_first(emails)
        assert out == ["second@sccssar.org", "first@sccssar.org", "personal@x.com"]

    def test_only_sccssar_passes_through(self):
        emails = ["only@sccssar.org"]
        assert _sort_emails_sccssar_first(emails) == ["only@sccssar.org"]

    def test_empty_list(self):
        assert _sort_emails_sccssar_first([]) == []

    def test_sccssar_substring_in_other_domain_not_matched(self):
        # Defensive: a domain like "@notsccssar.org" or "@sccssar.org.evil"
        # MUST NOT match. endswith() with ".org" is sufficient because
        # "@sccssar.org" appears at the rightmost position only for the
        # real domain.
        out = _sort_emails_sccssar_first([
            "first@notsccssar.org", "real@sccssar.org",
        ])
        assert out[0] == "real@sccssar.org"


# ---------------------------------------------------------------------------
# _parse_group_member_contacts_response() — [{contact_id, emails, external_id, ocean, display_name}]
# ---------------------------------------------------------------------------

class TestParseGroupMemberContactsResponse:
    def test_returns_contact_id_and_emails(self):
        body = {"page": {"data": [
            {"id": "c1", "paths": [{"value": "bill@example.com"}]},
            {"id": "c2", "paths": [{"value": "dana@example.com"}, {"value": "5555555"}]},
        ]}}
        out = _parse_group_member_contacts_response(body)
        assert out == [
            {"contact_id": "c1", "emails": ["bill@example.com"],
             "external_id": "", "ocean": None, "display_name": "contact c1"},
            {"contact_id": "c2", "emails": ["dana@example.com"],
             "external_id": "", "ocean": None, "display_name": "contact c2"},
        ]

    def test_contact_with_no_email_paths(self):
        # SMS-only contact — emails list is [] (not missing), caller handles gracefully.
        body = {"page": {"data": [{"id": "c1", "paths": [{"value": "4085551234"}]}]}}
        out = _parse_group_member_contacts_response(body)
        assert out == [{"contact_id": "c1", "emails": [],
                        "external_id": "", "ocean": None, "display_name": "contact c1"}]

    def test_skips_entries_without_id(self):
        body = {"page": {"data": [
            {"paths": [{"value": "no@id.com"}]},           # no id
            {"id": "c2", "paths": [{"value": "ok@example.com"}]},
        ]}}
        out = _parse_group_member_contacts_response(body)
        assert len(out) == 1
        assert out[0]["contact_id"] == "c2"

    def test_empty_page(self):
        assert _parse_group_member_contacts_response({"page": {"data": []}}) == []
        assert _parse_group_member_contacts_response({"message": "OK"}) == []

    def test_id_coerced_to_string(self):
        body = {"page": {"data": [{"id": 99887766, "paths": [{"value": "a@b.com"}]}]}}
        out = _parse_group_member_contacts_response(body)
        assert out[0]["contact_id"] == "99887766"
        assert isinstance(out[0]["contact_id"], str)


class TestGroupMemberContactsCarryOcean:
    """Off-call exclusion needs each group member's OCEAN# and a display
    name; Step 9 keeps reading contact_id + emails. Additive keys only."""

    _BODY = {"page": {"data": [
        {"id": 795090251415976, "externalId": "1O305", "firstName": "Bill", "lastName": "Burns",
         "paths": [{"pathId": 1, "value": "bill.burns@sccssar.org"}]},
        {"id": 906019145263623, "externalId": "1O242", "firstName": "Damian", "lastName": "Romard", "paths": []},
        {"id": 1, "externalId": "", "firstName": "", "lastName": "", "paths": []},
        {"id": 2, "firstName": None, "lastName": "Solo"},                     # missing externalId + paths keys
        {"id": 3, "externalId": "10O305", "firstName": " Bill ", "lastName": " Burns "},  # padded names, longer prefix
    ]}}

    def test_keys_and_ocean(self):
        out = _parse_group_member_contacts_response(self._BODY)
        assert set(out[0]) == {"contact_id", "emails", "external_id", "ocean", "display_name"}
        assert out[0]["ocean"] == "305" and out[1]["ocean"] == "242"
        assert out[0]["external_id"] == "1O305"
        assert out[0]["display_name"] == "Bill Burns"

    def test_padded_names_stripped_individually_and_longer_prefix_parses(self):
        # Sibling _parse_contacts_response strips first/last individually; a
        # joined-then-stripped string would leave the inner double space.
        out = _parse_group_member_contacts_response(self._BODY)
        assert out[4]["display_name"] == "Bill Burns"
        assert out[4]["ocean"] == "305"

    def test_unparseable_external_id_yields_none_ocean_and_id_fallback_name(self):
        out = _parse_group_member_contacts_response(self._BODY)
        assert out[2]["ocean"] is None and out[2]["external_id"] == ""
        assert out[2]["display_name"] == "contact 1"

    def test_missing_keys_are_none_safe(self):
        out = _parse_group_member_contacts_response(self._BODY)
        assert out[3] == {"contact_id": "2", "emails": [], "external_id": "", "ocean": None, "display_name": "Solo"}

    def test_step9_contract_unchanged(self):
        # The existing consumer reads exactly these two keys.
        out = _parse_group_member_contacts_response(self._BODY)
        assert out[0]["contact_id"] == "795090251415976" and out[0]["emails"] == ["bill.burns@sccssar.org"]


class TestGroupMemberContactsProductionParity:
    """First production-reading pin in this file: exec the parser and the
    helpers it calls straight out of everbridge.py so the mirror above cannot
    drift silently."""

    @staticmethod
    def _src() -> str:
        return (Path(__file__).parent / "everbridge.py").read_text(encoding="utf-8")

    @staticmethod
    def _fn(src: str, name: str) -> str:
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(src, node)
        raise AssertionError(f"{name} not found in everbridge.py")

    def _prod(self):
        # `Optional` seeded because _parse_ocean_from_external_id's return
        # annotation names it; everything else the parser needs is exec'd in
        # dependency order into the same namespace.
        ns: dict = {"Optional": Optional}
        src = self._src()
        for name in (
            "_sort_emails_sccssar_first",
            "_extract_paths_emails",
            "_parse_ocean_from_external_id",
            "_parse_group_member_contacts_response",
        ):
            exec(self._fn(src, name), ns)
        return ns["_parse_group_member_contacts_response"]

    def test_production_matches_mirror_on_all_fixtures(self):
        prod = self._prod()
        bodies = [
            TestGroupMemberContactsCarryOcean._BODY,
            {"page": {"data": [
                {"id": "c1", "paths": [{"value": "bill@example.com"}]},
                {"id": "c2", "paths": [{"value": "dana@example.com"}, {"value": "5555555"}]},
            ]}},
            {"page": {"data": [{"id": "c1", "paths": [{"value": "4085551234"}]}]}},
            {"page": {"data": [
                {"paths": [{"value": "no@id.com"}]},
                {"id": "c2", "paths": [{"value": "ok@example.com"}]},
            ]}},
            {"page": {"data": []}},
            {"message": "OK"},
            {"page": {"data": [{"id": 99887766, "paths": [{"value": "a@b.com"}]}]}},
        ]
        for body in bodies:
            assert prod(body) == _parse_group_member_contacts_response(body)
        # The parity is not vacuous: the OCEAN fixture must actually produce the
        # new keys through the PRODUCTION function, not merely agree on [].
        assert prod(TestGroupMemberContactsCarryOcean._BODY)[0]["ocean"] == "305"


# ---------------------------------------------------------------------------
# _parse_discovery_response() — totalCount=0 → None semantics
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _extract_id_from_post_response() — POST response shape (live EB confirmed
# 2026-04-27 to return id at TOP level, not wrapped in `result`).
# ---------------------------------------------------------------------------

class TestExtractIdFromPostResponse:
    def test_top_level_id_unwrapped(self):
        # Confirmed live shape from POST /notificationEvents (2026-04-27 curl):
        #   {"message":"OK", "id": <int>, "baseUri":"...", "instanceUri":"..."}
        body = {
            "message":     "OK",
            "id":          7000000000000018,
            "baseUri":     "https://api.everbridge.net/rest/notificationEvents/<orgId>",
            "instanceUri": "https://api.everbridge.net/rest/notificationEvents/<orgId>/7000000000000018",
        }
        assert _extract_id_from_post_response(body) == "7000000000000018"

    def test_wrapped_in_result_also_supported(self):
        # Defensive — if EB introduces a wrapped form (or the docs were
        # right about a different endpoint that wraps), handle it cleanly.
        body = {"message": "OK", "result": {"id": 12345}}
        assert _extract_id_from_post_response(body) == "12345"

    def test_id_returned_as_string(self):
        # EB returns numeric ids; we must always return strings so caller
        # code (Firestore writes, Slack message text, etc.) doesn't have to
        # coerce.
        out = _extract_id_from_post_response({"id": 9876543210})
        assert isinstance(out, str)
        assert out == "9876543210"

    def test_string_id_preserved(self):
        # Defensive — if EB ever returns id as a string, pass through.
        out = _extract_id_from_post_response({"id": "abc-123"})
        assert out == "abc-123"

    def test_missing_id_raises_runtime_error_with_keys(self):
        # If neither top-level nor result.id is present, raise loud with
        # the body's keys so an operator reading logs can diagnose.
        # Cluster E (EB-L8) updated the wording to "missing or zero 'id'"
        # since id=0 is now also rejected.
        with pytest.raises(RuntimeError, match="missing or zero 'id'"):
            _extract_id_from_post_response({"message": "Some Error"})

    def test_error_includes_keys_for_diagnosis(self):
        # A real EB error response (when the API returns 2xx with an error
        # body) — ensure the operator-visible message lists what keys WERE
        # present so diagnosis is fast.
        body = {"message": "Validation failed", "errorCode": "INVALID_INPUT"}
        with pytest.raises(RuntimeError) as excinfo:
            _extract_id_from_post_response(body)
        msg = str(excinfo.value)
        assert "message" in msg
        assert "errorCode" in msg

    def test_non_dict_response_raises(self):
        # Defensive — if EB ever returns a list or scalar, fail loud rather
        # than crash with a less-informative AttributeError. The type-guard
        # error message says "not a dict" (more diagnostic than the
        # missing-id message which assumes dict shape).
        with pytest.raises(RuntimeError, match="not a dict"):
            _extract_id_from_post_response([])

    def test_non_dict_scalar_also_raises(self):
        with pytest.raises(RuntimeError, match="not a dict"):
            _extract_id_from_post_response("some string")
        with pytest.raises(RuntimeError, match="not a dict"):
            _extract_id_from_post_response(None)

    def test_result_wrapper_with_non_dict_inner_raises(self):
        # `{"result": "string"}` — defensive against a future schema change.
        with pytest.raises(RuntimeError, match="missing or zero 'id'"):
            _extract_id_from_post_response({"result": "not a dict"})

    def test_id_zero_rejected_as_missing(self):
        """Cluster E (EB-L8). Pre-fix `if nid is None` let `{"id": 0}` through
        as the string "0" — that produced a dead deep_link_url
        (/histories/report/0) and a poll_notification("0") that 404s.
        EB IDs are always positive in practice; treat 0 as missing for
        defensive parity."""
        with pytest.raises(RuntimeError, match="missing or zero 'id'"):
            _extract_id_from_post_response({"message": "OK", "id": 0})

    def test_id_zero_under_result_wrapper_also_rejected(self):
        """Same rejection regardless of envelope shape."""
        with pytest.raises(RuntimeError, match="missing or zero 'id'"):
            _extract_id_from_post_response({"message": "OK", "result": {"id": 0}})

    def test_existing_message_diagnostic_updated_to_or_zero(self):
        """The error message itself was updated; pin the new wording so a
        future revert is caught."""
        with pytest.raises(RuntimeError) as excinfo:
            _extract_id_from_post_response({"message": "Some Error"})
        assert "missing or zero 'id'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _parse_discovery_response() — totalCount=0 → None semantics
# ---------------------------------------------------------------------------

class TestParseDiscoveryResponse:
    def test_total_count_zero_returns_none(self):
        # Dispatcher hasn't pressed Send (or didn't manually link) — discovery
        # query returns totalCount=0. We MUST return None, not raise.
        assert _parse_discovery_response(
            {"page": {"totalCount": 0, "data": []}}
        ) is None

    def test_total_count_one_returns_id(self):
        body = {"page": {"totalCount": 1, "data": [{"id": "NOT_999"}]}}
        assert _parse_discovery_response(body) == "NOT_999"

    def test_handles_missing_page_key(self):
        assert _parse_discovery_response({"message": "OK"}) is None

    def test_returns_first_when_multiple(self):
        # Defensive — should never happen in practice (event names are unique
        # per HHMM suffix), but if Everbridge returns >1, take the first.
        body = {"page": {"totalCount": 2, "data": [
            {"id": "FIRST"}, {"id": "SECOND"},
        ]}}
        assert _parse_discovery_response(body) == "FIRST"


# ---------------------------------------------------------------------------
# _parse_poll_response() — terminal status + YES filter + cache signal
# ---------------------------------------------------------------------------

class TestParsePollResponseTerminal:
    """Phase 0 Task 8: notificationStatus is the stop signal — NOT `status`."""

    def test_completed_is_terminal(self):
        body = {"result": {
            "notificationStatus": "Completed",
            "notificationResult": {"allDetails": []},
        }}
        r = _parse_poll_response(body)
        assert r["is_terminal"] is True
        assert r["notif_status"] == "Completed"

    def test_stopped_is_terminal(self):
        body = {"result": {
            "notificationStatus": "Stopped",
            "notificationResult": {"allDetails": []},
        }}
        r = _parse_poll_response(body)
        assert r["is_terminal"] is True

    def test_active_is_not_terminal(self):
        body = {"result": {
            "notificationStatus": "Active",
            "notificationResult": {"allDetails": []},
        }}
        r = _parse_poll_response(body)
        assert r["is_terminal"] is False

    def test_inprogress_is_not_terminal(self):
        # Phase 0 Task 9: discovery query returns notificationStatus="Inprogress"
        # right after dispatcher Send. Must NOT terminate the poll.
        body = {"result": {
            "notificationStatus": "Inprogress",
            "notificationResult": {"allDetails": []},
        }}
        r = _parse_poll_response(body)
        assert r["is_terminal"] is False

    def test_status_A_field_is_ignored(self):
        # Phase 0 Task 8: parent `status: "A"` is "A" for every notification
        # regardless of lifecycle. ONLY notificationStatus terminates.
        body = {"result": {
            "status": "A",
            "notificationStatus": "Active",
            "notificationResult": {"allDetails": []},
        }}
        r = _parse_poll_response(body)
        assert r["is_terminal"] is False

    def test_handles_root_result_or_unwrapped(self):
        # Defensive — the PoC handled both `{"result": {...}}` and
        # `{"notificationStatus": ...}` shapes. Mirror that here.
        unwrapped = {"notificationStatus": "Completed",
                     "notificationResult": {"allDetails": []}}
        r = _parse_poll_response(unwrapped)
        assert r["is_terminal"] is True


class TestParsePollResponseAcks:
    def test_yes_responder_filtered_in(self):
        body = {"result": {
            "notificationStatus": "Active",
            "notificationResult": {"allDetails": [
                {"contactId": "c1", "firstName": "Bill", "lastName": "Burns",
                 "confirmed": True, "responseTextMessage": "Yes",
                 "callResultByPaths": [
                     {"pathText": "bill@example.com"},
                     {"pathText": "555-1212"},
                 ]},
            ]},
        }}
        r = _parse_poll_response(body)
        assert len(r["ack_contacts"]) == 1
        ack = r["ack_contacts"][0]
        assert ack["contact_id"] == "c1"
        assert ack["first_name"] == "Bill"
        assert ack["last_name"] == "Burns"
        assert "bill@example.com" in ack["emails"]
        # Phone path must NOT be included in emails list
        assert "555-1212" not in ack["emails"]

    def test_unconfirmed_filtered_out(self):
        # confirmed=False — responder ignored even if responseTextMessage=Yes.
        body = {"result": {
            "notificationStatus": "Active",
            "notificationResult": {"allDetails": [
                {"contactId": "c1", "confirmed": False,
                 "responseTextMessage": "Yes"},
            ]},
        }}
        r = _parse_poll_response(body)
        assert r["ack_contacts"] == []

    def test_no_response_filtered_out(self):
        # confirmed=True but answered No — responder ignored.
        body = {"result": {
            "notificationStatus": "Active",
            "notificationResult": {"allDetails": [
                {"contactId": "c1", "confirmed": True,
                 "responseTextMessage": "No"},
            ]},
        }}
        r = _parse_poll_response(body)
        assert r["ack_contacts"] == []

    def test_yes_match_is_case_insensitive(self):
        body = {"result": {
            "notificationStatus": "Active",
            "notificationResult": {"allDetails": [
                {"contactId": "c1", "confirmed": True,
                 "responseTextMessage": "  YES  "},
            ]},
        }}
        r = _parse_poll_response(body)
        assert len(r["ack_contacts"]) == 1


class TestParsePollResponseEmptyCacheSignal:
    """Phase 0 Task 8 finding: allDetails[] briefly empties at natural expiry
    BEFORE notificationStatus flips to Completed. Caller (main.py) caches
    last non-empty list; this parser just signals 'this cycle was empty'."""

    def test_empty_all_details_signals_caller_to_use_cache(self):
        body = {"result": {
            "notificationStatus": "Active",
            "notificationResult": {"allDetails": []},
        }}
        r = _parse_poll_response(body)
        assert r["all_details_empty"] is True
        assert r["ack_contacts"] == []

    def test_non_empty_all_details_does_not_signal(self):
        body = {"result": {
            "notificationStatus": "Active",
            "notificationResult": {"allDetails": [
                {"contactId": "c1", "confirmed": True,
                 "responseTextMessage": "Yes"},
            ]},
        }}
        r = _parse_poll_response(body)
        assert r["all_details_empty"] is False

    def test_missing_notification_result_is_empty_signal(self):
        # If the verbose response is malformed/missing notificationResult
        # entirely, treat as empty so caller falls through to cache.
        body = {"result": {"notificationStatus": "Active"}}
        r = _parse_poll_response(body)
        assert r["all_details_empty"] is True


class TestParsePollResponseDeclineCounts:
    """Issue #592 — explicit-decline (NO) + no-response counts. Verified against
    the real La Verne MA notification 7000000000000021 (2026-07-18): EB's
    `confirmed` flag means "responded" (Yes OR No), so the explicit-NO count can
    only be derived by parsing allDetails[], never from confirmedCount."""

    def _body(self, details):
        return {"result": {
            "notificationStatus": "Active",
            "notificationResult": {"allDetails": details},
        }}

    def test_explicit_no_counted_as_decline_not_ack(self):
        r = _parse_poll_response(self._body([
            {"contactId": "c1", "confirmed": True, "responseTextMessage": "No"},
        ]))
        assert r["decline_count"] == 1
        assert r["no_response_count"] == 0
        assert r["ack_contacts"] == []       # a decline is never a YES responder

    def test_unconfirmed_counted_as_no_response_not_decline(self):
        r = _parse_poll_response(self._body([
            {"contactId": "c1", "confirmed": False, "responseTextMessage": ""},
        ]))
        assert r["no_response_count"] == 1
        assert r["decline_count"] == 0

    def test_yes_is_neither_decline_nor_no_response(self):
        r = _parse_poll_response(self._body([
            {"contactId": "c1", "confirmed": True, "responseTextMessage": "Yes"},
        ]))
        assert r["decline_count"] == 0
        assert r["no_response_count"] == 0
        assert len(r["ack_contacts"]) == 1

    def test_confirmed_empty_text_inflates_no_bucket(self):
        # A bare receipt (confirmed but no option picked) is neither Yes, No,
        # nor no-response — it must NOT inflate the decline count.
        r = _parse_poll_response(self._body([
            {"contactId": "c1", "confirmed": True, "responseTextMessage": ""},
        ]))
        assert r["decline_count"] == 0
        assert r["no_response_count"] == 0

    def test_decline_match_is_case_insensitive_and_trims(self):
        r = _parse_poll_response(self._body([
            {"contactId": "c1", "confirmed": True, "responseTextMessage": "no"},
            {"contactId": "c2", "confirmed": True, "responseTextMessage": "  NO  "},
        ]))
        assert r["decline_count"] == 2

    def test_la_verne_real_distribution(self):
        # notif 7000000000000021: 1 Yes / 50 No / 17 no-response (totalCount 68).
        details = (
            [{"contactId": f"y{i}", "confirmed": True, "responseTextMessage": "Yes"}
             for i in range(1)]
            + [{"contactId": f"n{i}", "confirmed": True, "responseTextMessage": "No"}
               for i in range(50)]
            + [{"contactId": f"z{i}", "confirmed": False, "responseTextMessage": ""}
               for i in range(17)]
        )
        r = _parse_poll_response(self._body(details))
        assert len(r["ack_contacts"]) == 1
        assert r["decline_count"] == 50
        assert r["no_response_count"] == 17

    def test_empty_all_details_yields_zero_counts(self):
        r = _parse_poll_response(self._body([]))
        assert r["decline_count"] == 0
        assert r["no_response_count"] == 0
        assert r["all_details_empty"] is True


# ---------------------------------------------------------------------------
# Deep-link builders
# ---------------------------------------------------------------------------

class TestDeepLinkBuilders:
    def test_review_draft_url(self):
        assert review_draft_url("TPL_321") == \
            "https://manager.everbridge.net/bcTemplates/edit/TPL_321"

    def test_monitor_active_url(self):
        assert monitor_active_url("NOTIF_789") == \
            "https://manager.everbridge.net/histories/report/NOTIF_789"
