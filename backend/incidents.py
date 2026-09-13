"""incidents.py — Firestore `incidents/{event_id}` collection helpers.

DESIGN DECISION (do not revert without team discussion):
This is the SINGLE allowed PII store in the project. Documented exception
to the 'no PII at rest' posture (CLAUDE.md). TTL = 24h after polling
closes (~25h max from creation since polls cap at 4h). Slack channel,
Everbridge reports, and future D4H persist the historical roster —
Firestore is ephemeral working state only.

Phase 1 surface area (this file):
- Mode-dependent timing constants (poll interval, error tolerance,
  hard cap, idle threshold, TTL).
- new_incident_doc() — build the initial Firestore doc body. Either
  notification_id (live path) or template_id (safe-mode draft path) is
  set; the other is None. The /poll-incident handler in main.py reads
  these to decide between live polling vs. discovery polling.
- stop_incident() — mark a doc stopped, set expire_at so the Firestore
  TTL field reaps it 24h later.
- now_utc() — single source of UTC `datetime` so tests can monkey-patch
  it for deterministic timestamps.

Pure stdlib (datetime + typing) — no GCP/Firestore SDK imports here.
The actual Firestore reads/writes live in main.py /Task 1.10. This
module is the data-shape contract.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Mode-dependent constants (design Section 3, with Item 2 amendment)
# ---------------------------------------------------------------------------

# Item 2 — same poll cadence in both modes. Earlier drafts had 60s for
# safe and 15s for full; changed because polling drives the live Slack
# tally that dispatchers + search management watch during shadow mode.
# Slowing it would mask UI behavior we explicitly need to see.
SAFE_MODE_POLL_INTERVAL_S = 15
FULL_MODE_POLL_INTERVAL_S = 15

# Error tolerance: how long the polling chain may keep failing before we
# give up and mark the incident stopped_error. Safe mode is more lenient
# because the dispatcher is more engaged (Everbridge UI is open) and
# transient discovery failures are common. Full mode is tighter because
# the dispatcher has handed off and we should fail visibly faster.
SAFE_MODE_ERROR_TOLERANCE_S = 30 * 60     # 30 min
FULL_MODE_ERROR_TOLERANCE_S = 10 * 60     # 10 min

# Hard cap on total polling time — stops the Cloud Tasks chain even if
# Everbridge somehow never marks the notification terminal. 4h matches
# the longest realistic SCCSSAR callout duration.
HARD_CAP_S = 4 * 60 * 60                  # 4 hours

# Idle stop: if no new YES responder arrives for this long, close the
# incident. Prevents an open-ended chain when an incident effectively
# concludes but Everbridge hasn't expired the notification yet.
# 60 min matches the team's stand-down policy — keep polling alive long
# enough for a late wave of late-arriving responders before auto-closing.
IDLE_S = 60 * 60                          # 60 min

# Firestore TTL window: doc is auto-deleted this long after stop. The
# Terraform `google_firestore_field.incidents_ttl` resource binds to
# the `expire_at` field; this constant is the single source for the
# value written into that field at stop time.
TTL_AFTER_CLOSE_S = 24 * 60 * 60          # 24 hours

# Manual-confirm banner trigger threshold. The dispatch console polls
# /incident-status every 30s after a safe-mode draft is created; the
# response includes `manual_confirm_offered: true` once auto-discovery
# has been hunting for ≥ this many seconds without finding the
# notification. Empirical Phase 0 Task 9c showed typical discovery
# latency from Send is < 5s, so 2 min is well past "fast happy path"
# while leaving the 30-min hard timeout plenty of headroom.
MANUAL_CONFIRM_OFFER_DELAY_S = 2 * 60     # 2 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    """Single source of UTC `datetime` so tests can monkey-patch this."""
    return datetime.now(timezone.utc)


def new_skeleton_incident_doc(
    *,
    event_id: str,
    event_name: str,                   # canonical Everbridge name with HHMM suffix
    event_name_human: str,             # HHMM-stripped display name
    dispatcher_email: str,
    created_at: datetime,
) -> dict[str, Any]:
    """Minimal placeholder doc for the atomic double-dispatch guard.

    /send-notification atomically `.create()`s this skeleton at the top of
    the handler, keyed by `event_id`. A double-click race (two requests
    for the same dispatcher / same HHMM / same street) produces the same
    `event_id`; the second `.create()` raises `AlreadyExists` and the
    handler returns 409 BEFORE any EB / Slack / D4H side effect fires.

    Rate-limiting (5/min per dispatcher) does NOT prevent the race — two
    clicks within ~1 second both pass the limiter (it catches the 6th,
    not the 2nd). `.create()` is the actual fix.

    On the success path, the full `new_incident_doc()` payload + D4H
    fields OVERWRITE this skeleton via the existing `.set()` at the
    bottom of the handler. On a mid-handler failure, the skeleton
    remains as a tombstone identifiable by `status == "creating"`.

    Fields are the minimum needed for two endpoints to gracefully handle
    a stuck skeleton:
      - `_validate_close_polling`: ownership check via `dispatcher_email`
      - `GET /dispatch-status`: returns the 7 status fields via `.get()`
        defaults; `status == "creating"` identifies the tombstone.
    """
    return {
        "event_id":         event_id,
        "event_name":       event_name,
        "event_name_human": event_name_human,
        "dispatcher_email": dispatcher_email,
        "status":           "creating",
        "created_at":       created_at,
    }


def new_incident_doc(
    *,
    event_id: str,
    event_name: str,                   # canonical Everbridge name with HHMM suffix
    event_name_human: str,             # HHMM-stripped display name (Slack/CalTopo/D4H)
    dispatcher_email: str,
    everbridge_event_id: str,          # POST /notificationEvents result
    notification_id: Optional[str],    # populated on send_live; None on send_draft
    template_id: Optional[str],        # populated on send_draft; None on send_live
    slack_channel_id: str,
    slack_channel_name: str,           # bare or collision-suffixed (whichever was used)
    active_incidents_ts: str,
    welcome_ts: str,                   # ts of pinned welcome message; "" if post failed
    staging_ts: str,                   # ts of pinned staging message (#673); "" if post failed
    caltopo_ts: str,                   # ts of pinned CalTopo follow-up; "" if no URL/post failed
    everbridge_mode: str,
    slack_mode: str,
    action: str,                       # 'send_live' | 'send_draft'
    selected_target_ids: list[str],
    requested_group_names: list[str],
    contact_group_map: Optional[dict],    # contact_id → [group_names]; {} for direct-only sends
    contact_email_map: Optional[dict],    # contact_id → [emails]; Slack invite fallback
    slack_dm_sent_user_ids: Optional[list[str]] = None,  # VIP-breakthrough DM recipients (PR #566/#568 feature)
    followup_notification_ids: Optional[list[str]] = None,  # #612 Standard follow-ups sent under this event
    off_call_excluded_names: Optional[list[str]] = None,  # D4H off-call members NOT paged (tally line)
) -> dict[str, Any]:
    """Build the initial Firestore doc body for a new incident.

    Live path (action='send_live'):     notification_id set, template_id=None
    Safe draft path (action='send_draft'): template_id set, notification_id=None

    The /poll-incident handler in main.py:
    - if notification_id  → poll directly (live path or post-discovery in safe path)
    - if template_id and not notification_id → run the Task-9 safe-mode discovery
      polling against the event_id until a notification_id appears or 30 min elapse.

    `last_non_empty_responders` is the Phase 0 Task 8 finding:
    `notificationResult.allDetails[]` briefly empties at natural expiry before
    `notificationStatus` flips to Completed. The poll handler caches the last
    non-empty list here; on terminal stop it uses the cache as the final roster.
    """
    return {
        "event_id":                  event_id,
        "event_name":                event_name,
        "event_name_human":          event_name_human,
        "dispatcher_email":          dispatcher_email,
        "everbridge_event_id":       everbridge_event_id,
        "notification_id":           notification_id,
        "template_id":               template_id,
        "slack_channel_id":          slack_channel_id,
        "slack_channel_name":        slack_channel_name,
        "active_incidents_ts":       active_incidents_ts,
        # Slack post timestamps for the per-incident channel — captured at
        # post time so a future retry-dedup pass (and admin audit) can
        # identify already-posted welcome/staging/CalTopo messages by ts. The
        # /send-notification handler ALSO writes these via incremental
        # .update() the moment each post returns, so they survive a later
        # handler failure that prevents this final .set() from running.
        #
        # staging_ts (#673) is a REQUIRED param, not an optional one, for the
        # #570 symmetry reason: it is incrementally patched at Step 8, so
        # omitting it here would let this .set() wipe it between send-time and
        # poll-time. It is also the handle an admin needs to find and delete
        # the staging message when a dispatch went out with the wrong location
        # — which is the entire purpose of splitting it out of the welcome.
        "welcome_ts":                welcome_ts,
        "staging_ts":                staging_ts,
        "caltopo_ts":                caltopo_ts,
        # VIP-breakthrough DM recipients — accumulated in Python at send-time
        # Step 7b so this final .set() DOESN'T wipe the ArrayUnion-patched
        # value (which is what the pre-fix behavior did, causing poll-time
        # to fire a duplicate DM to safe-list responders after they YES'd).
        # Same "pass through to survive the .set()" pattern as welcome_ts /
        # caltopo_ts. Poll-time cycles keep using ArrayUnion for their own
        # per-YES additions — that path has no overwriting .set() to work
        # around. Bug + fix: PR-after-#568.
        "slack_dm_sent_user_ids":    list(slack_dm_sent_user_ids or []),
        # D4H off-call members who were NOT paged, rendered as the 🚫 line of
        # the #active-incidents tally on every poll-cycle re-render. Only
        # populated when the exclusion was actually applied (plan mode
        # "excluded"); page_all / draft / all_off_call modes leave it empty because those
        # people WERE paged and the tally must not say otherwise — the Event
        # Log carries the nuance.
        "off_call_excluded_names":   list(off_call_excluded_names or []),
        # #612 Gap A — every Standard follow-up notification sent under this
        # event, appended via ArrayUnion by /send-followup-notification the
        # moment each id is obtained (Cluster C).
        #
        # Present here to satisfy the #570 symmetry requirement: an
        # incrementally-patched field that is NOT a param of this builder gets
        # wiped by the final .set() overwrite. /send-notification does not pass
        # a value because no follow-up can exist yet at that point — a follow-up
        # requires a notification_id the dispatch is still in the middle of
        # obtaining, and the UI does not offer the action until dispatch
        # completes. The theoretical race (a follow-up landing between the
        # notification_id patch and the final .set(), seconds apart, requiring a
        # human click) is accepted as negligible rather than papered over with a
        # speculative accumulator.
        "followup_notification_ids": list(followup_notification_ids or []),
        # Slack incident-channel provisioning outcome. "ok" on the happy path;
        # "failed" when Step 6 channel creation raised (EB had already fired,
        # so dispatch continues without a channel). /send-notification
        # overwrites both before the final .set(), mirroring the d4h_status
        # pattern below, so the persisted value reflects the real outcome.
        "slack_status":              "ok",
        "slack_error":               None,
        "everbridge_mode":           everbridge_mode,
        "slack_mode":                slack_mode,
        "action":                    action,
        "selected_target_ids":       list(selected_target_ids),
        "requested_group_names":     list(requested_group_names),
        "contact_group_map":         dict(contact_group_map or {}),
        "contact_email_map":         dict(contact_email_map or {}),
        "responders":                [],
        "last_non_empty_responders": [],
        "status":                    "polling",
        "stop_reason":               None,
        "created_at":                now_utc(),
        "last_poll_at":              None,
        "last_responder_at":         None,
        "first_error_at":            None,
        "expire_at":                 None,         # TTL field — set on stop_incident()
        # D4H Phase 2 — dispatch-time create + Selective-mode per-YES sync.
        # /send-notification overwrites d4h_status / d4h_activity_id / d4h_error
        # before persisting based on the actual D4H create outcome. Under
        # Selective mode (fullTeam: false) the per-YES Cloud Tasks worker
        # (/d4h-sync-yes) records ATTENDING via POST-new and writes only to
        # Cloud Run logs (NOT d4h_event_log) per the milestone-only contract.
        "d4h_status":                "pending",
        "d4h_activity_id":           None,
        "d4h_error":                 None,
        "d4h_event_log":             [],
    }


# How long a #612 follow-up tombstone may sit in "sending" before it is treated
# as a crashed attempt rather than an in-flight one. The real operation is a
# single Everbridge POST (~2s), so this is generous headroom. Being too lenient
# costs at worst a duplicate correction; being too strict permanently locks a
# dispatcher out of resending a correction that never actually went.
FOLLOWUP_STALL_SECONDS = 300


def followup_tombstone_is_dead(
    doc: Optional[dict],
    *,
    now: datetime,
    stall_seconds: int = FOLLOWUP_STALL_SECONDS,
) -> bool:
    """Is an existing follow-up tombstone a crashed/failed attempt (retryable)?

    Lives here rather than in main.py so it is executable under pytest — main.py
    cannot be imported locally (fastapi/httpx/google.cloud are container-only),
    and a source-reading pin cannot verify branching logic. A first draft of this
    logic sat in main.py and its mutation test came back VACUOUS.

    CONSERVATIVE BY DESIGN. Anything ambiguous returns False, which preserves the
    409 and with it the Cluster B double-click guarantee. Only a positively
    identified dead attempt unlocks a retry.

      "failed"                      -> True.  Everbridge rejected the send. The
                                      natural dispatcher response is to press
                                      send again with identical wording, which
                                      would otherwise 409 forever.
      "sending", older than stall   -> True.  Cloud Run was preempted between the
                                      .create() and the send. Nothing fired and
                                      nothing ever will, but the tombstone blocks
                                      the retry.
      "sending", younger than stall -> False. A live double-click — seconds old.
                                      This is the case Cluster B exists for.
      "sent"                        -> False. An identical correction already
                                      went out. The 409 is the correct answer and
                                      IS the duplicate-send protection.
      missing doc / bad timestamp   -> False.
    """
    if not doc:
        return False
    status = doc.get("status")
    if status == "failed":
        return True
    if status != "sending":
        return False
    started = doc.get("retried_at") or doc.get("created_at")
    if not isinstance(started, datetime):
        return False
    try:
        age = (now - started).total_seconds()
    except TypeError:
        # Naive/aware mismatch — unreadable, so treat as live.
        return False
    return age > stall_seconds


def stop_incident(doc: dict, reason: str) -> dict:
    """Mark an incident stopped in-place + set the Firestore TTL field.

    `expire_at` is stored as a UTC `datetime` so the Firestore SDK
    serializes it as a TIMESTAMP type. Firestore's TTL service only
    processes fields of type TIMESTAMP — numeric fields (float/int) are
    silently ignored, so storing a Unix epoch float would cause docs to
    accumulate indefinitely.

    Mutates and returns the input doc for caller convenience (the
    /poll-incident handler in main.py wraps this in a Firestore
    transaction for atomicity).
    """
    now = now_utc()
    doc["status"] = f"stopped_{reason}"
    doc["stop_reason"] = reason
    doc["expire_at"] = now + timedelta(seconds=TTL_AFTER_CLOSE_S)
    return doc
