"""
gdocs.py — Google Docs + Drive API integration for SCCSSAR Dispatch Turbo.

Creates a Google Doc pre-populated with incident data using the dispatcher's
own Google OAuth access token (drive.file scope), then shares it as 'writer'
(silent, no email notification) with all other authorized dispatchers.

DESIGN DECISION (do not revert without team discussion):
  Dispatcher OAuth token (not service account) creates and owns the document.
  The dispatcher is already authenticated via Google Sign-In; the frontend
  requests drive.file scope via google.accounts.oauth2.initTokenClient() and
  forwards the short-lived access token to the backend with each /create-doc
  request. No service account key or domain-wide delegation required.

  Security properties:
  - access tokens are short-lived (~1 hour) — exfiltration window is minimal
  - drive.file scope restricts access to only files created by this app
  - no persistent credentials stored server-side for Drive access
  - blast radius of a token leak: one dispatcher's Drive, files this app made
  See issue #197.

DESIGN DECISION (do not revert without team discussion):
  drive.file scope is used instead of drive — principle of least privilege.
  drive.file restricts the token to only files created or opened by the app;
  it cannot enumerate or read any other files in the dispatcher's Drive.

DESIGN DECISION (do not revert without team discussion):
  googleapiclient is NOT thread-safe. Do NOT use ThreadPoolExecutor inside
  create_incident_doc — sharing Credentials or service objects across threads
  causes SIGABRT (signal 6) crash in Cloud Run. Confirmed in PR #213: parallel
  discovery.build() calls with shared creds → crash → 503 on next request.
  All API calls (build, batchUpdate, permissions.create) MUST be serial.
"""

import logging
from datetime import datetime, timezone

import googleapiclient.discovery
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)


def create_incident_doc(
    event_name: str,
    content: str,
    dispatcher_name: str,
    authorized_emails: list,
    access_token: str,
) -> str:
    """
    Create a Google Doc titled '<event_name> — Working Notes', insert a
    timestamped header followed by the textarea content verbatim, share it
    as 'writer' with each address in authorized_emails (no email notification),
    and return the shareable edit URL.

    The doc is created in the dispatcher's own Google Drive using their OAuth
    access token.  authorized_emails must have the dispatcher's own email
    filtered out before calling — they already own the doc and adding them
    again would cause a 400 error.

    Raises ValueError if access_token is empty.
    Raises googleapiclient.errors.HttpError on API failure (bubbles up to caller).
    """
    if not access_token:
        raise ValueError("Drive access token not provided")

    # Build credentials from the dispatcher's OAuth access token.
    # cache_discovery=False is required: Cloud Run filesystem is read-only;
    # the discovery cache write would fail or write to /tmp unexpectedly.
    #
    # IMPORTANT: googleapiclient is NOT thread-safe — do NOT use
    # ThreadPoolExecutor here. Parallel build() or permissions.create() calls
    # with shared Credentials/service objects cause SIGABRT (signal 6) and
    # crash the Cloud Run instance. All calls must be serial. (PR #213)
    creds = Credentials(token=access_token)
    docs_svc = googleapiclient.discovery.build(
        "docs", "v1", credentials=creds, cache_discovery=False,
    )
    drive_svc = googleapiclient.discovery.build(
        "drive", "v3", credentials=creds, cache_discovery=False,
    )

    # Step 1: Create the document via Drive API.
    # IMPORTANT: docs.documents.create() requires 'drive' scope and returns 403
    # when called with 'drive.file' scope — even though 'drive.file' is listed
    # as supported in the Docs API docs. The Drive API files.create() with
    # mimeType=application/vnd.google-apps.document works correctly with
    # 'drive.file' scope and creates a proper Google Doc. Subsequent Docs API
    # batchUpdate calls on the created file also work with 'drive.file'.
    title = f"{event_name} \u2014 Working Notes"
    file_meta = drive_svc.files().create(
        body={"name": title, "mimeType": "application/vnd.google-apps.document"},
    ).execute()
    doc_id = file_meta["id"]
    logger.info("gdocs: created doc via Drive API | id=%s", doc_id)

    # Step 2: Insert header + textarea content via batchUpdate.
    # Google Docs API starts with an empty body at index 1.
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    by_line = f" for {dispatcher_name}" if dispatcher_name else ""
    header = (
        f"{title}\n"
        f"Created by SCCSSAR Dispatch Turbo{by_line} \u2014 {now_str}\n"
        f"---\n\n"
    )
    body_text = header + content

    docs_svc.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {
                    "insertText": {
                        "location": {"index": 1},
                        "text": body_text,
                    }
                }
            ]
        },
    ).execute()

    # Step 3: Share with each dispatcher as 'writer', silently (no email sent).
    # The dispatcher who created the doc already owns it (their token was used)
    # and must be excluded from authorized_emails before calling this function.
    # Failure to share with one email is logged but does not abort the overall
    # operation — the doc is still useful and the URL still valid.
    #
    # Serial loop required — googleapiclient is NOT thread-safe. Do NOT convert
    # this to a ThreadPoolExecutor. (PR #213)
    valid_emails = [e.strip() for e in authorized_emails if e.strip()]
    shared_count = 0
    for email in valid_emails:
        try:
            drive_svc.permissions().create(
                fileId=doc_id,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=False,
            ).execute()
            shared_count += 1
        except Exception as exc:
            # Log without the email address to avoid PII in Cloud Run logs.
            logger.warning(
                "gdocs: failed to share doc %s with a dispatcher — %s", doc_id, exc
            )

    logger.info(
        "gdocs: doc shared | id=%s shared_with=%d/%d",
        doc_id,
        shared_count,
        len(valid_emails),
    )

    return f"https://docs.google.com/document/d/{doc_id}/edit"
