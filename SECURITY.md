# Security Policy

## Reporting a Vulnerability

Report security vulnerabilities by email to **bill.burns@sccssar.org**.

Please do not open a public GitHub issue for a security vulnerability.

**What to include.** A report is easiest to act on when it contains:

- What the issue is, and which component or endpoint it affects.
- The steps to reproduce it.
- What an attacker gains — read access, write access, denial of service, information
  disclosure.
- The version or commit you tested against. The footer of a running instance reports both.

**What to expect.** A report is acknowledged within **72 hours**. You will get an
assessment of severity and an expected fix timeline once the issue is confirmed. Fixes ship
on a schedule proportionate to severity.

**Good-faith research.** Testing against your own deployment is welcome. Please do not test
against another team's running instance, and please do not access, modify or retain data
belonging to a real missing person or a real responder. Report what you find and give a
reasonable opportunity to fix it before disclosing publicly.

There is no bug bounty. This is a volunteer search-and-rescue project.

**Especially wanted.** Reports touching these are the most valuable, because they map to
the guarantees this project actually makes:

- Any path that writes personal information from a form into logs, disk, or a database.
- Any way to reach an endpoint without an allowlisted identity.
- Any way to make the app send a notification to recipients the dispatcher did not choose.

---

## No Published Advisories

There are currently no published security advisories for this repository.

---

## Security Model

This application processes sensitive personal information about missing persons —
names, dates of birth, home addresses, physical descriptions, and medical history —
extracted from SAR call-out forms. The security design reflects that sensitivity.

### Threat model

Stating the trust boundaries explicitly so the protections below — and the conclusions
of any security assessment — can be evaluated against a clear baseline.

**In scope** (the system actively defends against these):
- Unauthenticated internet attackers reaching any public endpoint.
- Authenticated Google users not on the dispatcher allowlist.
- Malicious or malformed content embedded in an uploaded form image or PDF.
- Malicious content returned by a third-party API (e.g., an Overpass response
  attempting to break query construction, a Nominatim record with control characters).
- Compromise of a single short-lived dispatcher OAuth access token (`drive.file`-scoped).
- Known vulnerabilities in third-party Python packages or transitive dependencies —
  Dependabot opens PRs on disclosed CVEs, and Aikido SCA continuously scans the
  installed dependency tree.
- Tainted third-party packages or anomalous CI/CD network activity reaching the build
  machine — covered by Aikido endpoint protection on the build host.

**Out of scope** (the system does not attempt to defend against these):
- A malicious insider — a dispatcher already on the allowlist who deliberately abuses
  their access. The allowlist + email-verified GSI gate is the trust decision.
- Compromise of a maintainer's GCP account credentials (covered by GCP-side controls
  outside this app: 2FA, conditional access, audit logs).
- Physical access to a dispatcher's signed-in workstation.
- A compromised Google Sign-In or Google Cloud platform itself.
- Compromise of an authorized third-party service account (Slack workspace admin,
  Everbridge org admin, CalTopo team admin) — these are out-of-band trust decisions.

### What is NOT logged (the core privacy guarantee)

- **Form images are never stored.** The raw JPEG/PDF bytes are passed in-memory to Vertex AI
  and garbage-collected after the API response. No image is written to disk, GCS, Firestore,
  or any log.
- **No PII appears in Cloud Run logs.** Cloud Run logs contain only operational data: request
  latency, Gemini finish reason, error type. Names, dates of birth, addresses, and case
  numbers are never logged — this is the core privacy guarantee.

**How the guarantee is enforced, and where it once broke.** Two controls back it up.
`backend/test_pii_log_patterns.py` walks every backend module's AST and fails the build if
a `logger.*` call interpolates PII; its baseline is monotone non-increasing, so a cleanup
can lower it but nothing may raise it. That guard covers only *our* logging calls, and in
September 2026 a review found the guarantee broken by a logger we do not own: `httpx` logs
every outbound request URL at INFO, which put the subject's residence and last-known
position into the query strings of Nominatim and Google Maps calls, and the LKP coordinates
into Geoapify calls — on every `/ocr`. It was measured at 42 / 7 / 12 log lines over seven
days on the team environment before the fix. The control is now a query-string redactor
applied at the single root log handler, which replaces the *values* of the `q`, `address`,
`filter` and `bias` parameters while leaving host and path intact; it is pinned against the
production pattern by `backend/test_log_redaction.py`. Adding a new geocoding parameter
means adding it there. One bypass is known and stated rather than implied: uvicorn's own
loggers do not propagate to that handler, so an unhandled ASGI traceback would skip the
redactor. Nothing currently routes subject text into one.

**Note on authorized third-party services:** Incident data is transmitted to authorized,
SCCSSAR-reviewed service providers as part of the dispatch workflow. These are deliberate
data flows to approved services, not accidental retention. The table below is the
complete inventory of outbound data flows; any addition requires updating this table.

| Service | Data sent | Auth | Persistence |
|---|---|---|---|
| **Vertex AI (Google)** | Form image + extracted text for OCR/reasoning | Service-account ADC | Transient; not stored by Google per API terms |
| **Nominatim (OSM)** | LKP street address (no PII attached) | Anonymous | Logged by OSM operators per public terms; no account |
| **Geoapify Places API** | The LKP coordinate, a search radius, and a list of POI categories. No name, date of birth or other identifier is attached — but the coordinate is the subject's last known position, so it is location data about an active search | API key (`geoapify-api-key`) | Per Geoapify's terms. Unlike the anonymous OSM services below, requests are attributable to the team's Geoapify account |
| **Overpass API (OSM)** | Same shape as Geoapify — lat/lng and a bounding box, no identifiers attached. **Fallback only**: called when the Geoapify lookup fails (`STAGING_SOURCE=geoapify`, `STAGING_SHADOW=off` in the team environment) | Anonymous | Logged by mirror operators per public terms; no account |
| **Google Maps Geocoding API** | Misspelled street as fallback only (no PII attached) | API key (Geocoding API only) | Per Google API terms |
| **CalTopo** | LKP, residence, staging coordinates as map markers | HMAC-SHA256 signed (per-request, no Bearer) | Persists in the team's CalTopo account |
| **Google Docs / Drive** | Working-notes doc body (event name + extracted summary) | Dispatcher's own OAuth access token, `drive.file` scope | Persists in dispatcher's Drive |
| **Everbridge** | Notification title/body (no PII), selected group/contact IDs, polling reads of responder roster | Basic auth via `everbridge-credentials` (base64 user:pass) | Notification + event records persist in the SCCSSAR Everbridge org |
| **Slack** | Channel name (event name), welcome/tally messages, invited responder Slack user IDs | Bot token (`xoxb-…`, `slack-bot-token`) | Channel + message history persists per Slack workspace retention |
| **D4H** | Incident record (event name, agency event #), involved-person details for the subject, and one attendance record per confirmed-YES responder | Personal Access Token (`d4h-access-token`) | Persists in the SCCSSAR D4H team |

**Internal stores under SCCSSAR control:**
- **Firestore `rate_limits`** — per-user and global request counters; no PII.
- **Firestore `incidents`** — operational state for an active incident **plus
  responder PII (name, Everbridge contact ID, Slack user ID once invited)**. This is
  the single allowed PII store for the Everbridge + Slack integration. Retention is
  bounded by a Firestore TTL on the `expire_at` field: documents auto-delete **24
  hours after polling closes** (~25h typical from incident creation, hard-capped by
  the 4h polling cap). The 24h window serves three operational needs:
  (1) Cloud Tasks polling chain (EB poll + Slack tally updates), (2) D4H per-YES
  sync retry budget (`max_attempts=5` per the CLAUDE.md Locked Decision — covers
  transient D4H 5xx without burying a real outage under months of retries), and
  (3) manual replay window if D4H is unavailable mid-incident. Historical
  record-of-incident lives in Everbridge reports, the Slack channel history, and
  D4H (Phase 2 live) — Firestore is strictly ephemeral working state by design.

The "not logged" guarantee refers specifically to Cloud Run logs. It has never been accurate to say incident data is "not stored anywhere" — the app deliberately routes it to the above services by design, and during an active incident a small operational copy lives in Firestore under the TTL above.

### Authentication

There are two distinct auth paths into the backend, and every endpoint enforces one or
the other server-side:

**Path 1 — dispatcher requests (Google Sign-In):**
- Every dispatcher-facing endpoint requires a valid **Google ID token** issued by
  Google Sign-In (GSI).
- The backend verifies the token server-side on every request using the Google Auth
  Library — it is not trusted on the client side alone.
- Verified tokens are checked against an **email allowlist** stored in GCP Secret
  Manager (`dispatch-authorized-emails`). Accounts not on the list receive HTTP 403
  even with a valid Google token.
- `email_verified: true` is enforced on every token.
- The OAuth consent screen is set to **External / Testing mode** — only explicitly
  listed test users can complete the OAuth flow. Moving to Production publishing mode
  requires a deliberate team decision.

**Path 2 — Cloud Tasks → Cloud Run (OIDC):**
- The Everbridge polling chain (`/poll-incident/{event_id}` and `/delete-template/{template_id}`)
  is invoked only by Cloud Tasks, not by browsers. These endpoints reject Google Sign-In
  tokens.
- Each task carries an OIDC token minted for a dedicated service account
  (`everbridge-poll-sa`). The endpoint pins both the **audience** (the Cloud Run service
  URL, env var `CLOUD_RUN_SERVICE_URL`) and the **token email** (the polling SA's email)
  before accepting the request.
- This dual-pin means the polling endpoints cannot be invoked by any other Cloud Run
  service, by a browser, or by a different SA — even if the SA token were leaked.

**Two dispatcher endpoints are deliberately not ownership-gated:**
- `GET /dispatch-status/{event_id}` and `POST /send-followup-notification/{event_id}` are
  gated on the allowlist but **any** authorized dispatcher may call them for **any**
  incident. This is a deliberate operational choice, not an oversight. A follow-up is the
  time-critical correction to an already-sent notification; on 2026-07-24 the dispatcher
  who needed to send one was driving to collect drone gear, and an ownership gate would
  have blocked precisely the backup dispatcher who was free to help. Everyone reaching
  these handlers is already on the allowlist and already trusted to originate a dispatch,
  and the acting dispatcher is logged. Contrast `GET /incident-status/{event_id}`, which
  **is** ownership-gated and returns 404 on not-owned as defence-in-depth against
  ownership-existence inference. Reviewed and confirmed 2026-09-06; do not "harden" the
  first two to match the third without re-reading this rationale.
- Recipients of a follow-up are **derived, never dispatcher-selected**: everyone who
  replied affirmatively plus everyone who has not yet replied, excluding decliners. There
  is no code path by which a caller chooses who receives one.

**Slack and Everbridge admin onboarding (out-of-band trust):**
- The Slack bot must be installed by a workspace admin and granted the documented scopes
  (channel create/invite/post/pin); the workspace admin retains the ability to revoke
  the bot at any time.
- The Everbridge SHO-SAR Dispatcher service account sees only groups explicitly granted
  by the EB org admin — group visibility is **not automatic** on new EB groups, and
  must be re-verified after each EB-side group change. Verify visibility before any live test.

### Credentials and Secrets

All API credentials are stored in **GCP Secret Manager** and injected into Cloud Run as
environment variables at deploy time. They are never committed to source code.

| Secret | Contents |
|--------|----------|
| `dispatch-authorized-emails` | Comma-separated email allowlist for dispatcher access |
| `dispatch-google-client-id` | Google OAuth client ID for Sign-In |
| `caltopo-team-id` | CalTopo Team ID (account scope for map creation) |
| `caltopo-credential-id` | CalTopo Team API Credential ID (HMAC-SHA256 signing) |
| `caltopo-credential-secret` | CalTopo Team API signing secret |
| `geoapify-api-key` | Geoapify Places API key — the primary staging POI lookup |
| `google-maps-api-key` | Google Maps Geocoding API key (optional fallback for misspelled streets) |
| `everbridge-credentials` | Everbridge service-account credentials, base64-encoded `username:password` (used directly as `Authorization: Basic <value>`) |
| `slack-bot-token` | Slack bot token (`xoxb-…`, no expiry); rotate via `bin/rotate-secret.sh` |
| `slack-so-coordinator-email` | Sheriff's Office SAR Coordinator address, added to every incident channel (a Slack guest, so excluded from user groups) |
| `d4h-access-token` | D4H Personal Access Token for incident creation and attendance sync |
| `dispatch-safe-list` | Email + Slack-handle allowlist used by `_route_send()` to gate live Everbridge sends and to partition Slack invitations during shadow mode (temporary scaffolding for the EB+Slack rollout) |

**`terraform.tfvars` and `*.tfstate` files are gitignored** and must never be committed.
They may contain credential references or infrastructure state.

### Rate Limiting

The `/ocr` endpoint is rate-limited per dispatcher using a Firestore-backed counter, on
four tiers (defaults; each is overridable by environment variable):

| Tier | Default | Env var |
|---|---|---|
| Per minute, per user | 5 | `OCR_RATE_LIMIT_PER_MINUTE` |
| Per hour, per user | 20 | `OCR_RATE_LIMIT_PER_HOUR` |
| Per day, per user | 50 | `OCR_RATE_LIMIT_PER_DAY` |
| Per day, all users | 200 | `OCR_DAILY_GLOBAL_CAP` |

This prevents runaway usage (accidental loops, credential misuse) and constrains Vertex
AI costs. Requests over a limit receive HTTP 429. The limiter is a cost control, not the
anti-abuse layer — the allowlist is. Source of truth: `backend/rate_limit.py`; the values
are pinned against `docs/architecture.md` by `backend/test_doc_parity.py`.

### Transport Security

All traffic is HTTPS (Cloud Run enforces TLS). The backend sets the following headers
on every response:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin`
- `X-Permitted-Cross-Domain-Policies: none`
- Server header suppressed

### Image Validation

Uploaded files are validated before being sent to Vertex AI:
- Magic bytes checked for valid JPEG signature (`FFD8FF`)
- Maximum file size enforced
- Maximum image dimensions enforced

Malformed or non-JPEG uploads are rejected with HTTP 400 before any AI processing occurs.

---

## Risk Areas for Teams Adapting This Software

If you fork or adapt this project for your own SAR team, pay attention to the following:

### Email Allowlist

The `dispatch-authorized-emails` secret controls who can access the app. Keep this list
current: remove dispatchers who leave the team, and verify the list after any Secret
Manager update.

The GCP OAuth consent screen **Test Users** list must also match — an email removed from
the allowlist can still reach the OAuth consent screen if it remains in the Test Users
list (it will be blocked by the backend, but the user experience is confusing).

### CalTopo API Credentials

The `caltopo-credential-id` and `caltopo-secret` values authorize map creation in your
team's CalTopo account. Treat them as passwords:
- Rotate them immediately if you suspect exposure.
- Do not paste them into chat, email, or GitHub issues.
- Use `gcloud secrets versions add` to rotate; old versions can be disabled after the
  new deploy is confirmed.

The CalTopo API uses HMAC-SHA256 request signing — the secret is never sent over the
wire, only used to compute a signature. This limits the blast radius of exposure compared
to a Bearer Token.

### Failure Mode

When this software fails or is unavailable:
- No missing person search is impacted — the app is a dispatch speed tool, not a
  dependency for the search itself.
- Dispatchers revert to the manual method (the same copy-paste workflow the app replaces).
- No data is lost — form images and intake form data live only in the dispatcher's browser
  session, which persists across an app outage.

### Cloud Run Service Accounts

Two service accounts are deployed; neither holds broad GCP roles. Both are defined
in Terraform (`terraform/environments/<env>/main.tf`) and any addition is reviewable
in source.

**`dispatch-runner`** (the Cloud Run service identity for `dispatch-console`):
- `roles/aiplatform.user` — Vertex AI / Gemini calls
- `roles/datastore.user` — Firestore reads/writes (rate limits + incidents)
- `roles/secretmanager.secretAccessor` — read access to the secrets table above
- `roles/logging.logWriter` — Cloud Run application logs
- `roles/cloudtasks.enqueuer` — enqueue polling tasks for `/poll-incident`
- `roles/iam.serviceAccountTokenCreator` and `roles/iam.serviceAccountUser` on the
  `everbridge-poll-sa` only — required to mint the OIDC token attached to each task

**`everbridge-poll-sa`** (the OIDC identity Cloud Tasks attaches to polling-chain
HTTP requests):
- `roles/run.invoker` on the `dispatch-console` Cloud Run service only

Do not grant either service account broad GCP roles (Editor, Owner) or
project-wide secret access.

### What This App Does NOT Do

- It does not send data to any third party beyond the inventory in the
  "authorized third-party services" table at the top of this document.
- It does not write form images to disk, GCS, Firestore, or any log.
- It does not log names, dates of birth, addresses, or case numbers.
- The only persistent PII store under SCCSSAR control is the Firestore `incidents`
  collection, which is bounded by a 24h-after-close TTL (see "authorized third-party
  services" section above).

---

## Security Assessment Process

A static code security review is conducted after each significant change to the codebase.
Reviews are performed against all backend and frontend source files using a structured
methodology:

1. **Full source read** — all files in `backend/` and `frontend/` reviewed for data flow,
   trust boundaries, and security-relevant patterns.
2. **Vulnerability identification** — each file assessed across: input validation, auth and
   authorization, cryptographic implementation, injection and code execution, data exposure,
   and outbound request construction.
3. **False-positive verification** — each candidate finding independently re-analyzed against
   the actual code before being reported.

Reviews are **static analysis only** — no live endpoint testing, no network traffic to
production or third-party services during the review.

### Re-review triggers

A new assessment should be run when:

- Any new endpoint is added to `backend/main.py`
- The authentication or rate-limiting logic is modified
- A new third-party integration is added or an existing integration's auth model
  changes
- A new Firestore collection is introduced, or the retention policy / TTL on an
  existing collection changes

History of cleared triggers (PDF ingest, `/create-doc`, Everbridge + Slack, Vertex AI
SDK migration, and D4H live activation) is preserved in the Assessment record table below.

### Assessment record

| Date | Build | Notes |
|---|---|---|
| 2026-03-01 | Phase 1.5z / `6b8d1f0` | Pre-PDF-ingest baseline |
| 2026-03-04 | Phase 1.5z / `0b5c007` | PDF ingest surface review. 0 HIGH, 3 MEDIUM, 3 LOW. M1/L1/L2 fixed in PR #168. M2/M3 risk accepted. L3 deferred to prod launch. See `research/security-assessment-2026-03-04.md` |
| 2026-03-07 | Phase 1.5z / `1fd36de` | Post-Google-Maps-fallback review (PRs #168–#185). 0 HIGH, 2 MEDIUM (M2/M3 carried forward, risk accepted), 2 LOW (L3 carried forward; new L: Google Maps API key in URL params — standard for Google APIs, accepted), 3 INFO. No regressions. See `research/security-assessment-2026-03-07.md` |
| 2026-03-08 | Phase 1.5z / (PR #197 pre-ship) | Google Doc working notes feature threat model (conducted during planning). HIGH: `drive` scope too broad → mitigated by using `drive.file` scope in implementation. MEDIUM: no rate limiting on `/create-doc` → mitigated by applying `check_rate_limits()`. LOW: `cache_discovery=False` required → applied. INFO: privacy framing "not stored" was inaccurate — corrected to "not logged" in SECURITY.md and footer. 0 unresolved HIGH/MEDIUM findings at ship. |
| 2026-03-09 | Phase 1.5z / `20c9c1e` | Full post-ship assessment of `/create-doc` + dispatcher OAuth flow (PRs #197–#213). 0 HIGH, 1 MEDIUM (`HttpError` not caught → wrong error code + log gap), 2 LOW (doc_url in logs, no body size guard), 2 INFO (Safari popup recovery works; token-forwarding design limitation accepted). Deprecated service-account artifacts confirmed fully cleaned up. M1/L1/L2 fixed in PR #217. See `research/security-assessment-2026-03-09.md` |
| 2026-03-09 | Phase 1.5z / `e520d66` | M1/L1/L2 code fixes from 2026-03-09 assessment applied (PR #217). `GoogleApiHttpError` added to except clause; doc_id logged instead of full doc_url; 100 KB body size guard added to `/create-doc`. 0 open findings above INFO. |
| 2026-05-01 | Phase 1.8 Slacker / `cc1bf8b` | Full audit covering Everbridge REST + Slack API + Cloud Tasks polling endpoints (`/poll-incident`, `/delete-template`, `/close-incident-polling`) + three-tier email resolution (PRs #328/#329) + Vertex AI SDK migration (PR #333) + Aikido CIS log-metrics (PR #358). 0 HIGH / 0 MEDIUM / 0 LOW. Six candidate findings raised and all rejected after per-finding code-verification (max confidence 3/10). Three defense-in-depth recommendations noted (CSP header, OIDC env-var startup guard, Slack `WebClient` singleton) — not blocking. See `research/security-assessment-2026-05-01.md` |
| 2026-05-23 | Phase 1.8 Slacker / `4014697` | Closure of significant external security review (31 findings via external tracker, distinct from the 2026-05-01 internal static audit). All 31 findings disposed across 38 PRs merged 2026-05-19 → 2026-05-23. 0 open HIGH / MEDIUM / LOW at closure. Infra changes followed the staged personal-dev → sccssar-dev mirror pattern (Phase 1 / Phase 2 PR pairs for K/L/M/N/O/R). |
| 2026-05-26 | Phase 1.8 Slacker / `05b585b` | Closure of external code-review batch 2 (28 findings across `backend/slack.py`, `backend/d4h.py`, `backend/everbridge.py` + their call sites in `backend/main.py`; same reviewer as the 2026-05-23 batch-1 closure, different scope). All 28 findings disposed across 6 PRs (#515 / #516 / #517 / #518 / #523 / #524) + 1 CLAUDE.md follow-up (#525) promoting two new patterns into Locked Decisions. Breakdown: 4 Critical/High orphan-after-EB hardening + 1 atomic double-dispatch guard + 3 side-effect-before-persistence + 9 D4H robustness + 6 EB robustness + 4 Slack polish. 0 open HIGH / MEDIUM / LOW. 4 pre-existing platform issues surfaced during testing filed for separate tracking (#519 OOM, #520 silent-500, #521 refresh-wipes-state, #522 cold-start Firestore expiry); cumulative build is gated on #522 before sccssar-dev promotion. Tests: 1188 → 1216 pytest. See `research/melanie-batch-2-resolution.md`. |
| 2026-09-06 | 1.11.66 / `886934e` | Pre-publication scanning sweep ahead of making the repository public. Aikido full scan run against the migrated repository; secret-scanning and push protection enabled and confirmed (0 alerts on the code repository); a blob scan across the full commit history validated with a positive control before being trusted — the control itself contaminates a `--history` scan, so it is run against a throwaway clone. Dependabot open alerts taken **17 → 0** (11 of them high). No credential file (`terraform.tfvars`, service-account JSON, `.pem`) was ever committed in any of the ~1,360 commits; the one live key ever present in a test fixture had already been rotated and deleted. |
| 2026-09-07 | 1.11.67 `a7d1f3a` → 1.11.69 `3edbcad` | Four-dimension backend security review (input handling, authentication, data exposure, outbound request construction). **1 HIGH** — `httpx` logged outbound geocoding URLs at INFO, placing the subject's residence, last-known position and coordinates into Cloud Run logs on every `/ocr`; measured at 42 / 7 / 12 lines over seven days. Notable because every `logger.*` call in the repository was clean and an existing test actually *asserted* the address survived redaction, so neither the AST guard nor the test suite could see it. Fixed by redacting the PII query-string values at the single root log handler, pinned by `backend/test_log_redaction.py`, and confirmed closed on both environments (18/18 sampled lines clean each). **1 MEDIUM** — intake free text reached Slack unescaped and could inject `mrkdwn` markup into a pinned incident message; fixed with `_mrkdwn_escape()` on every interpolated field. One further candidate (follow-up endpoint auth model) was reviewed and deliberately left as-is. |

---

## Confirmed Security Implementation Practices

The following security practices have been implemented and will be maintained.

### Authentication & Authorization

- `require_authorized_dispatcher` FastAPI dependency applied to every protected endpoint —
  no endpoint is reachable without a valid, verified Google ID token on the allowlist.
- `email_verified: true` enforced on every token — unverified Google accounts are rejected
  even if the email address is on the allowlist.
- All authorization decisions made server-side. Client-side JWT decoding is display-only
  (showing the dispatcher's email in the UI); it is never used for access control.

### Data Handling

- Form images processed in-memory only — raw bytes are never written to disk, GCS,
  Firestore, or any log. See "What is NOT stored" above.
- Intake form data is session-scoped: it exists only in the HTTP response and the
  dispatcher's browser session.

### Input Handling

- All outbound HTTP requests use `httpx` with `params={}` dictionary syntax throughout —
  the library automatically percent-encodes all parameter values. No manual URL
  construction with user-supplied data.
- Coordinates from Nominatim JSON are cast to `float()` before use in Overpass QL queries —
  this is a structural sanitizer; non-numeric values raise `ValueError` and abort the
  request before the query is constructed.
- File uploads validated on JPEG magic bytes (`FFD8FF`), file size, and image dimensions
  before any AI processing.

### Cryptography

- CalTopo API requests signed with HMAC-SHA256 — the secret is never sent over the wire,
  only used to compute a per-request signature.
- All credentials in GCP Secret Manager; none in source code or baked into images.

### Response Security

- Security headers on all responses: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin`,
  `X-Permitted-Cross-Domain-Policies: none`. Server header suppressed.
- `Cache-Control: no-cache` on `index.html` responses — prevents browsers from serving
  stale JavaScript to dispatchers after a deploy.

### Dependency Management

- Third-party dependencies are scanned for known security vulnerabilities via
  **GitHub Dependabot** (weekly automated alerts) and **Aikido Security** (SCA, SAST,
  and infrastructure-as-code scanning). Alerts are reviewed and patched on a priority
  basis relative to severity. Every Dependabot PR is reviewed and merged by a human —
  none are auto-merged. The open-alert backlog was taken to **zero** in September 2026.
- **GitHub secret scanning and push protection are enabled on the repository.** Push
  protection blocks a commit containing a recognised credential pattern at push time,
  rather than reporting it after the fact. A dependency bump that cannot resolve is
  dry-run in a scratch virtualenv and judged on the installer's **exit code** before
  merge — a resolution failure otherwise surfaces only at `docker build`, after the
  pre-flight test suite has already passed.
- Aikido scans source code (SAST), third-party dependencies (SCA), Docker image contents,
  and Terraform/IaC configuration for misconfigurations on a **scheduled cadence of every
  3 days**. It is the primary source for infrastructure-level security findings
  (e.g. VPC firewall rules, audit logging gaps, overly permissive IAM).
- **Aikido scope:** scans target the team environment's GCP project and **both** SCCSSAR
  repositories — this one and the private operations repository that holds the issue
  tracker and withheld runbooks. The GitHub Apps are installed at the organization level
  with access set to *All*, but **scanning is activated per repository inside Aikido's own
  console** — an org-level install alone does not start scanning a new repo. Findings are
  mirrored into the sandbox environment manually as part of routine maintenance.
- **Build-host endpoint protection:** the build/deploy machine runs the Aikido endpoint
  agent, which monitors for anomalous CI/CD network traffic and tainted third-party
  packages reaching the build environment. This complements the SCA scan of the
  installed dependency tree.

### Aikido data flow

Aikido is a third-party SaaS — scan data leaves the maintainer's machine and the
connected GCP project, lands in Aikido cloud, and is processed by Aikido's
SAST/SCA/IaC pipelines. This section is explicit about what crosses the boundary,
what does not, and how findings are dispositioned. It complements the scope and
cadence bullets above.

**Data flowing OUT to Aikido:**

- **Full repository tree** — Aikido's GitHub App clones `SCCSSAR/SAR_dispatch_flow`
  on the every-3-days scheduled scan AND on every PR (a PR-time CheckRun named
  `Aikido Security: check code` runs for ~15–25 minutes per PR). Aikido sees every
  file the GitHub App's installation permissions allow: `backend/`, `frontend/`,
  `terraform/` (committed `main.tf` + `variables.tf` + `outputs.tf` + `terraform.tfvars.template`),
  `docs/`, `experiments/` (non-gitignored portions), and any branch with an open PR.
- **Dependency manifests + resolved trees** — `backend/requirements.txt`, the Dockerfile,
  and the installed package set inside the built image. Used for CVE matching.
- **GCP project metadata** — Aikido's GCP integration reads IAM policies, audit log
  configurations, firewall rules, Cloud Run service shape, Secret Manager secret IDs
  (NOT VERSIONS), and Logging-derived metric definitions.
- **PR diff context** — at PR-event time, the PR's diff and surrounding file
  context flow to Aikido. Findings come back as inline review-thread comments
  anchored to file+line locations, plus a single review with empty body and
  `state: COMMENTED`.
- **Build-host telemetry** — the endpoint agent on the maintainer's build/deploy
  machine reports CI/CD network anomalies and tainted-package warnings to Aikido
  cloud. This is local-machine → Aikido, separate from the repo and GCP integrations.

**Data NOT flowing to Aikido:**

- **`terraform.tfvars`** is gitignored, so secret payloads and per-environment
  configuration never reach the repo and therefore never reach Aikido. Only
  `terraform.tfvars.template` (placeholder values only, per PR-P) is committed.
- **Secret Manager VERSIONS** — Aikido sees secret IDs via the GCP integration
  but never the underlying secret values. Versions stay in Secret Manager and
  reach only the Cloud Run service runtime via `secret_key_ref` mounts.
- **OCR data / form images / extracted dispatch data** — in-memory in Cloud Run,
  never written anywhere Aikido scans. The "What is NOT stored" guarantee above
  is comprehensive.
- **Cloud Logging contents** — Aikido reads log METRIC DEFINITIONS (the CIS
  log-metrics from PR #358) but not the underlying log entries. PII patterns are
  blocked from logs by `backend/test_pii_log_patterns.py` regardless of who reads
  Logging.
- **D4H, Everbridge, Slack, CalTopo, Google Docs, Google Maps content** — third-party
  API request/response bodies are not logged and not retained anywhere Aikido has
  visibility into.

**Finding disposition pattern:**

Aikido findings are dispositioned in one of three ways. Every disposition
becomes a public artifact on the PR thread — either a fix commit or an
in-thread reply — so the audit trail is the PR itself.

1. **Actionable** — a real vulnerability or misconfiguration. Open a follow-up
   PR with a fix; cite the Aikido finding's `issue_hash` in the commit body for
   audit linkage. The 2026-05 security review's Clusters A through 5 followed
   this pattern.
2. **Already addressed elsewhere** — Aikido re-surfaces a finding that an
   existing control covers (e.g. CIS log-metrics, audit configs, the
   monotone-non-increasing PII baseline). Reply in-thread with
   `@AikidoSec ignore: <reason linking to the existing control>`. Aikido suppresses
   the finding on subsequent commits to that PR.
3. **AI-generated stylistic suggestion** (`AIK_AI_*` rule prefix) — recommendations
   like "split this giant file into modules" that don't correspond to a vulnerability.
   Project disposition is to dismiss with `@AikidoSec ignore: <reason citing prior
   PRs that established the same disposition>`. The repo's deliberate monolithic
   per-environment `terraform/.../main.tf` is the canonical example; the
   dispositions on PRs #412, #447, and #491 are the precedent chain.

The PR-time CheckRun returns `conclusion: SUCCESS` when findings are advisory —
a green check does NOT mean Aikido raised nothing; it means nothing Aikido raised
is gate-blocking. Always read the PR's review-thread comments before merging,
not just the check status.

**Operator obligations (revocation path):**

- The Aikido GitHub Apps (`aikido-security` and `aikido-pr-checks`) are installed
  at the **SCCSSAR organization** level with repository access set to *All*, not
  on this repository. There is no per-repository uninstall. Revoking them is an
  org-owner action in the organization's installed-apps settings, and it removes
  Aikido from every SCCSSAR repository at once. To withdraw only this repository,
  change the installation's repository access from *All* to *Only select
  repositories* and leave this one out.
- The Aikido GCP integration uses a service account with read-only roles on the
  project — revoking is `gcloud projects remove-iam-policy-binding` against the Aikido
  service account.
- The endpoint agent on the build host is uninstalled locally on the maintainer's
  machine via the Aikido agent's uninstall procedure; no remote action revokes it.
- All three should be done before publishing the repository, transferring ownership,
  or decommissioning the project's infrastructure.

### GCP Infrastructure Hardening

The following infrastructure-level controls are applied to every deployed environment
and are part of the standard setup procedure (see [docs/DEPLOYING.md](docs/DEPLOYING.md)).
SCCSSAR-dev is the lead environment for new hardening — once a control is validated
there, it is mirrored into personal-dev as part of routine maintenance, so the two
projects are kept in approximate parity.

**Audit Logging (Terraform-managed):**
- `cloudresourcemanager.googleapis.com` — DATA_WRITE and DATA_READ audit logs enabled.
  Records project-level ownership and resource manager changes.
- `iam.googleapis.com` — ADMIN_READ audit logs enabled. Records IAM policy reads and
  admin operations.
- Both are declared as `google_project_iam_audit_config` resources in `main.tf` and
  applied via `terraform apply` — drift is detectable and reproducible.

**VPC Firewall Rules (applied manually via gcloud post-Terraform):**
- `default-allow-rdp` (TCP 3389, `0.0.0.0/0`) — **deleted**. No Compute Engine VMs exist;
  open internet RDP access serves no purpose.
- `default-allow-ssh` (TCP 22, `0.0.0.0/0`) — **deleted**. Same rationale.
- `default-allow-icmp` — **retained** with firewall logging enabled.
- `default-allow-internal` — **retained** with firewall logging enabled.

These changes were identified via Aikido findings #261–#264 (April 2026) and are now
part of the standard new-environment setup in [docs/DEPLOYING.md](docs/DEPLOYING.md).

### Rate Limiting

- Firestore-backed rate limiter uses transactions to prevent race-condition bypasses.
  A dispatcher who submits concurrent requests cannot double-spend their quota window.
