"""everbridge.py — Everbridge REST API client + helpers.

Phase 1 surface area:
- Constants: EVERBRIDGE_BASE_URL, SUPPRESSED_GROUP_IDS, _NOTIFICATION_BODY_TEMPLATE,
  CALLER_ID, DELIVER_PATHS
- Pure-logic helpers: strip_sar_prefix(), compose_notification_body(),
  _build_create_event_payload(), _build_send_notification_payload(),
  _build_template_payload(), _build_discovery_query_params(),
  _parse_poll_response(), _parse_groups_response(), _parse_contacts_response(),
  _parse_group_members_response(), _parse_discovery_response(),
  review_draft_url(), monitor_active_url()
- Auth header: _auth_header() reading EVERBRIDGE_CREDENTIALS env var
- Thin httpx wrappers: list_groups(), list_contacts(), list_group_members(),
  list_group_member_contacts(), get_contact_emails(),
  create_notification_event(), send_notification_live(),
  create_notification_template(), discover_notification_by_event(),
  poll_notification()

Test architecture decision (Task 1.6, with Bill 2026-04-26):
    Tests mirror the pure-logic helpers locally (constants, payload builders,
    response parsers) and assert on dict shape. Thin httpx wrappers are NOT
    exercised in local pytest because httpx is not installed in the local
    Python env (PEP 668 externally-managed). Wrappers are covered at live-test
    time on personal-dev. Revisit at end of Phase 1 (after Task 1.11) for a
    requirements-test.txt + venv-based pytest env.

PoC reference: experiments/everbridge_slack/poc_eb_slack.py (gitignored, in main
repo only). Phase 0 Tasks 8 + 9 exercised end-to-end notification creation +
polling against the live Everbridge org. Phase 1 ports that proven flow into
production code behind the EVERBRIDGE_MODE / SLACK_MODE feature gate.

CRITICAL — discovery query parameter (Task 9 / Phase 0 Key Discovery #8):
    Always use `?notificationEventId={id}`. The lookalikes `?eventId=`,
    `?event=`, and `?search={"notificationEventId": id}` are silently ignored
    and return ALL recent notifications instead of filtering. Pinned by a
    regression test in test_main_regression.py.

CRITICAL — group member lookup parameters (Task 1.7+):
    Always use groupId=<id>&byType=id (singular). groupIds (plural) returns silent 401
    with misleading error message. Confirmed across two PoC sessions (2026-04-21 + 2026-04-23).
"""
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVERBRIDGE_BASE_URL = "https://api.everbridge.net/rest"

# DESIGN DECISION (do not revert without team discussion):
# The ALERTSCC ADMIN group is filtered server-side from /everbridge-groups
# (its ID is supplied via EVERBRIDGE_SUPPRESSED_GROUP_IDS, not held here).
# It is an org-admin group, not a SAR paging target. Filter applied here so the ID never
# reaches the frontend or audit log selection set.
# Everbridge group IDs never paged (an admin/records group that must not receive
# a callout). Org-specific record IDs, so configuration rather than source --
# publishing them hands an outsider a map of another org's Everbridge estate.
# Comma-separated in EVERBRIDGE_SUPPRESSED_GROUP_IDS. Empty is legitimate and
# means "suppress nothing", so this one does NOT fail loud: a deployment with no
# admin group to hide is normal, and refusing to dispatch over it would be worse
# than sending one extra page.
SUPPRESSED_GROUP_IDS: frozenset[str] = frozenset(
    g.strip() for g in os.environ.get("EVERBRIDGE_SUPPRESSED_GROUP_IDS", "").split(",")
    if g.strip()
)

_NOTIFICATION_BODY_TEMPLATE = (
    "Need {resources_phrase} for a missing {age_phrase} in {city}. "
    "Please respond below with your availability.\n"
    "-##, {dispatcher_last_name}"
)

# Org-wide voice caller ID. A LIVE dialable number, so unlike the Everbridge
# record IDs above it is configuration, not a source constant — it must not ship
# in a public repo. Set via the EVERBRIDGE_CALLER_ID env var, declared in
# terraform/environments/<env>/{variables.tf,main.tf} with the value in the
# gitignored terraform.tfvars. Empty default so importing this module never
# requires it; validated at point of use, matching gemini.py and slack.py.
CALLER_ID = os.environ.get("EVERBRIDGE_CALLER_ID", "").strip()

# Delivery path records — REQUIRED in every notification + template payload
# (Phase 0 Key Discovery #2). Each entry needs both `id` (record ID) and
# `pathId` (path type). Without `id`, Everbridge returns HTTP 400
# "No DeliveryPath for notification".
#
# These are org-specific Everbridge record IDs and are configuration, not
# source: they are of no use to another Everbridge customer and, published,
# they describe this org's delivery estate. Supplied as a JSON array in
# EVERBRIDGE_DELIVER_PATHS; validated at point of use, so importing this module
# never requires it.
def _parse_deliver_paths(raw: str) -> list[dict]:
    """Parse EVERBRIDGE_DELIVER_PATHS. Returns [] on anything malformed.

    Deliberately does NOT raise here: import-time failure would take the whole
    service down for a config typo. _require_deliver_paths() raises instead, at
    payload-build time and therefore before the Everbridge POST.
    """
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.error("EVERBRIDGE_DELIVER_PATHS is not valid JSON — treating as unset")
        return []
    if not isinstance(parsed, list):
        logger.error("EVERBRIDGE_DELIVER_PATHS is not a JSON array — treating as unset")
        return []
    return parsed


DELIVER_PATHS: list[dict] = _parse_deliver_paths(
    os.environ.get("EVERBRIDGE_DELIVER_PATHS", "")
)

# Discovery query parameter name — pinned by regression test.
# Phase 0 Key Discovery #8: `?eventId=`, `?event=`, and `?search={"notificationEventId": id}`
# are silently ignored and return all recent notifications instead of filtering.
DISCOVERY_QUERY_PARAM = "notificationEventId"

# Notification types. Both are empirically-observed literals read off real
# notifications in the org, NOT guesses (#612 §2.2).
#
# Polling  — the dispatch original. Carries the Yes/No questionaire; the polling
#            chain watches exactly ONE of these per incident.
# Standard — the #612 follow-up. No questionaire, so there is nothing to poll,
#            which is precisely why it is the correct type: a second Polling
#            notification would collect YESes that no chain reads.
NOTIFICATION_TYPE_POLLING  = "Polling"
NOTIFICATION_TYPE_STANDARD = "Standard"

# SMS length limits, read off the Everbridge compose UI's own tooltip for the
# Standard message type (Bill, screenshot 2026-07-27):
#
#   "SMS messages will not exceed the standard 160 character limit.
#    SMS messages containing non-GSM-7 characters will be limited to 70
#    characters. This limit will include any auto generated text (ex.
#    Confirmation Instructions, Short URL, etc.). Messages longer than the
#    limit will include a link to the SMS Web Page Message."
#
# The last sentence is the operational stake: an over-length follow-up does not
# just wrap, it is REPLACED by a link the recipient has to tap and load — during
# an active callout, on whatever signal they have. Bill's requirement is to stay
# under 160 so the correction "is received easily and plainly".
#
# Verified against the same screenshot: with a 57-character body EB reported
# "103 - SMS" remaining. 57 + 103 = 160 exactly, and the 68-character TITLE was
# NOT counted — the SMS budget applies to the body alone.
#
# DESIGN DECISION (Bill, 2026-07-28) — WE ENFORCE A FLAT 160, ALWAYS.
# Everbridge really does drop to 70 for non-GSM-7 content, but we deliberately do
# NOT enforce that lower ceiling: international spellings are rare in a follow-up
# dispatch, and gating every message on the rare one is the wrong trade. 70 is
# simply too small to be a working limit for a correction.
#
# Accepted consequence, stated plainly: a body that keeps a non-GSM-7 character
# AND exceeds 70 characters will be truncated BY EVERBRIDGE into an SMS Web Page
# link. That is EB's behaviour, not ours, and we cannot prevent it — only warn.
# normalize_sms_text() already removes the COMMON causes (smart quotes, dashes,
# ellipsis), so what remains is genuinely-international letters and emoji.
# prepare_followup_sms_body() reports `truncation_risk` for that case so the
# dispatcher is informed rather than surprised — informed, not blocked.
SMS_LIMIT_GSM7    = 160

# Everbridge's real ceiling for non-GSM-7 content. Retained for the WARNING path
# only — it is deliberately NOT an enforcement limit (see the decision above).
SMS_LIMIT_UNICODE = 70

# Characters Everbridge appends to a Standard notification that has
# confirm: true, which count against the 160 even though EB's own compose-time
# counter does NOT subtract them.
#
# MEASURED on the first live follow-up, 2026-07-28 (personal-dev, 1.11.30).
# The delivered SMS was:
#
#     CANCEL - the test is now complete. -burns #305
#     Reply with YES to confirm receipt or https://evb.gg/0lgO2up3
#
# The second line plus its leading newline is 61 characters. Both parts are
# fixed-shape — the phrase is constant and the short link is always
# "https://evb.gg/" + 8 characters — so this is a stable reserve rather than an
# estimate. (For contrast the Polling original's block is 58: "Reply / 1 for Yes
# / 2 for No / or click <url>".)
#
# => the dispatcher's real budget is 160 - 61 = 99 characters.
#
# This is not academic: the actual 2026-07-24 correction (§2.7 of the design doc)
# is 96 characters and fits with 3 to spare. Telling a dispatcher they have 160
# would let a routine correction be truncated into a click-through link.
#
# Re-measure if EB's confirmation wording or short-link domain ever changes.
FOLLOWUP_SMS_RESERVE = 61

# GSM 03.38 default alphabet. Any character outside this set forces the whole
# message to UCS-2 and collapses the limit from 160 to 70 — which matters more
# than it sounds, because macOS and iOS substitute a curly apostrophe (U+2019)
# for a typed one by default. A dispatcher typing "reco's" naturally on a Mac can
# more than halve their own budget without seeing why.
_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

# Characters that ARE representable in GSM-7 but cost two septets each, because
# they are reached via the escape table.
_GSM7_EXTENDED = frozenset("^{}\\[~]|€\f")


# Punctuation that macOS/iOS (and pasted Word/Google-Docs text) substitute in
# automatically, mapped to GSM-7 equivalents. Flattening these is what keeps a
# dispatcher in the 160-character lane instead of the 70-character one, and it is
# invisible to them (Bill, 2026-07-27: "auto-normalize / flatten punctuation").
#
# Scoped to PUNCTUATION on purpose. Accented letters are deliberately NOT folded:
# à ä ö ñ ü é è ì ò ù Ç Å Ø Æ ß É are already GSM-7, so ordinary names survive
# untouched, and rewriting a name to buy septets is not a trade this code should
# make on the dispatcher's behalf.
_SMS_PUNCTUATION_NORMALIZATION: dict[str, str] = {
    # Curly single quotes / primes → apostrophe. THE common one: macOS turns a
    # typed ' into U+2019, which alone drops the ceiling from 160 to 70.
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    # Curly double quotes / guillemets → straight quote.
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "«": '"', "»": '"',
    # Dash family (en, em, figure, minus, horizontal bar) → hyphen.
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-",
    # Ellipsis → three periods. Costs 3 septets but keeps the message GSM-7.
    "…": "...",
    # Exotic spaces → plain space. Non-breaking space is a frequent paste artifact.
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    # Zero-width characters carry no meaning in an SMS but force UCS-2.
    "​": "", "‌": "", "‍": "", "﻿": "",
    # Bullets and separators.
    "•": "*", "·": ".", "⁄": "/",
}


def normalize_sms_text(text: str) -> str:
    """Flatten smart punctuation to GSM-7 so the 160-char ceiling applies.

    Applied BEFORE measuring — see prepare_followup_sms_body for why the order is
    load-bearing. Purely a transliteration: no content is added, removed, or
    reworded, which keeps it compatible with the §3.3 blank-slate decision (we
    are not composing on the dispatcher's behalf, only spelling their punctuation
    in a way the SMS channel can carry).
    """
    # CRLF → LF first: \r and \n are both GSM-7, but the pair costs two septets
    # for one line break.
    out = text.replace("\r\n", "\n")
    for src, dst in _SMS_PUNCTUATION_NORMALIZATION.items():
        if src in out:
            out = out.replace(src, dst)
    return out


def non_gsm7_characters(text: str) -> list[str]:
    """Distinct characters that would force the message to UCS-2, in order.

    Used to tell the dispatcher exactly WHICH characters cost them the 160-char
    budget, rather than reporting an unexplained limit drop. Empty list means the
    text is GSM-7-clean.
    """
    seen: list[str] = []
    for ch in text:
        if ch not in _GSM7_BASIC and ch not in _GSM7_EXTENDED and ch not in seen:
            seen.append(ch)
    return seen


def prepare_followup_sms_body(
    raw: str, *, reserve: int = FOLLOWUP_SMS_RESERVE
) -> dict:
    """Normalize, then measure, then decide — the whole #612 body policy.

    ⚠️ **Order is load-bearing.** Normalizing AFTER measuring would report a
    70-character ceiling for a body that becomes clean 160-character GSM-7 once
    flattened, blocking a dispatcher from sending something that was always fine.

    The 160/70 ceilings are hard-coded and enforced, not advisory (Bill,
    2026-07-27). `accepted=False` means the caller must refuse the send.

    `reserve` defaults to the MEASURED FOLLOWUP_SMS_RESERVE (61), so the real
    dispatcher budget is 99 characters, not 160. Everbridge appends its own
    confirmation line ("Reply with YES to confirm receipt or <short url>") which
    counts against the 160 even though EB's compose-time counter does not
    subtract it. Measured on the first live send 2026-07-28 — see the constant.

    The enforced ceiling is a FLAT 160 regardless of character set (Bill,
    2026-07-28) — 70 is too small to be a working limit for a correction, and
    international spellings are too rare to gate every dispatch on. See the
    SMS_LIMIT_GSM7 decision block.

    `truncation_risk` is the honest consequence of that call: EB itself still
    caps non-GSM-7 messages at 70 and replaces the overflow with an SMS Web Page
    link. We cannot prevent that, so we surface it. It is a WARNING, never a
    block — `accepted` is decided by the 160 limit alone.

    Returns:
        {
          "text":                 str        # normalized — send THIS, not `raw`
          "budget":               dict       # sms_budget() of the normalized text
          "offending_characters": list[str]  # non-GSM-7 survivors, for the UI
          "normalized":           bool       # True if flattening changed anything
          "truncation_risk":      bool       # EB will likely truncate — warn only
          "accepted":             bool       # governed by the flat 160
        }
    """
    text = normalize_sms_text(raw)
    budget = sms_budget(text, reserve=reserve)
    offenders = non_gsm7_characters(text)
    return {
        "text":                 text,
        "budget":               budget,
        "offending_characters": offenders,
        "normalized":           text != raw,
        "truncation_risk":      bool(offenders) and len(text) > SMS_LIMIT_UNICODE,
        "accepted":             not budget["over_limit"],
    }


def sms_budget(text: str, *, reserve: int = 0) -> dict:
    """Report the SMS character budget for a notification body.

    Mirrors Everbridge's own compose-time counter so the dispatcher-facing number
    matches what they would see in the EB UI, then allows a `reserve` on top.

    ⚠️ **EB's counter does NOT subtract auto-generated text**, even though the
    tooltip says the 160-character limit includes it. That was verified
    arithmetically: a 57-character body reported exactly 103 remaining. Our
    Standard follow-up sets `confirm: true`, so confirmation instructions WILL be
    appended and WILL count against the real limit. `reserve` exists to hold room
    for that; its correct value is not yet known empirically (it needs one live
    send to measure), so it defaults to 0 — i.e. parity with EB's counter — and
    the caller decides the policy.

    The limit is a FLAT 160 for both character sets (Bill, 2026-07-28) — we do
    NOT drop to Everbridge's 70-character UCS-2 ceiling. `charset` is still
    reported so callers can warn about EB-side truncation, but it no longer
    changes the budget.

    Returns:
        {
          "charset":    "gsm7" | "unicode"   # reporting only — does NOT set the limit
          "limit":      int   # always SMS_LIMIT_GSM7 minus reserve
          "used":       int   # extension chars count as 2 in gsm7
          "remaining":  int   # may be negative
          "over_limit": bool
        }
    """
    is_gsm7 = all(ch in _GSM7_BASIC or ch in _GSM7_EXTENDED for ch in text)
    if is_gsm7:
        # Escape-table characters occupy two septets each.
        used = sum(2 if ch in _GSM7_EXTENDED else 1 for ch in text)
    else:
        # UCS-2: one unit per character. EB would cap this at 70; we do not.
        used = len(text)
    limit = SMS_LIMIT_GSM7 - reserve
    return {
        "charset":    "gsm7" if is_gsm7 else "unicode",
        "limit":      limit,
        "used":       used,
        "remaining":  limit - used,
        "over_limit": used > limit,
    }


# ---------------------------------------------------------------------------
# Pure-logic helpers
# ---------------------------------------------------------------------------

def strip_sar_prefix(name: str) -> str:
    """Strip 'SAR - ' prefix from a group name. e.g. 'SAR - K9' → 'K9'."""
    return name[len("SAR - "):] if name.startswith("SAR - ") else name


def compose_notification_body(
    *,
    resource_types: list[str],
    age: Optional[int],
    city: str,
    dispatcher_last_name: str,
    template_type: str = "incounty",
) -> str:
    """Compose the Everbridge notification body per design Section 2.

    age=None falls back to 'unknown age person' so the message is still well-formed
    when the OCR'd Age field is empty. Dispatcher can edit before sending.

    template_type="mutualaid" prepends "(Mutual aid request) ".
    "All Members" alone omits the "teams" suffix (reads naturally without it).
    """
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


# ---------------------------------------------------------------------------
# Auth header
# ---------------------------------------------------------------------------

def _auth_header() -> dict:
    """Return Authorization header from EVERBRIDGE_CREDENTIALS env var.

    Secret is stored base64-encoded in Secret Manager — same format the PoC used
    via EVERBRIDGE_AUTH. We pass it directly as 'Authorization: Basic <value>'.
    """
    creds = os.environ.get("EVERBRIDGE_CREDENTIALS", "")
    if not creds:
        raise RuntimeError("EVERBRIDGE_CREDENTIALS env var not set")
    return {"Authorization": f"Basic {creds}", "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Pure-logic payload builders + response parsers (testable without httpx)
# ---------------------------------------------------------------------------

def _build_create_event_payload(org_id: str, event_name: str) -> dict:
    """Build POST /notificationEvents/{orgId} payload (Phase 0 Step 1)."""
    return {"organizationId": int(org_id), "name": event_name}


def _require_deliver_paths() -> list[dict]:
    """Return DELIVER_PATHS, or raise if unset or malformed.

    Raised while BUILDING the payload -- before the Everbridge POST -- so a
    misconfigured environment fails ahead of the point of no return. Everbridge
    answers a missing/incomplete deliverPaths with HTTP 400 "No DeliveryPath for
    notification" (Phase 0 Key Discovery #2), so the alternative is an opaque
    400 mid-dispatch.
    """
    if not DELIVER_PATHS:
        raise RuntimeError(
            "EVERBRIDGE_DELIVER_PATHS env var not set (or not a JSON array). "
            "Everbridge requires deliverPaths on every notification; declare it "
            "in terraform/environments/<env>/ and `terraform apply` (build "
            "scripts do NOT pick up Terraform changes)."
        )
    missing = [p for p in DELIVER_PATHS
               if not isinstance(p, dict) or "id" not in p or "pathId" not in p]
    if missing:
        raise RuntimeError(
            f"EVERBRIDGE_DELIVER_PATHS has {len(missing)} entr(y/ies) missing "
            f"'id' or 'pathId'. Everbridge returns HTTP 400 "
            f"'No DeliveryPath for notification' without both."
        )
    return DELIVER_PATHS


def _require_caller_id() -> str:
    """Return CALLER_ID, or raise if it is unset.

    Raised while BUILDING the payload, i.e. before the Everbridge POST -- so a
    misconfigured environment fails ahead of the point of no return rather than
    sending voice calls with a blank caller ID or eating an opaque EB 400.
    """
    if not CALLER_ID:
        raise RuntimeError(
            "EVERBRIDGE_CALLER_ID env var not set. It is the org-wide voice "
            "caller ID Everbridge requires on every notification; declare it in "
            "terraform/environments/<env>/ and `terraform apply` (build scripts "
            "do NOT pick up Terraform changes)."
        )
    return CALLER_ID


def _build_send_notification_payload(
    *,
    org_id: str,
    event_id: str,
    event_name: str,
    title: str,
    body: str,
    target_contact_ids: list[str],
    target_group_ids: list[str],
    category_id: int,
    include_launchtype: bool,
    notification_type: str = NOTIFICATION_TYPE_POLLING,
) -> dict:
    """Build POST /notifications/{orgId} or /notificationTemplates/{orgId} payload.

    The two endpoints share a single payload shape (Phase 0 Key Discovery #1):
    `sourceTemplateId` is ABSENT in both. The only difference is `launchtype`,
    which is present for the live /notifications endpoint (`SendNow`) and
    omitted for /notificationTemplates (Phase 0 Key Discovery #6).

    `notificationEventId` is included in BOTH (Phase 0 Key Discovery #5 + #6).
    The template stores it; the live notification path inherits it natively.

    `notification_type` defaults to Polling so every pre-#612 caller produces a
    byte-identical payload. NOTIFICATION_TYPE_STANDARD applies the §6.0 delta for
    the #612 follow-up — see _standard_type_payload. `category_id` is IGNORED
    for Standard (the field is dropped entirely); callers may pass anything.
    """
    payload: dict = {
        "organizationId": int(org_id),
        "notificationEventId": event_id,
        "notificationName": event_name,
        "type": notification_type,
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
            "deliverPaths": list(_require_deliver_paths()),
            "senderCallerInfos": [{
                "callerId": _require_caller_id(),
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
    if notification_type == NOTIFICATION_TYPE_STANDARD:
        payload = _standard_type_payload(payload)
    if include_launchtype:
        payload["launchtype"] = "SendNow"
    return payload


def _standard_type_payload(payload: dict) -> dict:
    """Return a NEW payload in the Standard shape, leaving the input untouched.

    (#612 §6.0. Made pure in response to an Aikido review finding on PR #657 —
    the original mutated its argument in place.)

    ⚠️ **A shallow `dict(payload)` is NOT sufficient here and would silently
    re-introduce the mutation.** Three of the five edits below land inside the
    nested `message` and `broadcastSettings` dicts, which a shallow copy shares
    with the caller — so `dict(payload)["message"].pop("questionaire")` removes
    the key from the CALLER's dict too. Both nested dicts are therefore copied
    before being modified. Pinned by
    TestStandardTypePayloadDelta::test_building_standard_does_not_mutate_the_input.

    Every line here is derived from the real Standard notification Everbridge
    ITSELF produced on 2026-07-24 (the hand-built follow-up
    to the SCPD Moreland callout) compared field-by-field against the Polling
    original) — not from Swagger, per Session Rule #10.

    - `categoryId` (top level AND inside `message`): dropped. Across all 306
      notifications in the org, ZERO of the 31 Standard notifications carry the
      key, while 114 of 275 Polling ones do. Whether POST *rejects* or merely
      *ignores* it on Standard is deliberately not relied upon — omitting the
      field removes the question instead of answering it.
    - `message.questionaire`: dropped. THIS IS THE LOAD-BEARING ONE. A Standard
      notification carrying a Yes/No questionnaire would show responders a prompt
      that NO polling chain reads (the chain watches exactly one notification_id
      — the original). They would answer, believe themselves confirmed, and never
      reach the Slack tally or D4H. That is the §2.2 failure mode reached by a
      different route, and it is invisible in testing because the payload is
      accepted and the notification looks fine.
    - `voiceMailOption` → MESSAGE_ONLY: the only broadcastSettings VALUE that
      differs between the two types (deep compare: 26 keys each, 25 shared).
    - `smsCallBack`: dropped — present on Polling, absent on Standard.

    `mobileSettings` appears on Standard and not Polling, but is server-populated;
    the builder does not set it and does not need to.
    """
    out = dict(payload)
    out.pop("categoryId", None)

    message = dict(payload["message"])
    message.pop("categoryId", None)
    message.pop("questionaire", None)
    out["message"] = message

    settings = dict(payload["broadcastSettings"])
    settings["voiceMailOption"] = "MESSAGE_ONLY"
    settings.pop("smsCallBack", None)
    out["broadcastSettings"] = settings

    return out


def _build_template_payload(
    *,
    org_id: str,
    event_id: str,
    event_name: str,
    title: str,
    body: str,
    target_contact_ids: list[str],
    target_group_ids: list[str],
    category_id: int,
) -> dict:
    """Build POST /notificationTemplates/{orgId} payload (safe-mode path).

    Identical to the live-send payload EXCEPT no `launchtype` field (Phase 0
    Key Discovery #6).
    """
    return _build_send_notification_payload(
        org_id=org_id, event_id=event_id, event_name=event_name,
        title=title, body=body,
        target_contact_ids=target_contact_ids,
        target_group_ids=target_group_ids,
        category_id=category_id,
        include_launchtype=False,
    )


def _build_discovery_query_params(event_id: str) -> dict:
    """Build query params for GET /notifications/{orgId}?notificationEventId={eventId}.

    Hardcoded param name — see CRITICAL block in module docstring + Phase 0
    Key Discovery #8. A future "cleanup" PR that switches to ?eventId= would
    silently break safe-mode discovery; the regression test in
    test_main_regression.py pins this literal.
    """
    return {DISCOVERY_QUERY_PARAM: event_id}


def _parse_groups_response(json_body: dict) -> list[dict]:
    """Filter SUPPRESSED_GROUP_IDS, normalize SAR- prefix.

    Returns: [{id: str, name: str}, ...]
    """
    raw = json_body.get("page", {}).get("data", []) or []
    out = []
    for g in raw:
        gid = str(g.get("id", ""))
        if gid in SUPPRESSED_GROUP_IDS:
            continue
        out.append({"id": gid, "name": strip_sar_prefix(g.get("name", ""))})
    return out


def _parse_contacts_response(json_body: dict) -> list[dict]:
    """Return ONLY {contact_id, display_name} — design Section 1 PII boundary.

    Email and phone deliberately NOT surfaced. The frontend typeahead only
    needs display_name; Everbridge dispatches by contact_id. Anything more
    leaves the backend.
    """
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


# ---------------------------------------------------------------------------
# OCEAN# parsing — issue #363, spike 2026-05-01.
#
# SCCSSAR encodes the dispatcher's OCEAN# (3-digit radio call sign) in the
# Everbridge `externalId` field as a literal "1O" prefix + 3 digits, e.g.
# Bill Burns = "1O305". Spike confirmed all 68 tested contacts had a 5-char
# externalId in this exact shape.
#
# Slice rule: external_id[-3:] (last 3 chars) keeps the parser robust to a
# future record with a longer prefix (e.g. "10O305"). Validate against
# ^\d{3}$ — anything non-numeric falls back to None and the caller leaves
# the existing "##" placeholder (or bare last name) in place. Convenience,
# not correctness.
# ---------------------------------------------------------------------------

def _parse_ocean_from_external_id(external_id) -> Optional[str]:
    """Return the trailing 3-digit OCEAN# from an externalId, or None.

    Validates the slice against ``^\\d{3}$``; returns None for missing,
    empty, too-short, or non-numeric tail values. Pure function.
    """
    if not external_id:
        return None
    s = str(external_id).strip()
    if len(s) < 3:
        return None
    tail = s[-3:]
    if not tail.isdigit():
        return None
    return tail


def _extract_dispatcher_ocean(json_body: dict, dispatcher_email: str) -> Optional[str]:
    """Return the dispatcher's OCEAN# from a raw list-contacts response, or None.

    Looks up the contact whose `paths` list contains a path value matching
    `dispatcher_email` (case-insensitive), then parses `externalId` via
    ``_parse_ocean_from_external_id``. Email never leaves this function —
    only the 3-digit OCEAN# (or None) is returned.

    Returns None when:
      - dispatcher_email is empty
      - no contact matches the email
      - matched contact has no externalId or it doesn't yield 3 digits
    """
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


def _sort_emails_sccssar_first(emails: list[str]) -> list[str]:
    """Return emails with @sccssar.org addresses first (preserve relative order otherwise).

    DESIGN DECISION (do not revert without team discussion): Slack lookup must
    always use the responder's @sccssar.org email — never the contact path they
    happened to reply on. Confirmed empirically 2026-04-30 against the live EB
    org: the API returns paths in the dispatcher-set "Order" column from the
    Everbridge UI, which dispatchers/admins can re-rank at any time. So
    positional ordering of `paths[]` cannot be trusted as a stable signal.

    By sorting at the extraction layer, every downstream consumer
    (contact_email_map[cid], ack["emails"], main.py::_apply_responder_diff,
    Slack lookup at main.py:4301 picking emails[0]) gets the right address
    naturally without each site needing its own filter.

    Sheriff-dept email (SAR coordinator) is the only documented exception —
    those contacts have no @sccssar.org email at all, so they fall through to
    preserved-original-order naturally. No need to identify a sheriff-dept
    domain literal.
    """
    sccssar = [e for e in emails if e.lower().endswith("@sccssar.org")]
    others  = [e for e in emails if not e.lower().endswith("@sccssar.org")]
    return sccssar + others


def _extract_paths_emails(contact: dict) -> list[str]:
    """Extract email address strings from a contact's `paths` field.

    Both GET /contacts/{orgId}/{id} and GET /contacts/groups/{orgId} return
    contact objects whose `paths` list has per-path dicts with a `value` key
    that holds the path value (email address, phone number, etc.).

    Result is sorted via _sort_emails_sccssar_first() so the @sccssar.org
    address (when present) is at index [0] — see that helper's docstring for
    why path-order from EB cannot be trusted positionally.
    """
    raw = [
        str(p.get("value", ""))
        for p in (contact.get("paths") or [])
        if "@" in str(p.get("value", ""))
    ]
    return _sort_emails_sccssar_first(raw)


def _parse_group_members_response(json_body: dict) -> list[str]:
    """Return a list of contact ID strings from GET /contacts/groups/{orgId}.

    Same page.data shape as _parse_contacts_response — contact ID is at
    the top-level `id` field of each element.  Only the ID is extracted;
    PII fields (names, paths) are discarded.
    """
    raw = json_body.get("page", {}).get("data", []) or []
    return [str(c["id"]) for c in raw if c.get("id")]


def _parse_group_member_contacts_response(json_body: dict) -> list[dict]:
    """Return [{contact_id, emails, external_id, ocean, display_name}] from
    GET /contacts/groups/{orgId}.

    Same endpoint as _parse_group_members_response but also extracts email
    addresses from `paths` (contact_id → emails map for Slack invite lookups
    without relying on poll-response callResultByPaths), plus — for the
    off-call exclusion (2026-09-12) — the OCEAN# parsed from externalId (the
    join key to D4H `member.ref`) and a display name for the Event Log /
    tally line. Server-side only: this projection is never returned to the
    browser and never logged.
    """
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


def _extract_id_from_post_response(json_body: dict) -> str:
    """Extract the new resource id from POST /notificationEvents,
    /notifications, or /notificationTemplates response bodies.

    Live API behavior (confirmed via direct curl 2026-04-27): id is at the
    top level of the response, alongside `message`, `baseUri`, `instanceUri`:

        {"message":"OK", "id": <notification id>, "baseUri":"...", "instanceUri":"..."}

    The original Phase 0 plan documented this as `result.id`, which Phase 1
    code took to mean a wrapped `{"result": {"id": ...}}` envelope. The plan
    docstring was actually saying "the resulting id is named notificationEventId"
    — there is no `result` wrapper. This helper handles both shapes
    (defensively — a future API upgrade or schema change won't silently
    break parsing) and raises a clear RuntimeError with the response shape
    if `id` is genuinely missing.
    """
    if not isinstance(json_body, dict):
        raise RuntimeError(
            f"Everbridge POST response is not a dict (got {type(json_body).__name__})"
        )
    inner = json_body.get("result", json_body)
    nid = inner.get("id") if isinstance(inner, dict) else None
    # Cluster E (EB-L8): explicit `nid in (None, 0)` rather than `nid is None`.
    # If EB ever returned `{"id": 0}` (API bug, error envelope with status 200,
    # or schema change), the prior None-check would let it through. Downstream
    # `notification_id="0"` builds a dead deep_link_url (/histories/report/0)
    # and poll_notification("0") gets a 404 or unrelated record. EB IDs are
    # always positive in practice; treat 0 as "missing" for defensive parity.
    if nid is None or nid == 0:
        raise RuntimeError(
            f"Everbridge POST response missing or zero 'id' (top-level or "
            f"under 'result'); keys={list(json_body.keys())}"
        )
    return str(nid)


def _parse_discovery_response(json_body: dict) -> Optional[str]:
    """Return notification_id from a discovery query, or None when totalCount=0.

    Phase 0 Key Discovery #6: only returns 1 when the dispatcher has manually
    checked "Include as part of an event" + picked the matching event in the
    Everbridge UI at Send time. Otherwise totalCount=0 and we return None.
    """
    page = json_body.get("page", {}) or {}
    total = page.get("totalCount", 0)
    if not total:
        return None
    data = page.get("data", []) or []
    if not data:
        return None
    return str(data[0].get("id", "")) or None


def _parse_poll_response(json_body: dict) -> dict:
    """Parse GET /notifications/{orgId}/{nid}?verbose=true response.

    Returns:
        {
          "is_terminal":   bool       # True iff notificationStatus in {Completed, Stopped}
          "notif_status":  str        # raw notificationStatus string
          "ack_contacts":  list[dict] # YES responders (filtered by confirmed + Yes text)
          "decline_count": int        # explicit NOs (confirmed + non-Yes option) — issue #592
          "no_response_count": int    # sent-but-unconfirmed rows in allDetails[]
          "all_details_empty": bool   # True when allDetails[] is empty THIS cycle
        }

    Filter logic (matches PoC `get_yes_responders`, both conditions required):
        confirmed == True AND responseTextMessage.lower() == "yes"

    Decline / no-response classification (issue #592) — verified against real
    telemetry (a mutual-aid callout, 2026-07-18):
      - EB's `confirmed` flag means "responded" (Yes OR No), NOT "said yes":
        confirmedCount=51 there was 1 Yes + 50 No. So the explicit-NO count
        CANNOT be read from the top-level notificationResult rollup
        (confirmedCount conflates Yes and No) — it must be counted here.
      - decline = confirmed AND a non-empty option that isn't "yes".
      - no_response = a row present in allDetails[] but not confirmed. All 68
        contacts (incl. the 17 no-responses) appeared in allDetails[], so this
        is derivable from the same iteration rather than a separate field.
      - Both counts are derived from the SAME allDetails iteration as
        ack_contacts, so they share its last-non-empty staleness behavior: the
        /poll-incident caller persists them only on non-empty cycles (see
        `all_details_empty`), leaving the last good value in Firestore across
        the natural-expiry empty cycles.
      - COUNTS ONLY — no decline contact identities are captured. Declines and
        non-responses surface in the Slack #active-incidents tally and nowhere
        else; they are NEVER sent to D4H as ABSENT records. Issue #442 (per-
        decline ABSENT sync) was DECLINED per Bill 2026-07-19 — negative
        replies and non-responses are not wanted in D4H. Do not extend this to
        emit decline contacts without first revisiting that decision.

    NOTE: the last-non-empty caching for the YES-count regression at natural
    expiry (Phase 0 Task 8 finding) lives in main.py /poll-incident handler,
    NOT here. This parser reports exactly what THIS cycle returned —
    `all_details_empty=True` is the signal the caller uses to fall back to
    its cached list.
    """
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
            # Sent but not yet responded — the "no response" bucket.
            no_response_count += 1
        elif not is_yes and response_text:
            # Responded with an explicit non-Yes option = decline (the NO count).
            # A confirmed row with empty response_text (a bare receipt) is
            # counted in none of the three buckets — it is neither Yes, No, nor
            # a clean no-response.
            decline_count += 1
        if not is_yes:
            continue
        # Emails — only paths attempted in this notification.
        # Sort sccssar.org first so emails[0] is reliable for Slack lookup
        # (see _sort_emails_sccssar_first docstring — EB returns paths in
        # dispatcher-set UI order, which is volatile).
        emails = _sort_emails_sccssar_first([
            p["pathText"]
            for p in (c.get("callResultByPaths") or [])
            if "@" in str(p.get("pathText", ""))
        ])
        ack_contacts.append({
            "contact_id":    str(c.get("contactId", "")),
            "first_name":    c.get("firstName", ""),
            "last_name":     c.get("lastName", ""),
            "emails":        emails,
        })

    return {
        "is_terminal":       notif_status in ("Completed", "Stopped"),
        "notif_status":      notif_status,
        "ack_contacts":      ack_contacts,
        "decline_count":     decline_count,
        "no_response_count": no_response_count,
        "all_details_empty": len(all_details) == 0,
    }


def partition_contacts_for_followup(json_body: dict) -> dict:
    """Partition a verbose notification response into #612 follow-up buckets.

    Deliberately SEPARATE from _parse_poll_response, which is left byte-identical
    (§3.2). That function feeds the polling chain and the D4H path, and its
    docstring carries the #442 guardrail against emitting decline identities. This
    one is a one-shot dispatcher action that can afford its own fresh GET, so the
    two never share state and the #442 boundary stays intact.

    **The #442 boundary, stated precisely:** this helper DOES compute decliner
    identities, but only to EXCLUDE those people from an Everbridge re-page.
    Excluding a decliner from a re-page is a different act from reporting their
    decline to D4H; Bill's 2026-07-25 direction authorises the former, and the
    latter remains declined. Nothing here may be routed to d4h.py.

    ⚠️ **Never call these people "confirmed."** Everbridge's `confirmed` flag means
    *responded* — Yes OR No. On the 07-24 original, `confirmedCount=36` was 10 Yes
    + 26 No (#592). Targeting `confirmed == True` would re-page all 26 who had
    already declined — the exact inverse of the intent, and it would look correct
    in every unit test written against a fixture where everyone said yes.

    Buckets, per §3.1:
      (a) affirmatively_replied — responded AND the option text is "yes"
      (b) not_yet_replied       — no response recorded at all
          declined              — responded with a non-Yes option (EXCLUDED)

    Buckets are resolved PER CONTACT, and a contact holding more than one row is
    decided by their MOST RECENT answer (`confirmedDate`, epoch ms) — Bill,
    2026-07-27. The four lists therefore partition the roster exactly once each.
    Empirically each contact occupies exactly one row (verified against both 07-24
    notifications), so the tie-break is defensive rather than a live path.

    Returns:
        {
          "followup_contact_ids": list[str]  # (a) + (b), de-duped, order-stable
          "affirmatively_replied": list[str]
          "not_yet_replied":       list[str]
          "declined":              list[str]
          "bare_receipt":          list[str]  # see below — also excluded
          "all_details_empty":     bool
        }

    **Bare receipts are excluded, and that is a literal reading of the spec.** A
    row that is `confirmed` with an EMPTY option text said neither Yes nor No, so
    it is in neither (a) (not "yes") nor (b) (it IS confirmed). §2.3 observed zero
    of these on 07-24 so the case has never occurred in production. It is surfaced
    as its own list rather than silently dropped: if a real incident ever produces
    a non-empty `bare_receipt`, that is the signal to revisit whether those people
    should receive the correction. Missing a correction is worse than receiving a
    redundant one, so this is the bucket to re-examine first.
    """
    notif = json_body.get("result", json_body)
    nr = notif.get("notificationResult") or {}
    all_details = nr.get("allDetails") or []

    # Fold PER CONTACT, resolving any multi-row contact by MOST RECENT ANSWER
    # (Bill, 2026-07-27: "I would always take the most recent answer as
    # authoritative").
    #
    # Empirically, a contact occupies exactly ONE row: verified 2026-07-27 against
    # both 07-24 notifications — the Polling original had 44 rows / 44 distinct
    # contacts and the Standard follow-up 18 / 18, with zero contacts holding
    # conflicting answers. Everbridge does not appear to permit re-answering a
    # poll. So the tie-break below is defensive, not a live code path.
    #
    # It is still worth having, and worth being CORRECT rather than merely
    # arbitrary, because the cost of guessing wrong is paging someone who
    # withdrew (or missing someone who joined) during an active callout.
    #
    # Recency comes from `confirmedDate` (epoch ms, present on exactly the rows
    # that responded — 36 of 44 on the original). Array order is NOT used: EB
    # does not document an ordering guarantee, so relying on position would make
    # the result depend on an unspecified server behaviour.
    by_contact: dict[str, list[dict]] = {}
    order: list[str] = []
    for c in all_details:
        contact_id = str(c.get("contactId", "")).strip()
        if not contact_id:
            # No id means we cannot target them; skip rather than emit "".
            continue
        if contact_id not in by_contact:
            by_contact[contact_id] = []
            order.append(contact_id)
        by_contact[contact_id].append(c)

    def _answered_at(row: dict) -> int:
        """Epoch-ms of the response; -1 for a row carrying no answer.

        A row with no `confirmedDate` is either unanswered or malformed, so it
        must never outrank a genuine dated answer.
        """
        raw = row.get("confirmedDate")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return -1

    def _bucket(cid: str) -> str:
        # max() is stable, so among rows tied at the same timestamp (or all
        # undated) the first-seen row wins — an arbitrary but deterministic pick.
        winner = max(by_contact[cid], key=_answered_at)
        if not bool(winner.get("confirmed")):
            return "not_yet_replied"
        response_text = str(winner.get("responseTextMessage", "")).strip()
        if response_text.lower() == "yes":
            return "affirmatively_replied"
        if response_text:
            return "declined"
        return "bare_receipt"

    buckets = {cid: _bucket(cid) for cid in order}
    affirmatively_replied = [c for c in order if buckets[c] == "affirmatively_replied"]
    not_yet_replied       = [c for c in order if buckets[c] == "not_yet_replied"]
    declined              = [c for c in order if buckets[c] == "declined"]
    bare_receipt          = [c for c in order if buckets[c] == "bare_receipt"]

    # Every contact lands in exactly one bucket, so this is already de-duped and
    # the four lists partition the roster — the caller can trust the counts to sum.
    followup_contact_ids = [
        c for c in order
        if buckets[c] in ("affirmatively_replied", "not_yet_replied")
    ]

    return {
        "followup_contact_ids":  followup_contact_ids,
        "affirmatively_replied": affirmatively_replied,
        "not_yet_replied":       not_yet_replied,
        "declined":              declined,
        "bare_receipt":          bare_receipt,
        "all_details_empty":     len(all_details) == 0,
    }


# ---------------------------------------------------------------------------
# Deep-link builders (pure)
# ---------------------------------------------------------------------------

def review_draft_url(template_id: str) -> str:
    """Everbridge UI deep link to review/edit/send a notification template."""
    return f"https://manager.everbridge.net/bcTemplates/edit/{template_id}"


def monitor_active_url(notification_id: str) -> str:
    """Everbridge UI deep link to monitor an active notification's responder report."""
    return f"https://manager.everbridge.net/histories/report/{notification_id}"


def _build_end_notification_url(org_id: str, notification_id: str) -> str:
    """Build URL for stopping an active notification (GET-modify-PUT pattern).

    Endpoint: PUT /notifications/{orgId}/{notificationId}
    Same URL pattern as the GET poll endpoint — the HTTP verb (PUT) is what
    distinguishes a stop from a read. Pinned by
    test_main_regression.py::TestEverbridgeEndNotificationUrl.

    Verified empirically against live Swagger 2026-04-28 — see CLAUDE.md
    Locked Design Decision (Everbridge stop notification — GET-modify-PUT).
    """
    return f"{EVERBRIDGE_BASE_URL}/notifications/{org_id}/{notification_id}"


# Substrings in EB 400 response messages that mean "already stopped — treat as success".
# Matched case-insensitively against the JSON message field. Pinned in regression tests.
_END_NOTIFICATION_ALREADY_STOPPED_MARKERS: tuple[str, ...] = (
    "is stopped",
    "not in progress",
)


# ---------------------------------------------------------------------------
# Response-body-logging raise helper
# ---------------------------------------------------------------------------

# Cap on the body fragment we log for failed EB responses. EB error bodies are
# typically small JSON envelopes (~200–500 chars), but a runaway HTML gateway
# error page could be huge. 800 chars covers every real EB error message we've
# seen empirically (Phase 0 + Phase 1 live tests) plus headroom.
_EB_ERROR_BODY_LOG_CAP = 800


def _log_and_raise_for_status(resp: "httpx.Response", op_label: str) -> None:
    """Call resp.raise_for_status() but log the response body first on 4xx/5xx.

    Without this, our wrappers swallow the EB error message — we see only
    `httpx.HTTPStatusError: Client error '400 Bad Request' for url '...'`
    in Cloud Run logs, which is useless for diagnosis. EB error bodies tell
    us *why* (duplicate event name, missing DeliveryPath, expired auth,
    etc.) — the body is the actionable signal.

    Logged at WARNING for 4xx (caller-side issue, often recoverable) and
    ERROR for 5xx (EB-side issue, escalate to ops). PII risk is bounded:
    the body is the EB API's own error envelope, not user data.

    `op_label` is a short string describing the call site
    (e.g. "create_notification_event", "send_notification_live") so log
    queries can filter by operation when triaging.

    Idempotent on success — no-op when status_code < 400.
    """
    sc = resp.status_code
    if sc < 400:
        return
    # Truncate to avoid log bloat from runaway HTML error pages. We keep
    # repr() form so non-printable bytes don't break the log line.
    raw = resp.text or ""
    body_fragment = raw[:_EB_ERROR_BODY_LOG_CAP]
    truncated = "(truncated)" if len(raw) > _EB_ERROR_BODY_LOG_CAP else ""
    log_fn = logger.error if sc >= 500 else logger.warning
    log_fn(
        "Everbridge %s failed: status=%d url=%s body=%r%s",
        op_label, sc, str(resp.request.url), body_fragment, truncated,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Thin httpx wrappers — NOT covered by local pytest (httpx not in local env).
# Covered at live-test time on personal-dev. Each wrapper is minimal: build
# URL → httpx call → parse via the pure helper above. Logic-bearing changes
# go in the helpers, not here.
# ---------------------------------------------------------------------------

def list_groups(org_id: str) -> list[dict]:
    """Fetch groups, filter suppress list, normalize SAR- prefix.

    pageSize=1000 matches the list_contacts() precedent and pre-empts silent
    truncation. EB's default page size is 100; SCCSSAR currently has ~12
    visible groups so we're nowhere near it, but a future SCCSSAR rollout
    could add many groups and a silently-dropped group at slot 101 would be
    invisible at the dispatch console with no error.

    Visibility note: the result reflects what the authenticated SA persona
    (SHO-SAR Dispatcher) has been *explicitly granted* read access to.
    New EB groups do NOT auto-inherit visibility — they must be granted by
    the EB org admin before they reach this list. See ops-runbook.md
    "Everbridge group not visible in dispatch console" for the diagnostic +
    resolution flow.
    """
    url = f"{EVERBRIDGE_BASE_URL}/groups/{org_id}"
    resp = httpx.get(
        url, headers=_auth_header(), timeout=15.0,
        params={"pageSize": 1000},
    )
    _log_and_raise_for_status(resp, "list_groups")
    return _parse_groups_response(resp.json())


def _fetch_contacts_raw(org_id: str) -> dict:
    """Single httpx GET against /contacts/{orgId} — returns raw JSON body.

    Split out from list_contacts() so callers that need additional fields
    (e.g. dispatcher OCEAN# lookup via _extract_dispatcher_ocean) can derive
    them from the same fetch without a second EB API round-trip.
    """
    url = f"{EVERBRIDGE_BASE_URL}/contacts/{org_id}"
    resp = httpx.get(
        url, headers=_auth_header(), timeout=30.0,
        params={"pageSize": 1000},
    )
    _log_and_raise_for_status(resp, "list_contacts")
    return resp.json()


def list_contacts(org_id: str) -> list[dict]:
    """Fetch contacts (PII-minimal: id + display_name only)."""
    return _parse_contacts_response(_fetch_contacts_raw(org_id))


def list_contacts_with_dispatcher(
    org_id: str, dispatcher_email: str
) -> tuple[list[dict], Optional[str]]:
    """Fetch contacts + the caller's OCEAN# in a single EB API call.

    Returns ``(projected_contacts, dispatcher_ocean_or_None)``. The projection
    side honors the same PII boundary as ``list_contacts``; the OCEAN# side
    looks the caller up by email in the raw `paths` field but never returns
    the email itself — only the 3-digit OCEAN# (or None).
    """
    raw = _fetch_contacts_raw(org_id)
    return _parse_contacts_response(raw), _extract_dispatcher_ocean(raw, dispatcher_email)


def list_group_members(org_id: str, group_id: str) -> list[str]:
    """Fetch contact IDs for a single group (PII-free: IDs only).

    Used at send time to build the contact_id → [group_names] map stored on
    the Firestore incident doc, which lets _apply_responder_diff populate each
    responder's groups field for the per-group tally breakdown in #active-incidents.

    CRITICAL param spelling: `groupId` (singular), NOT `groupIds`.
    `groupIds` is silently ignored by the EB API (returns all contacts, no filter).
    """
    url = f"{EVERBRIDGE_BASE_URL}/contacts/groups/{org_id}"
    resp = httpx.get(
        url, headers=_auth_header(), timeout=15.0,
        params={"byType": "id", "groupId": group_id, "groupName": 0, "pageSize": 1000},
    )
    _log_and_raise_for_status(resp, "list_group_members")
    return _parse_group_members_response(resp.json())


def list_group_member_contacts(org_id: str, group_id: str) -> list[dict]:
    """Fetch [{contact_id, emails, external_id, ocean, display_name}] for group members.

    Same endpoint and params as list_group_members() but calls
    _parse_group_member_contacts_response so the caller gets email addresses
    alongside contact IDs. Used in /send-notification Step 9 to populate BOTH
    contact_group_map (per-group tally) and contact_email_map (Slack invite
    fallback) in a single API call per group.

    The email snapshot avoids the callResultByPaths gap: EB only includes paths
    it actually attempted delivery on, so a contact who answers SMS before EB
    tries their email path has no email in poll-response data.
    """
    url = f"{EVERBRIDGE_BASE_URL}/contacts/groups/{org_id}"
    resp = httpx.get(
        url, headers=_auth_header(), timeout=15.0,
        params={"byType": "id", "groupId": group_id, "groupName": 0, "pageSize": 1000},
    )
    _log_and_raise_for_status(resp, "list_group_member_contacts")
    return _parse_group_member_contacts_response(resp.json())


def get_contact_emails(org_id: str, contact_id: str) -> list[str]:
    """Fetch email addresses for a single directly-targeted contact.

    Used in /send-notification Step 9 to populate contact_email_map for
    contacts targeted individually (not via a group). Falls back silently
    to [] if the contact has no email paths configured.

    Cluster E (EB-M7) — contract on empty return: `[]` is a REAL and EXPECTED
    outcome, not an error. SMS-only and voice-only contacts have no email
    paths in EB; the contact lookup succeeds but no addresses are surfaced.
    The /send-notification caller (main.py:~4945) guards with `if emails:`
    before populating `contact_email_map[cid]`, so an SMS-only contact ends
    up not in the map — that's intentional. On Slack-invite time, the caller
    falls back to `callResultByPaths`, which similarly has no email entry
    (EB never tried email for that contact) — net behavior: SMS-only
    contacts respond YES, get counted in the tally, but are NOT auto-invited
    to the per-incident Slack channel. The admin email hygiene rule
    (CLAUDE.md "Slack email lookup uses @sccssar.org email") is what closes
    this gap in practice — when a responder shows "Cannot invite" in Slack,
    the on-call adds their @sccssar.org email to their EB account.

    Endpoint choice — DESIGN DECISION (do not revert without team discussion):
    Uses the LIST endpoint with `contactIds` filter, NOT the path-based
    GET /contacts/{orgId}/{contactId}. The SHO-SAR Dispatcher SA role lacks
    per-method permission on the path-based endpoint (returns HTTP 401
    "User does not have API permissions for this method"). Verified
    empirically 2026-04-30 against the live SHO-SAR Dispatcher SA: every
    embed/expand/include/byType variant on the path-based URL returned the
    same 401, but the list endpoint with `contactIds` filter returns 200
    with paths populated — same response shape as list_group_member_contacts.

    CRITICAL param spelling — same gotcha as list_group_members (groupId vs
    groupIds): the filter MUST be `contactIds` (PLURAL). The singular
    `contactId` is silently ignored — Everbridge returns 200 with the FULL
    contact list (68 items in this org) instead of filtering. `byType=id` is
    required for both. Confirmed 2026-04-30 in experiments/everbridge_slack
    (gitignored spike).

    Symptom this fixes: a contact-direct send on the 2026-04-30 multi-recipient
    live test failed to invite that responder to Slack
    with "Cannot invite" — get_contact_emails raised HTTPStatusError, the
    caller silently swallowed it, and contact_email_map had no entry for her.
    The group-send path through list_group_member_contacts kept working
    because that endpoint IS permitted for the SA role.
    """
    url = f"{EVERBRIDGE_BASE_URL}/contacts/{org_id}"
    resp = httpx.get(
        url, headers=_auth_header(), timeout=15.0,
        params={"byType": "id", "contactIds": contact_id, "pageSize": 1},
    )
    _log_and_raise_for_status(resp, "get_contact_emails")
    items = _parse_group_member_contacts_response(resp.json())
    if not items:
        return []
    # contactIds filter narrows to 1 item — return its emails directly.
    return items[0].get("emails", [])


def create_notification_event(org_id: str, event_name: str) -> str:
    """POST /notificationEvents/{orgId} — returns event_id (Phase 0 Step 1).

    Event names MUST be unique. Caller appends an HHMM suffix per the Event
    Name Format convention in the integration plan to guarantee uniqueness.
    """
    url = f"{EVERBRIDGE_BASE_URL}/notificationEvents/{org_id}"
    payload = _build_create_event_payload(org_id, event_name)
    resp = httpx.post(url, headers=_auth_header(), json=payload, timeout=15.0)
    _log_and_raise_for_status(resp, "create_notification_event")
    return _extract_id_from_post_response(resp.json())


def send_notification_live(
    *,
    org_id: str,
    event_id: str,
    event_name: str,
    title: str,
    body: str,
    target_contact_ids: list[str],
    target_group_ids: list[str],
    category_id: int,
) -> str:
    """POST /notifications/{orgId} — fires live notification.

    Returns notification_id. Caller is responsible for safe-list gating via
    _route_send() in main.py (Task 1.9). This function is unconditional —
    it WILL fire to the targeted contacts/groups when called.
    """
    url = f"{EVERBRIDGE_BASE_URL}/notifications/{org_id}"
    payload = _build_send_notification_payload(
        org_id=org_id, event_id=event_id, event_name=event_name,
        title=title, body=body,
        target_contact_ids=target_contact_ids,
        target_group_ids=target_group_ids,
        category_id=category_id,
        include_launchtype=True,
    )
    resp = httpx.post(url, headers=_auth_header(), json=payload, timeout=30.0)
    _log_and_raise_for_status(resp, "send_notification_live")
    return _extract_id_from_post_response(resp.json())


def send_followup_notification_live(
    *,
    org_id: str,
    event_id: str,
    event_name: str,
    title: str,
    body: str,
    target_contact_ids: list[str],
) -> str:
    """POST /notifications/{orgId} — fires a live Standard follow-up (#612).

    A follow-up is a NEW notification under the EXISTING notificationEventId, not
    a new event (§2.1). `event_id` is therefore the incident's already-persisted
    `everbridge_event_id`.

    Deliberately narrower than send_notification_live:

    - **No `target_group_ids` parameter at all.** Groups are out of scope for the
      follow-up (Bill, 2026-07-25): it targets individuals only, and `groupIds` is
      always empty. Omitting the parameter rather than defaulting it to [] means a
      future caller cannot re-introduce group targeting by passing one.
    - **No `category_id` parameter.** Standard drops the field (§6.0), so accepting
      one would imply it mattered. 0 is passed through the shared builder purely to
      satisfy its signature and is discarded by _standard_type_payload.

    Like send_notification_live this is UNCONDITIONAL — safe-list gating via
    _route_send() is the caller's responsibility. Returns the notification_id,
    which the caller MUST persist immediately (Cluster C).
    """
    url = f"{EVERBRIDGE_BASE_URL}/notifications/{org_id}"
    payload = _build_send_notification_payload(
        org_id=org_id, event_id=event_id, event_name=event_name,
        title=title, body=body,
        target_contact_ids=target_contact_ids,
        target_group_ids=[],
        category_id=0,
        include_launchtype=True,
        notification_type=NOTIFICATION_TYPE_STANDARD,
    )
    resp = httpx.post(url, headers=_auth_header(), json=payload, timeout=30.0)
    _log_and_raise_for_status(resp, "send_followup_notification_live")
    return _extract_id_from_post_response(resp.json())


def create_notification_template(
    *,
    org_id: str,
    event_id: str,
    event_name: str,
    title: str,
    body: str,
    target_contact_ids: list[str],
    target_group_ids: list[str],
    category_id: int,
) -> str:
    """POST /notificationTemplates/{orgId} — pre-filled review-and-send form.

    Returns template_id. Caller deep-links the dispatcher to
    `review_draft_url(template_id)` and schedules a +30min Cloud Task to
    DELETE the template unconditionally.

    Phase 0 Key Discovery #6: the template stores notificationEventId, but
    Everbridge does NOT auto-honor it at Send. The dispatcher MUST manually
    check "Include as part of an event" + pick the event from the picker.
    Without that manual UI action, the resulting notification cannot be
    discovered by safe-mode polling.
    """
    url = f"{EVERBRIDGE_BASE_URL}/notificationTemplates/{org_id}"
    payload = _build_template_payload(
        org_id=org_id, event_id=event_id, event_name=event_name,
        title=title, body=body,
        target_contact_ids=target_contact_ids,
        target_group_ids=target_group_ids,
        category_id=category_id,
    )
    resp = httpx.post(url, headers=_auth_header(), json=payload, timeout=30.0)
    _log_and_raise_for_status(resp, "create_notification_template")
    return _extract_id_from_post_response(resp.json())


def delete_notification_template(*, org_id: str, template_id: str) -> None:
    """DELETE /notificationTemplates/{orgId}/{templateId} — audit-safe (Phase 0).

    Notification history is independent of the template — deleting the
    template does NOT remove the resulting notification from history.
    Phase 1 schedules this unconditionally at +30min from template creation.
    """
    url = f"{EVERBRIDGE_BASE_URL}/notificationTemplates/{org_id}/{template_id}"
    resp = httpx.delete(url, headers=_auth_header(), timeout=15.0)
    _log_and_raise_for_status(resp, "delete_notification_template")


def discover_notification_by_event(
    *, org_id: str, event_id: str
) -> Optional[str]:
    """GET /notifications/{orgId}?notificationEventId={eventId} — find safe-mode notification.

    Returns notification_id when the dispatcher has manually linked the event
    in the Everbridge UI at Send time, otherwise None.

    Phase 0 Key Discovery #8: param name MUST be `notificationEventId`. The
    lookalikes (`?eventId=`, `?event=`, `?search={...}`) are silently ignored.
    Pinned by regression test in test_main_regression.py.
    """
    url = f"{EVERBRIDGE_BASE_URL}/notifications/{org_id}"
    resp = httpx.get(
        url, headers=_auth_header(), timeout=15.0,
        params=_build_discovery_query_params(event_id),
    )
    _log_and_raise_for_status(resp, "discover_notification_by_event")
    return _parse_discovery_response(resp.json())


def poll_notification(*, org_id: str, notification_id: str) -> dict:
    """GET /notifications/{orgId}/{nid}?verbose=true — single poll cycle.

    Returns the dict shape from `_parse_poll_response`. Caller (the
    /poll-incident handler in main.py) is responsible for caching the last
    non-empty ack_contacts list across cycles to absorb the YES-count
    regression at natural expiry (Phase 0 Task 8 finding).
    """
    url = f"{EVERBRIDGE_BASE_URL}/notifications/{org_id}/{notification_id}"
    resp = httpx.get(
        url, headers=_auth_header(), timeout=15.0,
        params={"verbose": "true"},
    )
    _log_and_raise_for_status(resp, "poll_notification")
    return _parse_poll_response(resp.json())


def fetch_notification_raw(*, org_id: str, notification_id: str) -> dict:
    """GET /notifications/{orgId}/{nid}?verbose=true — RAW response body.

    Same request as `poll_notification`, but returns the unparsed Everbridge
    envelope instead of `_parse_poll_response`'s projection.

    ⚠️ **Do NOT "simplify" the #612 follow-up path to call `poll_notification`
    instead.** They look interchangeable and are not:

      poll_notification  -> {"is_terminal", "notif_status", "ack_contacts",
                             "decline_count", "no_response_count",
                             "all_details_empty"}
      this function      -> {"result": {"notificationResult": {"allDetails": [...]}}}

    `partition_contacts_for_followup` needs `allDetails[]`, which
    `_parse_poll_response` consumes and discards. Feeding it the parsed shape
    yields an EMPTY recipient set on every call — silently, with no exception —
    so the follow-up refuses to send with a misleading "everyone declined"
    message. Caught in review before it ever shipped; pinned by
    TestPollAndPartitionAreNotInterchangeable.

    `poll_notification` deliberately keeps its parsed contract: it is shared with
    the polling chain and /confirm-draft-sent, which depend on that shape.
    """
    url = f"{EVERBRIDGE_BASE_URL}/notifications/{org_id}/{notification_id}"
    resp = httpx.get(
        url, headers=_auth_header(), timeout=15.0,
        params={"verbose": "true"},
    )
    _log_and_raise_for_status(resp, "fetch_notification_raw")
    return resp.json()


def end_notification(*, org_id: str, notification_id: str) -> bool:
    """Stop an active notification via GET-modify-PUT.

    Halts all SMS/voice/email escalation immediately. Called by
    /close-incident-polling as part of the dual-stop semantic (see CLAUDE.md
    Locked Design Decision — Everbridge stop notification).

    Pattern (verified empirically 2026-04-28):
      1. GET /notifications/{orgId}/{nid}?verbose=false  — retrieve full body
      2. Modify body["notificationStatus"] = "Stopped"
      3. PUT /notifications/{orgId}/{nid} with the modified body

    EB requires the full notification object echoed back; a small patch body
    is rejected with HTTP 400 (BroadcastMessageWrapper deserialization error).
    `verbose=false` keeps the round-trip body small (~6KB) — verbose=true adds
    per-contact result detail that's not needed for the stop.

    Returns:
        True  — notification stopped successfully OR was already stopped
                (idempotent: 400 "is Stopped...can not be stopped" is treated
                as success, since the operational goal "no more escalation"
                is already satisfied)
        False — 404 on GET or PUT, OR non-JSON body on the GET 200 response
                (notification gone / behind a gateway error page — the
                "no more escalation" goal is moot or already achieved)

    Raises on other 4xx/5xx and on network-level failures. Caller is expected
    to wrap in try/except for best-effort behavior — see /close-incident-polling
    in main.py.

    Cluster E parity (2026-05-26): GET-vs-PUT path handlers brought to
    symmetry — both legs now return False on 404 (the GET-only behavior),
    and the GET path now guards against a non-JSON 200 body the way the
    PUT path already guards its 400.
    """
    url = _build_end_notification_url(org_id, notification_id)

    # Step 1: GET the current notification body
    resp_get = httpx.get(
        url, headers=_auth_header(), timeout=15.0,
        params={"verbose": "false"},
    )
    if resp_get.status_code == 404:
        return False
    _log_and_raise_for_status(resp_get, "end_notification.get")
    # Cluster E (EB-M6): defensive JSON decode. A status-200 response from a
    # Cloud Run / EB gateway error path can be HTML — resp.json() then raises
    # JSONDecodeError and crashes the stop. Treat as "we tried, can't proceed"
    # → return False, matching the GET 404 semantic (idempotent best-effort).
    try:
        get_body_json = resp_get.json()
    except Exception as e:
        logger.warning(
            "Everbridge end_notification GET %s: non-JSON 200 body (likely "
            "gateway error page) — treating as 'gone' for idempotent stop "
            "(err=%s)",
            notification_id, type(e).__name__,
        )
        return False
    body = get_body_json.get("result", {}) if isinstance(get_body_json, dict) else {}
    if not isinstance(body, dict) or not body:
        raise RuntimeError(
            f"Everbridge GET /notifications/{notification_id} returned an "
            f"empty/non-dict result; cannot construct PUT body"
        )

    # Step 2: Modify the status
    body["notificationStatus"] = "Stopped"

    # Step 3: PUT the modified body back
    resp_put = httpx.put(
        url, headers=_auth_header(), json=body, timeout=30.0,
    )
    # Cluster E (EB-M5): symmetric 404 handling. Race between GET and PUT —
    # EB or another operator (UI Stop, natural expiry) deleted the notification
    # in the millisecond window. Same operational state as "GET 404" so
    # return False here too rather than raising and forcing the caller into
    # a generic eb_end_status='failed' bucket.
    if resp_put.status_code == 404:
        logger.info(
            "Everbridge end_notification PUT %s: 404 — notification gone "
            "between GET and PUT (idempotent success)",
            notification_id,
        )
        return False
    if resp_put.status_code == 400:
        try:
            msg = (resp_put.json().get("message") or "").lower()
        except Exception as e:
            # Non-JSON 400 (HTML error page, gateway response, malformed body).
            # Don't fail the parse — fall through with empty msg, which won't
            # match any already-stopped marker, so raise_for_status() below
            # will fire. Log so ops can correlate when investigating.
            logger.warning(
                "Failed to parse JSON from Everbridge 400 response for "
                "notification %s: %s",
                notification_id, e,
            )
            msg = ""
        if any(marker in msg for marker in _END_NOTIFICATION_ALREADY_STOPPED_MARKERS):
            logger.info(
                "Everbridge notification %s already stopped (400 swallowed)",
                notification_id,
            )
            return True
    _log_and_raise_for_status(resp_put, "end_notification.put")
    return True
