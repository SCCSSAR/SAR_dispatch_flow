# SCCSSAR Dispatch Console — Architecture Reference

**Version 1.11.71** | September 2026 | [GitHub](https://github.com/SCCSSAR/SAR_dispatch_flow) | [SCCSSAR Dev](https://dispatch-console-1010784158087.us-central1.run.app) | [Personal Dev](https://dispatch-console-970461953836.us-central1.run.app)

---

## System Overview

The SCCSSAR (Santa Clara County Search and Rescue) Dispatch Console reduces the time it
takes a SAR dispatcher to notify the team and open incident records from 20–35 minutes of
manual copy-paste across four separate systems down to approximately 2–3 minutes.

A dispatcher uploads a photo of a handwritten call-out form **or a digitally-filled PDF** (v2 form). For JPEG photos, Gemini AI extracts all structured fields via OCR. For PDFs, AcroForm field values are read directly — no OCR, 100% deterministic questionnaire accuracy. The dispatcher reviews and corrects the extracted text, then uses two separate button clicks to (a) create a CalTopo incident map, and (b) create an Everbridge mass notification + a per-incident Slack channel + a D4H incident record (a single combined dispatch). Separate helper buttons open a Google Map centered on the LKP, create a pre-filled draft Google Doc for incident note-taking, or open D4H to view all incidents.

**Current status:** version **1.11.71** is the current build, deployed to SCCSSAR dev (`sar-dispatch-sccssar-dev`). Everbridge automated dispatch (`/send-notification`, `/poll-incident`, `/close-incident-polling` endpoints, `backend/everbridge.py`) is live with **full-mode** — unconditional live send, no draft confirm step. Slack incident channels (`backend/slack.py`) are created automatically with **full-mode** — real invites sent to all confirmed responders. **Dispatch Turbo VIP-breakthrough DMs** (`_send_dm_and_persist` in `main.py`, `send_incident_dm` in `backend/slack.py`) are sent at dispatch time to the safe-list pilot cohort via the `SCCSSAR Dispatch Turbo Bot` (`U0XXXXXXXXX`) — designed to break through locked-phone Do-Not-Disturb / notification-schedule silences (see §8 Slack Integration). D4H incident records (`backend/d4h.py`) are also created automatically on each Everbridge send, pre-filled with intake form information and attendees, K9s, and UAS if requested by the dispatcher. Cloud Tasks drives polling cycles via the `everbridge-poll` and `d4h-per-yes-sync` queues. CalTopo incident map creation (`/create-map`, `backend/caltopo.py`) and Google Doc creation (`/create-doc`, `backend/gdocs.py`) remain live from Phase 1.5z. Slack is the official incident coordination channel; the WhatsApp `wa.me` deep link was retired in #614 and no longer exists in the UI.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  Browser (plain HTML/JS)                                            │
│  frontend/index.html                                                │
│                                                                     │
│  [Sign-In] → GET /verify (allowlist check on OAuth completion)      │
│  [Upload JPEG or PDF] → multipart POST /ocr                         │
│  [Review textarea] ← JSONResponse {text, map_data}                  │
│  [Primary dispatch — click in workflow order: Map → EB + Slack + D4H]:│
│     Create Incident Map      (POST /create-map → CalTopo API)       │
│     Everbridge + Slack + D4H (POST /send-notification → EB + Slack  │
│                               + D4H auto-create + Cloud Tasks chain;│
│                               gated on Map having been created)     │
│                                                                     │
│  [Helper / reference buttons — not part of the dispatch chain]:     │
│     View in D4H              (opens generic D4H /incidents page —   │
│                               manual reference only; per-YES sync   │
│                               runs server-side via /d4h-sync-yes    │
│                               regardless of this click)             │
│     Google Maps              (deep link to LKP — reference)         │
│     Google Doc               (POST /create-doc → Google Docs API)   │
└────────────────────────┬────────────────────────────────────────────┘
                         │ Authorization: Bearer {Google ID token}
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Google Sign-In (GSI library)                                       │
│  Verifies identity → issues ID token to browser                     │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Cloud Run gen2 — dispatch-console (FastAPI / Python 3.11)          │
│                                                                     │
│  Endpoint inventory — full per-endpoint detail in Component         │
│  Descriptions (§1–§11b) and the Request Lifecycle section below.    │
│                                                                     │
│  Utility / auth:                                                    │
│    GET  /health, /version              — Probes + footer SHA        │
│    GET  /verify                        — Allowlist fail-fast        │
│                                          (called post-OAuth)        │
│                                                                     │
│  Intake → dispatch (browser-driven):                                │
│    POST /ocr                           — JPEG 2-pass Gemini OR PDF  │
│                                          AcroForm (§3, §3a)         │
│    POST /create-map                    — CalTopo incident map (§7)  │
│    POST /create-doc                    — Google Doc working notes,  │
│                                          dispatcher OAuth (§7b)     │
│    POST /apply-staging-override        — Dispatcher staging         │
│                                          override (§6)              │
│                                                                     │
│  EB + Slack + D4H orchestration (browser-driven):                   │
│    GET  /everbridge-groups             — UI typeahead               │
│    GET  /everbridge-contacts           — OCEAN# auto-fill           │
│                                          (EB = identity SoT, §8)    │
│    POST /send-notification             — EB event + Slack channel + │
│                                          D4H auto-create + Cloud    │
│                                          Tasks chain (§8)           │
│    GET  /incident-status/{event_id}    — UI polling status,         │
│                                          ownership-gated            │
│    GET  /dispatch-status/{event_id}    — Team monitoring (allowlist │
│                                          only; D4H-aware; PII-free) │
│    POST /confirm-draft-sent/{event_id} — Safe-mode draft confirmed  │
│    POST /close-incident-polling/{eid}  — Dual-stop: EB GET-modify-  │
│                                          PUT + Firestore flag       │
│                                                                     │
│  Cloud Tasks → Cloud Run (OIDC, internal):                          │
│    POST /poll-incident/{event_id}      — Re-enqueues self; diffs EB │
│                                          ACKs → Slack (§11b)        │
│    POST /d4h-sync-yes                  — Per-YES D4H attendance     │
│                                          (max_attempts=5, §11b)     │
│    POST /delete-template/{template_id} — Cleanup safe-mode draft    │
│                                          (+30 min)                  │
└─────────────────────────────────────────────────────────────────────┘
    │ External service calls:
    ├─► Vertex AI / Gemini 2.5 Flash — OCR (JPEG: 2-pass; PDF: 1-pass text-only)
    ├─► Nominatim (OSM) + Google Maps fallback — street address → lat/lng
    ├─► Geoapify Places API — primary staging POI source, 1200 m radius
    │     (per-env key; STAGING_SOURCE=geoapify on both live envs;
    │      retried once at 4828 m when 1200 m returns zero candidates)
    ├─► Overpass API (OSM) — staging POI fallback, 1200 m radius, 2 mirrors
    │     (drives only when STAGING_SOURCE unset/overpass, else fallback-on-failure;
    │      same widened retry applies when it is the driving source)
    ├─► US Census Geocoder (keyless) — 2020 block urban/rural flag at the LKP,
    │     for the LPB environment line; coordinates rounded to 3 dp
    ├─► Open-Meteo Elevation (keyless) — terrain relief within 2 km,
    │     only when classified rural (incl. an urban block surrounded by
    │     wilderness); coordinates rounded to 3 dp
    ├─► CalTopo Team API (HMAC-SHA256) — map + marker creation
    ├─► Google Docs + Drive APIs (dispatcher drive.file token) — working notes
    ├─► Everbridge REST API (HTTP Basic) — groups, contacts, notification
    │     events, live send / draft template, polling, stop, template delete
    ├─► Slack Web API (bot token) — conversations.create, conversations.invite,
    │     chat.postMessage, users.lookupByEmail (EB-email join key), pins.add,
    │     conversations.open + DM chat.postMessage (Dispatch Turbo, safe-list cohort)
    ├─► D4H API v3 (Bearer PAT) — incident create + per-YES attendance sync
    │     (Selective mode: fullTeam=false, POST-new per YES)
    ├─► Cloud Tasks — schedules /poll-incident, /d4h-sync-yes, /delete-template
    │     against everbridge-poll and d4h-per-yes-sync queues (OIDC-signed)
    └─► Firestore — rate_limits collection (per-user) +
                    incidents collection (24 h TTL on expire_at, EB+Slack only)
```

---

## Component Descriptions

### 1. Frontend

**File:** `frontend/index.html` — single file, no build step, no framework.

Served as a static file by FastAPI. The dispatcher interacts with four phases:

1. **Sign-in** — Google Sign-In (GSI) library handles the OAuth flow and returns a Google ID token to the browser. The token is sent as a Bearer header on every API call.
2. **Upload** — JPEG photo of the handwritten call-out form, uploaded via multipart form POST to `/ocr`.
3. **Review** — Extracted text appears in an editable textarea. An amber warning banner prompts the dispatcher to verify all checkboxes (Q1–Q12) against the original photo before dispatching.
4. **Dispatch** — Two primary dispatch buttons in workflow order: **Create Incident Map** → **Everbridge + Slack + D4H** (one combined button — sends the EB notification, creates the Slack incident channel, and creates the D4H incident record together). The EB+Slack+D4H button is gated until the Map is created so the CalTopo URL is available for the Slack welcome message. A separate **Override Staging Location** panel (address / lat-lng / UTM input modes) sits below the textarea — promotes a dispatcher-chosen location to entry #1 in the Recommendations list and CP marker on the CalTopo map. Helper / reference buttons: **View in D4H** (opens generic D4H incidents page — manual reference; the auto-create + per-YES sync runs server-side regardless of this click), **Google Maps** (LKP deep link), **Google Doc** (working notes).

**Why plain HTML/JS:** No build toolchain to maintain. The dispatcher UI is intentionally simple — this is an operational tool used under time pressure, not a consumer app.

---

### 2. Backend API

| Property | Value |
|----------|-------|
| Language | Python 3.11 |
| Framework | FastAPI with uvicorn |
| Hosting | Cloud Run gen2, `us-central1` |
| Service name | `dispatch-console` |
| Dockerfile location | `backend/Dockerfile` (build context is repo root) |
| Build context | Repo root (includes both `backend/` and `frontend/`) |
| Scaling | Scales to zero when idle (`min_instance_count=0`) |

Key files:

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI app: `/health`, `/version`, `/verify`, `/ocr`, `/create-map`, `/create-doc`, `/everbridge-groups`, `/everbridge-contacts`, `/send-notification`, `/incident-status/{id}`, `/confirm-draft-sent/{id}`, `/close-incident-polling/{id}`, `/poll-incident/{id}`, `/delete-template/{id}` endpoints; format detection; geocoding; Overpass query; post-processing pipeline; Cloud Tasks orchestration |
| `backend/auth.py` | Google ID token verification + email allowlist check |
| `backend/gemini.py` | `SYSTEM_PROMPT` (JPEG two-pass OCR) + `STAGING_KOESTER_PROMPT` (PDF text-only call) + `google-genai` client with `api_version='v1'` pin |
| `backend/pdf_extract.py` | pymupdf AcroForm extraction + `build_synthetic_summary()` for v2 typed PDF forms; `_normalize_datetime()` for officer timestamp fields |
| `backend/caltopo.py` | CalTopo Team API integration: HMAC-SHA256 signing, map creation, marker placement (LKP, Residence, staging — no range rings as of Phase 1.5z2) |
| `backend/gdocs.py` | Google Docs + Drive API integration: creates Google Doc from textarea content using dispatcher's OAuth `drive.file` token; shares silently with all authorized dispatchers |
| `backend/everbridge.py` | Everbridge REST client: groups, contacts, notification events, live send / draft template, polling, GET-modify-PUT stop, template delete; OCEAN# extraction from `externalId` field; safe-list enforcement helpers |
| `backend/slack.py` | Slack Web API client: channel creation (`conversations.create`), invites, welcome + tally messages, lookups by email, pins; `_partition_arrivals_for_shadow()` 3-way YES classification (retained for shadow-path callers); `send_incident_dm()` + `format_incident_dm_text()` for Dispatch Turbo VIP-breakthrough DMs |
| `backend/incidents.py` | Firestore `incidents` collection helpers: state read/write, polling flag, manual_stop_requested, expire_at TTL boundary |
| `backend/d4h.py` | D4H API v3 client: incident create on send-notification (pre-filled with intake form data + K9/UAS tabs), per-YES attendance sync via Cloud Tasks (`d4h-per-yes-sync` queue, `max_attempts=5`), Selective attendance mode (`fullTeam: false`) per CLAUDE.md Locked Decision |
| `backend/image_validation.py` | JPEG and PDF magic-byte detection, size/dimension bounds |
| `backend/rate_limit.py` | Firestore-backed per-user rate limiter (separate from `incidents` collection) |
| `backend/test_pdf_extract.py` | 99 unit tests for PDF extraction pipeline (AcroForm field parsing, `build_synthetic_summary()`, time/datetime normalization) |

---

### 3. OCR and Reasoning (Gemini 2.5 Flash)

The AI component does two things: read the handwritten form image, and format the results using real geographic data.

**Two-pass design:**

- **Pass 1** — Raw image is sent to Gemini with a structured extraction prompt. Gemini reads all form fields including the Last Known Position (LKP) address. Address spelling is verified and corrected as part of this pass.
- **Pass 2** — The verified LKP is geocoded (see Nominatim below), nearby POIs are fetched (see Overpass below), and then a second Gemini call formats the full output with real coordinates and real staging candidates injected as context.

**Why two passes:** Gemini cannot know the real-world coordinates or nearby businesses from the image alone. Injecting pre-fetched OSM data into Pass 2 gives Gemini real locations to choose from, eliminating hallucinated staging areas.

**Configuration:**

| Setting | Value |
|---------|-------|
| Model | `gemini-2.5-flash` (controlled by `GEMINI_MODEL` env var) |
| Temperature | 0.1 (near-deterministic output) |
| Max output tokens | 16,384 (raised from 8,192 after confirmed truncation in testing; Gemini 2.5 Flash supports up to 65,536) |
| Access method | `google-genai` SDK in Vertex AI mode (`genai.Client(vertexai=True, ...)`) — not the Gemini Developer API |
| Region | `us-central1` |

**Finish reason guard:** When Gemini's output is cut off by the token limit, `finish_reason` is set to `MAX_TOKENS`. The response is incomplete and must not be returned to the dispatcher. The `gemini.py` call raises a `RuntimeError` immediately when `finish_reason == types.FinishReason.MAX_TOKENS`, causing a HTTP 502 response with a "retry" message. The dispatcher sees an error rather than silently receiving a truncated form that looks complete. (Under the legacy `vertexai.generative_models` SDK this comparison was against the integer `2`; the migration to `google-genai` switched it to enum equality — see CLAUDE.md "Vertex AI SDK — `google-genai`, NOT `vertexai.generative_models`".)

To detect in logs:
```bash
gcloud logging read "textPayload:\"finish_reason\"" --limit=10 --project sar-dispatch-dev
```

**Prompt placeholders replaced at call time:**

| Placeholder | Injected value | Example |
|-------------|----------------|---------|
| `__INTAKE_TIMESTAMP__` | Current Pacific time | `2026-02-19 15:22` |
| `__CURRENT_DAYTIME__` | Day name + time PT | `Wednesday, 15:22 PT` |
| `__LKP_COORDS__` | Geocoded lat/lng + area | `37.40087,-121.88387 (Berryessa, San Jose, CA)` |
| `__STAGING_CANDIDATES__` | Pre-fetched OSM POI list | Up to 12 candidates with type, address, distance |

---

### 3a. PDF AcroForm Path

When an officer fills the v2 form digitally and submits as a PDF, the backend reads AcroForm field values directly — no image interpretation, no OCR. This gives 100% deterministic questionnaire accuracy on Q1–Q12, vs the JPEG path's ~8–10/12 OCR accuracy. PDF requires 1 (text-only) Gemini call for staging + Koester; JPEG requires 2 (Pass 1 OCR + Pass 2 formatting).

`build_synthetic_summary()` in `pdf_extract.py` formats the AcroForm dict into the same labeled-field text Gemini Pass 1 would produce, so the geocoding pipeline, Overpass query, and staging/Koester Gemini call are shared between the two paths.

**pymupdf dependency:** `pymupdf==1.25.4` is an in-process Python library. No new GCP APIs, IAM grants, or secrets are needed for PDF ingest.

The amber checkbox warning banner shows for both paths; for PDF it's a UI safeguard the dispatcher can dismiss.

---

### 4. Geocoding (Nominatim → Google Maps fallback)

The backend uses a two-tier geocoding cascade for every address (LKP, Residence, and alternate staging entries):

**Tier 1 — Nominatim (OpenStreetMap, free)**
Nominatim converts a street address into latitude/longitude coordinates. No API key required; a descriptive `User-Agent` header is required by OSM's terms of service. Fast (1–2s), no cost, works for well-formed addresses in California.

**Tier 2 — Google Maps Geocoding API (fallback, paid)**
When Nominatim returns no result — most commonly because the officer misspelled the street name — the backend retries with the Google Maps Geocoding API (`_geocode_google_maps()` in `main.py`). Google Maps has significantly better spelling-correction tolerance than Nominatim. For example, an officer writing `"TRADEN DR"` instead of `"TRADAN DR"` fails Nominatim entirely but resolves correctly via Google Maps.

Google Maps is **optional** — the fallback is skipped if `GOOGLE_MAPS_API_KEY` is not configured (empty env var). When configured, it is called for LKP, Residence, and alternate staging entries (Pass B) in that order.

**Spelling correction propagation** — When Google Maps corrects a street spelling, `_street_correction_note()` detects the difference between the officer's input and Google's `formatted_address`. `_apply_street_correction_to_summary()` then propagates the corrected spelling throughout the full summary text — LKP field, Event Log, Staging Area Recommendations, and the Event Name. This ensures the CalTopo map, the Slack incident messages, the D4H payload, and the Google Doc all use the verified spelling, not the officer's misspelling.

**Google Maps API configuration:**
- Secret: `google-maps-api-key` in Secret Manager
- Cloud Run env var: `GOOGLE_MAPS_API_KEY` (injected via Terraform)
- API to enable: "Geocoding API" at GCP Console → APIs & Services → Library

**City/state cascade** — Bare street addresses without a city produce ambiguous results (a past bug matched a San Jose street to the Netherlands). The backend resolves city/state in this priority order before calling either geocoder:

1. If the LKP address already contains a comma, use it as-is.
2. Fall back to the Residence Address field from the form.
3. Fall back to an agency abbreviation lookup table (e.g., `SJPD` → `San Jose, CA`; `SCSO` → `Santa Clara County, CA`).
4. Append `, CA` unconditionally if `CA` or `California` is absent — SCCSSAR operates predominantly in California.

**Blank LKP — residence-as-geocode-anchor fallback (PR #191)** — When an officer leaves the Last Known Position field blank (PDF AcroForm writes `"[not recorded]"`), the geocode query would normally be empty, causing the entire staging/Koester/CalTopo pipeline to be skipped. Instead, if the Residence address is available, the backend uses it as the geocoding anchor so staging recommendations, the Koester analysis, and the CalTopo map are still generated. A `WARNING:` entry is added to the Event Log: `"Last Known Position not provided — staging and CalTopo map based on Residence address; confirm actual LKP with officer"`. The Event Name is reconstructed from the Residence street using the same stripping pipeline (apt/unit → cardinal direction → street type). If the officer later provides an LKP, the dispatcher should resubmit the form.

City abbreviations ≤3 characters (e.g., `SJ`, `LA`) are rejected as city context candidates — they are truthy but useless and silently block the agency-lookup branch.

**Apartment/unit qualifier stripping** — Before sending either the LKP or Residence address to any geocoder, the backend strips apartment and unit qualifiers (`Apt`, `Apartment`, `Unit`, `Suite`, `Ste`, `#` followed by a number/letter), which tend to confuse geolocaters. The original address with the qualifier is preserved in the CalTopo marker label and the textarea output — only the geocoding query is simplified.

**Output:** `lat,lng` pair used for:
- CalTopo map centering (`LKP_LL:` line in text response, parsed by frontend for legacy CalTopo button)
- `map_data.lkp.lat/lng` in the JSON response (used by `/create-map` endpoint)
- Overpass API query center point

---

### 5. Staging POI Lookup (Geoapify primary → Overpass fallback)

The backend fetches real-world points of interest near the LKP so Gemini selects staging from actual nearby locations instead of guessing from training data. As of the Overpass→Geoapify migration (v1.11.x, July 2026) there are **two interchangeable POI sources** behind one internal dispatcher (`_query_staging_pois()` in `main.py`), each returning the identical `(candidates, school_count, church_count, source_ok)` 4-tuple so every downstream caller is source-agnostic:

- **Geoapify Places API** — primary on both live environments.
- **Overpass API (OpenStreetMap)** — the historical source, now the automatic fallback.

Both feed the same shared ranking helper (`_rank_dedupe_cap_staging()`): dedupe by house+street, tier-sort by `(tier, distance)`, cap at 12. Candidates from either source carry the same OSM amenity vocabulary, so the tier table, the Gemini injection format, and all post-processing (§6) are unchanged regardless of which source answered.

#### Source selection (`STAGING_SOURCE` / `STAGING_SHADOW`)

Two env flags — same per-env rollout pattern as `EVERBRIDGE_MODE`/`SLACK_MODE`, both fail-fast at module import on an unrecognized value:

| Flag | Values | Meaning |
|---|---|---|
| `STAGING_SOURCE` | `overpass` (default when unset) · `geoapify` | Which source *drives* the returned result. |
| `STAGING_SHADOW` | `on` (default) · `off` | `on`: run **both** sources in parallel every lookup — the active source drives, the other is a measured-only shadow (emits the `Staging source compare` log). `off`: run **only** the active source, calling the other **sequentially only if the active one fails** (no parallel wait). |

**Live configuration (July 2026):**

| Environment | `STAGING_SOURCE` | `STAGING_SHADOW` | Behavior |
|---|---|---|---|
| **personal-dev** (stable) | `geoapify` | `on` | Parallel shadow soak — Geoapify drives, Overpass measured every lookup for A/B latency + overlap. |
| **sccssar-dev** (active) | `geoapify` | `off` | Sequential — Geoapify drives; Overpass called only on Geoapify failure. No per-`/ocr` Overpass latency. |
| default (flags unset) | — | — | Overpass drives exactly as pre-migration; Geoapify never called. |

Both flags are Terraform-managed. On sccssar-dev they are hardcoded in `main.tf` (`STAGING_SOURCE=geoapify`, `STAGING_SHADOW=off`); on personal-dev `staging_source` is a `terraform.tfvars` variable (default `geoapify`) and `STAGING_SHADOW` is left unset (defaults to `on`). Changing either flag is an env-var change and therefore requires `terraform apply` — the build scripts do not pick Terraform changes up.

#### Shared query parameters and tier sort

Applies to both sources:
- Radius: 1200m (~0.75 miles) from the geocoded LKP
- Widened retry: if the 1200m query returns **zero** candidates and the source itself was
  healthy, the lookup runs once more at `_STAGING_FALLBACK_RADIUS_M` = 4828m (3.00 mi), and
  an Event Log note tells the dispatcher the search was widened and that results are farther
  out than usual. Zero at 1200m is usually a *rendering* outcome rather than a remote LKP:
  at a measured suburban anchor the provider returned 28 features inside 1200m and not one
  carried a house number, so every one was dropped by the §6 PASS 2 leading-digit predicate.
  The retry fires only on zero, so the common path is untouched and nothing new competes for
  the 7-slot cap or the provider's 100-feature cap. Well inside `_MAX_STAGING_DIST_M`, so the
  staging distance guard does not reject the results.
- POI types: fast food, gas station, pharmacy, hotel, motel, school, place of worship, convenience store, supermarket, grocery store, park
- Returns: up to 12 candidates after ranking

Candidates are sorted server-side by `(tier, distance)` before being sent to Gemini. The sort is deterministic and immutable — Gemini is instructed to work top-to-bottom in the order provided, without reordering.

| Tier | POI types | Rationale |
|------|-----------|-----------|
| 1 (preferred) | `park`, `fast_food`, `pharmacy`, `hotel`, `motel`, `supermarket`, `grocery`, `school` | Large parking, restrooms, open space, or covered shelter. Schools are tier 1 so they survive the staging cap in dense urban areas — the only time they're excluded is during school hours, which is handled downstream by Gemini's prompt rules (PR #176) |
| 2 (secondary) | `convenience`, `chemist`, `place_of_worship` | Parking varies; time-of-day restrictions may apply; Sunday worship hours are excluded for places of worship |
| 3 (last resort) | `fuel` | Active vehicle traffic conflicts with personnel staging; limited seating and restrooms |

#### Primary: Geoapify Places API

Geoapify's `/v2/places` endpoint (`X-API-Key` header; per-project key in the `geoapify-api-key` secret) is queried with **two concurrent category calls** — a civic set (`_GEOAPIFY_CIVIC_CATEGORIES`: schools, parks, places of worship) fetched unbiased, and a commercial set (`_GEOAPIFY_COMMERCIAL_CATEGORIES`: fuel, food, pharmacy, lodging, retail) fetched with proximity bias. The split exists because a single mixed Geoapify query ranks by relevance rather than distance, which starves civic POIs. Results are mapped to the OSM amenity vocabulary via `_GEOAPIFY_PRIORITY` (best-tier-first) and normalized to the OSM address shapes downstream already speaks (`_geoapify_addr_shape`). Typical latency: ~0.5–1.1s (roughly half the Overpass path).

**Parks return the `(address not in OSM)` sentinel, never a city string** (`_geoapify_addr_shape`, PR `a31f7f5`) — Geoapify populates `city` for nearly every park (unlike OSM/Overpass parks, which usually lack `addr:city`), and the shared dedupe keys on the pre-comma text *unless* it is that sentinel. Returning the bare city would collapse all same-city parks to one as false strip-mall duplicates. The sentinel makes Geoapify parks behave exactly like Overpass parks (exempt from address dedup; dropped from the `/apply-staging-override` navigability filter). This is a CLAUDE.md Locked Decision.

> **Geoapify does not fix OSM's wilderness gaps.** Remote/rural anchors (e.g. Joseph D. Grant County Park, San Felipe Rd) are sparse in *both* sources' underlying OSM data — Geoapify is faster and more reliable, not more complete. Adaptive-radius-on-≤1-POI is tracked in issue #587.

#### Fallback: Overpass API (OpenStreetMap)

Overpass queries the OSM database directly. Parks use `nwr` (nodes, ways, and relations) with `out center body` to get the polygon centroid for distance calculations (OSM parks are ways/relations, not nodes). It drives when `STAGING_SOURCE` is unset/`overpass`, and otherwise is called only when the primary fails.

**Mirror fallback chain** (tried in order if the previous returns an error or HTTP 504) — see `_OVERPASS_MIRRORS` in `main.py`:

1. `overpass-api.de` (primary — returns HTTP 504 under load; see the response-time profile below for why this does not warrant reordering)
2. `overpass.private.coffee` (was `overpass.kumi.systems` — domain renamed)

`maps.mail.ru` (VK Maps) was removed 2026-03-16 after the operator officially suspended the public mirror. All mirrors fail → log warning, return empty candidate list. If Geoapify has already answered (sequential mode) the dispatcher never sees this; if both sources fail, Gemini falls back to training-data knowledge for staging (less reliable).

**Response-time profile and per-mirror timeout (issue #551, May 2026):** A 30-day analysis of Cloud Run logs across both environments (158 Overpass calls) measured the per-mirror response-time distribution:

- `overpass-api.de`: p50 ≈ 1.3s, p95 ≈ 8.3s, p99 ≈ 11.5s; max observed success 11.9s; ~88% of successful queries complete in under 3s. Its failure mode is exclusively HTTP 504 returned within ~8–12s — the per-mirror timeout never fired on the primary across 30 days.
- `overpass.private.coffee`: when reachable, ~11s; when down, it accepts the TCP connection then hangs to the full timeout.

The per-mirror timeout is **12s**, cut from 18s based on this data (no observed success ever reached the 12–18s band, so the cut sacrifices no real successes). On a full-cascade Overpass outage (primary 504 → backup timeout) the wait is ~22s — but with Geoapify primary + sequential fallback, Overpass is only reached at all when Geoapify itself failed, so this worst case is now rare. This profile also argues *against* reordering the mirrors: the primary fails fast (quick 504) while the backup fails slow (full-timeout hang), so a swap would make the common single-mirror-down case slower. Full analysis with histograms and the reusable log-parsing script: `research/overpass-timeout-analysis/` (gitignored).

#### Observability

Each staging lookup emits exactly one PII-safe log line — counts, latencies, radius, and ok flags only, never POI names, addresses, or coordinates (the name sets are built locally and only their sizes are logged):

| Log line | When | Key fields |
|---|---|---|
| `Staging source compare \| active=…` | `STAGING_SHADOW=on` (parallel) | both counts, name/tier1 overlap, both latencies, both ok flags |
| `Staging sequential \| source=geoapify` | `STAGING_SHADOW=off`, primary succeeded | geoapify count + `geoapify_ms` + radius |
| `Staging fallback \| primary=geoapify backup=overpass` | active source failed → fell back | backup ok flag (+ both latencies in sequential mode) |

---

### 6. Gemini / staging-source Interaction and Staging Recommendation Logic

Understanding how Gemini and the staging source (§5) work together — and how staging recommendations are calculated and ordered — is critical for debugging and for understanding what the dispatcher sees.

#### Step 1: Server builds the candidate list (staging source → tier sort → inject)

The backend queries the staging source (Geoapify primary, Overpass fallback — §5) for real POIs, deduplicates by house+street, sorts by `(tier, distance)`, caps at 12 candidates, then injects them into the Pass 2 Gemini prompt as an `INTERNAL STAGING DATA` block — explicitly flagged as not to be reproduced in the output.

Each candidate line in the injected block looks like:
```
  1. McDonald's — 123 Main St, San Jose [Fast food, 0.28 mi from LKP]
  2. CVS Pharmacy — 456 Oak Ave, San Jose [Pharmacy, 0.31 mi from LKP]
  3. Cardoza Park, Milpitas [City park, 0.45 mi from LKP]
  4. ...
```

The server sort is **authoritative**. Gemini is not permitted to reorder.

#### Step 2: Gemini selects from the list (top-to-bottom, no reordering)

Gemini receives explicit instructions:
- Work through the list **top to bottom** in the order provided
- Do **not** reorder based on its own judgment of parking, lighting, or other factors
- Skip a candidate **only** if it is excluded by a time-of-day rule (school hours, church hours)
- Do not skip candidates for perceived parking or lighting concerns — include them; the parking estimate in the output is sufficient for the dispatcher to decide

**Time-of-day exclusion rules** (prompt-enforced, using injected `__CURRENT_DAYTIME__`):
- Elementary/middle schools: excluded 7 am–3:30 pm weekdays; available evenings, weekends, holidays
- Churches/places of worship: less preferred Sunday morning 8 am–12 pm only; available all other times

**Geographic diversity rule** (prompt-enforced):
- Maximum 2 candidates from any single street name
- Prevents clustering (e.g., four locations all on South Park Victoria Drive within 0.4 mi)

#### Step 3: Server-side ordering, merging, and capping (issue #244)

After Gemini formats the staging section, the backend applies three post-processing passes:

**PASS 1 — Park address stripping:** Park entries are reformatted to `"Park Name, City"` (street addresses stripped — parks have multiple entrances, a specific address is misleading).

**PASS 2 — Filter, validate, renumber (cap = 7):**
- Drop entries with no `" — "` separator (no address, not a park)
- Drop entries where `loc_part` (text before `" — "`) has no leading digit and is not a park (name-only entries like `"Milpitas Unified School District — School"` are unnavigable)
- Renumber remaining entries 1–7; entries beyond 7 are discarded

**PASS 3 — Officer staging injection:**
If the officer wrote a `Staging Area for Resources` and it does NOT appear in the PASS 2 list, it is appended as the next numbered entry (8 when all 7 slots are full) labeled `" — Officer-designated staging location (not among top recommendations — dispatcher discretion)."` If it matches an existing entry, that entry is labeled in-place and the total stays ≤7.

**LKP/Residence NOT in list:** LKP and Residence are shown as dedicated CalTopo markers — they do not appear in the Staging Area Recommendations text at all (issue #244).

**Total list length: 7 or 8 entries.** 7 when officer staging matches a recommendation or is blank. 8 when officer staging is set and is not among the top 7. A list of 8 is correct behavior — do NOT treat it as a bug.

**When no officer staging:** Up to 7 quality-ranked recommendations only.

**Address-less entries dropped:** Staging candidates that have no street address (e.g., `"Silicon Valley University"` — name only) are filtered out after Gemini's output, then the list is renumbered consecutively.

**Park address stripping:** Park and open-space entries display as `"Park Name, City"` only — no street address. Parks have multiple entrances; a specific street address is actively misleading. Applied by `_strip_park_address()` in `main.py`.

---

### 7. CalTopo Incident Map Integration

The **Create Incident Map** button triggers a POST to `/create-map`, which builds a complete incident map in CalTopo using the CalTopo Team API (HMAC-SHA256 signed).

**Map creation flow (`caltopo.py`):**

1. Create a new map with the LKP marker as the initial feature (seed)
2. Add the Residence marker
3. Add staging markers (officer-designated + recommended alternates from Gemini output)
4. Return the map URL to the frontend

(Range rings were removed in Phase 1.5z2 — see the [Koester LPB Range Ring Analysis](#koester-lpb-range-ring-analysis) section for context. Distances appear as text in the Full Incident Summary only.)

**Marker types and symbols:**

| Symbol string | Icon | Used for |
|---------------|------|---------|
| `placemark2` | Standard push pin (red) | LKP — last known position |
| `hut` | House icon (red) | Residence |
| `cp` | Command Post / ICS icon (red) | Officer-designated staging (#1 in list) |
| `point` | Plain dot (blue) | Alternate/recommended staging locations |

**Why officer staging uses `cp` and alternates use `point`:**
The Command Post icon is visually distinctive and makes the operationally authoritative location immediately visible on the map. Alternate staging locations use a plain blue dot — visually subordinate, clearly dispatcher choices rather than officer directions. Dispatchers can use the alternates during an incident as needed, or delete them from the CalTopo map if visually distracting.

**Residence marker always plotted:**
The residence marker is always added, even when Nominatim geocoding fails for the residence address. In that case, it falls back to LKP coordinates, with a note in the marker description: `"(Note: geocoding failed — plotted at LKP)"`. A missing residence marker is operationally worse than a duplicate at LKP.

**Map creation seed:**
CalTopo requires ≥1 feature in the initial map state. The LKP marker is the most important marker on the map and always exists, so it is used as the first (seed) feature in the map creation POST. The map is immediately useful even if subsequent marker calls fail.

**Safari popup blocker fix:**
The `Create Incident Map` button opens a blank window synchronously on click (before the async fetch), then redirects `mapWin.location.href` to the CalTopo URL on success. This satisfies Safari's requirement that `window.open()` be called synchronously within a user gesture handler.

---

### 7b. Google Docs Working Notes Integration

The **📄 Google Doc** button (`/create-doc` endpoint, `backend/gdocs.py`) creates a Google Doc pre-populated with the full textarea content (incident summary, event log, LPB questionnaire, staging recommendations, and Koester analysis) and shares it silently with all other authorized dispatchers. Implemented in Phase 1.5z (PRs #197–#202, #205, #211–#213).

**Button states (frontend state machine):**

| Button label | State | What it does on click |
|---|---|---|
| `📄 Google Doc` | Initial (no doc yet) | Requests Drive token, then creates the doc |
| `⏳ Authorizing…` | Token request pending | Waiting for Google OAuth consent/token |
| `⏳ Creating doc… (Ns)` | POST `/create-doc` in flight | Live elapsed-seconds counter updates every second |
| `📄 View Google Doc` | Doc created | Opens the existing doc URL — does NOT create a second doc |

The "View Google Doc" state persists until "Clear & Start Over" is clicked, which resets the button to the initial state and clears `_docUrl`.

**OAuth flow (dispatcher token, not service account):**

1. Dispatcher clicks "📄 Google Doc." Button shows `⏳ Authorizing…`.
2. Frontend calls `google.accounts.oauth2.initTokenClient()` requesting `https://www.googleapis.com/auth/drive.file` scope — same OAuth client ID already loaded for Google Sign-In.
3. **First use:** Google consent dialog appears. **Subsequent uses within the token lifetime:** silent (~1 second).
4. Short-lived access token forwarded in the POST body (`drive_access_token`) to `/create-doc`.
5. Backend builds `google.oauth2.credentials.Credentials(token=access_token)` — no service account, no persistent credential.
6. Doc is created in the dispatcher's own Google Drive. Dispatcher is the owner.
7. Shared as `writer` (silent, no notification email) with every other email in `AUTHORIZED_EMAILS`.
8. Doc URL returned; button shows `📄 View Google Doc`.

**Why dispatcher OAuth, not service account:**
- GCP service accounts have no Google Drive storage quota — `files.create()` returns `storageQuotaExceeded` (403). There is no way to grant a service account a Drive storage quota in a standard GCP project without domain-wide delegation.
- Domain-wide delegation requires Workspace org-admin access and a broad impersonation scope — rejected on security grounds.
- Dispatcher OAuth eliminates both problems: doc lives in the dispatcher's Drive (their storage quota), no server-side credential, token is short-lived with `drive.file` blast radius only.

**`drive.file` scope — principle of least privilege:**
The token can only access files created or opened by this app. It cannot enumerate or read any other file in the dispatcher's Drive. Exfiltration window: ~1 hour (token lifetime). Blast radius: files this app created.

**Doc content and title:**
- Title: `{event_name} — Working Notes`
- Header line: `Created by SCCSSAR Dispatch Console for {LastName} — YYYY-MM-DD HH:MM UTC`
- Body: full textarea content verbatim (all sections, no reformatting)

**Sharing logic:**
The dispatcher who created the doc already owns it. Their email is filtered out of `AUTHORIZED_EMAILS` before sharing — adding the owner again causes a Drive API 400. Sharing failures for individual recipients are logged (without the email address, to avoid PII in Cloud Run logs) but do not abort the operation.

**Thread safety — serial API calls required:**
`googleapiclient` is NOT thread-safe. `ThreadPoolExecutor` inside `create_incident_doc()` causes SIGABRT (signal 6) and crashes the Cloud Run instance (confirmed in PR #213). All API calls — `discovery.build()`, `batchUpdate()`, each `permissions.create()` — must be serial. The entire function runs in a thread pool via `asyncio.run_in_executor()` to avoid blocking the event loop, but internally all calls are sequential.

**Prerequisites (must be enabled in GCP Console before this feature works):**
- Google Docs API enabled on the project
- Google Drive API enabled on the project

See Section 15 and the deployment guide Section 4 for setup instructions.

---

### 8. External Dispatch Targets

| Target | Current State | Integration Method |
|--------|-------------|-------------------|
| Everbridge | ✅ Live — automated dispatch (Phase 1.8) | POST `/send-notification` → EB REST API (3-call flow: create event → send/template → poll) |
| Slack | ✅ Live — private per-incident channels (Phase 1.8) | POST `/send-notification` → Slack bot token (`channels:create`, `chat:write`, `users:read.email`) |
| CalTopo | ✅ Live — full incident map creation (Phase 1.5q+) | POST `/create-map` → CalTopo Team API (HMAC-SHA256) |
| D4H | ✅ Live — auto-create + per-YES attendance sync (Phase 2 Selective mode) | POST `/send-notification` orchestration creates the incident; per-YES sync via the `d4h-per-yes-sync` Cloud Tasks queue calling `/d4h-sync-yes` (`max_attempts=5`). "View in D4H" helper button opens the team incidents page as manual reference (sync runs server-side regardless of this click). |
| Google Doc | ✅ Live — working notes doc (Phase 1.5z) | POST `/create-doc` → Google Docs + Drive APIs (dispatcher OAuth, `drive.file` scope) |
| Google Maps | Reference tool | Deep link to LKP coordinates |

#### Identity model (cross-system join key)

Everbridge is the authoritative store for each member's SAR email address. Three downstream consumers use that email as the join key when matching a responder across systems:

- **D4H per-YES attendance sync** (`backend/d4h*.py`) — looks up the D4H member by the EB-stored email when an EB poll surfaces a confirmed YES, then POSTs the ATTENDING record.
- **Slack channel invites** (`backend/slack.py`) — calls `users.lookupByEmail` on the EB-stored email; safe-list gating during the rollout window runs against the same key.
- **`#active-incidents` tally** — running responder count keyed off the same lookups.

A roster change in EB propagates to the other systems on the next dispatch without any separate sync step. The Slack lookup uses `@sccssar.org` by convention (Locked Decision in CLAUDE.md: "Slack email lookup"); the SAR coordinator at the `slack-so-coordinator-email` secret is the documented cross-domain exception.

#### Everbridge Integration (Phase 1.8)

**3-call send flow:**
1. `POST /notificationEvents/{orgId}` — create named notification event
2. `POST /notifications/{orgId}` — send live (EVERBRIDGE_MODE=full) or `POST /notificationTemplates/{orgId}` — create draft for dispatcher to confirm (EVERBRIDGE_MODE=safe)
3. Cloud Tasks queue (`everbridge-poll-queue`) schedules `/poll-incident/{event_id}` cycles — each cycle calls `GET /notifications/{orgId}/{id}?verbose=true` to read responder ACKs and posts diffs to Slack

**Stop flow:** `/close-incident-polling/{event_id}` — GET-modify-PUT against EB API (sets `notificationStatus=Stopped`) + sets `manual_stop_requested=True` on Firestore doc. Already-stopped returns HTTP 400 — swallowed as success.

**Feature flags:**

| Env var | Values | Effect |
|---------|--------|--------|
| `EVERBRIDGE_MODE` | `off` / `safe` / `full` | `off` = button hidden; `safe` = draft+manual confirm; `full` = live send |
| `SLACK_MODE` | `off` / `shadow` / `full` | `off` = no channel; `shadow` = log only (no real invites); `full` = real channel invites |

**Firestore `incidents` collection** — stores polling state per incident. Fields: `event_id`, `notification_id`, `polling_active`, `manual_stop_requested`, `manual_confirm_offered`, `responders` (dict of contact → last ACK), `slack_dm_sent_user_ids` (list of Slack user IDs that have already received a Dispatch Turbo DM — idempotency guard so a polling retry never double-DMs), `expire_at` (24h TTL, auto-delete for PII boundary). Separate from the `rate_limits` collection.

**OCEAN# auto-fill:** `/everbridge-contacts` calls `GET /contacts/{orgId}?pageSize=1000`, extracts the dispatcher's `externalId` by email match, parses the last 3 digits as the OCEAN number. Displayed pre-filled in the send UI.

#### Slack Integration (Phase 1.8)

**Channel creation:** `conversations.create` with name `YYYY-MM-DD_agency_street` (private channel). Welcome message posted via `chat.postMessage` (3-line format: event name, MP line, staging links). CalTopo URL posted as a separate follow-up message (so it unfurls — Slack URL dedup suppresses unfurl on first message if already seen in the channel).

**Initial channel members (issue #579):** On channel creation the bot pre-adds a fixed set — **the dispatcher, the SO Coordinator (`SLACK_SO_COORDINATOR_EMAIL`), and every member of the `@active_incident_management` Slack user group** (`get_active_incident_management_members()` reads `usergroups.users.list` for the group ID in `ACTIVE_INCIDENT_MGMT_USERGROUP_ID`). This is the admin-curated Search-Management roster; it deliberately does NOT source from `#active-incidents` membership — that is an open watch-channel, and sourcing pre-adds from it pulled curious lurkers into every incident. The SO Coordinator is a Slack guest (Slack excludes guests from user groups), so they are added by email rather than via the group. Confirmed (YES) responders are invited separately at poll time. `dispatch-safe-list` is NOT a pre-add source — it is the VIP-DM / EB cohort only. The group read is best-effort: it runs after Everbridge has already fired, so a Slack error or an unset env var degrades to dispatcher + SO Coordinator rather than crashing the dispatch.

**#active-incidents tally:** `chat.postMessage` to `ACTIVE_INCIDENTS_CHANNEL_ID` after send, updated by each polling cycle with responder confirmations. (`#active-incidents` remains a read-only status feed here — its *membership* is no longer an invite source; see Initial channel members above.)

**Shadow mode (SLACK_MODE=shadow, personal-dev only):** Channels are created but confirmed-responder invites are not sent at poll time. New YES respondents are classified into three groups by safe-list match: match → `<Name> added`; has email but not safe-listed → `Would invite: <Name>` (logged, not invited); no email → `Cannot invite: <Name>`. The safe-list is used here as a *proxy* for send-time channel membership; since #580 the actual pre-add set is dispatcher + SO Coordinator + `@active_incident_management` (not the safe-list), so this label can be inaccurate on a group-vs-safe-list mismatch — a cosmetic transcript issue tracked in #581.

**Unfurl pattern:** Welcome message sent with `unfurl_links=False, unfurl_media=False` (no CalTopo URL in this message). Separate follow-up message contains bare CalTopo URL with default unfurl. Both messages pinned to the channel.

**Dispatch Turbo — VIP Breakthrough DM (v1.10.0):** At send time (`/send-notification` step 7b), after the incident channel is created and its initial members are invited, `_send_dm_and_persist()` in `main.py` sends each initial member whose email is on the `dispatch-safe-list` (the VIP-DM pilot cohort) a direct message via `send_incident_dm()` in `backend/slack.py`. The bot (`SCCSSAR Dispatch Turbo Bot`, display name `@sccssar_dispatch_bot`, user ID `U0XXXXXXXXX`) sends a two-step DM: `conversations.open` returns an IM channel ID, then `chat.postMessage` posts a formatted link to the incident channel. Each Slack user ID that receives a DM is written to `slack_dm_sent_user_ids` on the Firestore incidents doc via `ArrayUnion` — the polling loop reads this set before any retry DM to prevent duplicates. The VIP breakthrough mechanism: team members add the bot to their iOS **VIPs** list and enable "Always allow notifications from VIPs" in Focus mode — this causes a DM from the bot to break through Do-Not-Disturb and notification-schedule silences on a locked or killed-app phone. `DISPATCH_TURBO_TEST_LABEL` env var (set via `gcloud run services update`, not yet in Terraform) prepends a visible test prefix to the DM text so pilot test messages are clearly labeled before they reach real team members. At poll time, the same `_send_dm_and_persist` path fires for YES respondents whose DM was not yet delivered at send time (e.g., joined Slack after dispatch).

**CalTopo map URL write-back:** After `Create Incident Map` succeeds, the map URL is written back to the `CalTopo Map ID:` field in the textarea, and is posted to the incident channel as its own pinned Slack message.

#### WhatsApp Business API — Investigation Summary (Feb 2026)

The WhatsApp Business API (Cloud API, hosted by Meta) was evaluated as a potential path to automate the team notification message. **Conclusion: not viable for SCCSSAR. The `wa.me` deep-link approach is the correct permanent design.**

**What the API can do:**
- Account setup is free. Programmatic message sending is possible via Meta's [WhatsApp Cloud API](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started).
- Service conversations (user messages your number first) are free and unlimited.
- Business-initiated template messages cost ~$0.07–0.13/message — negligible at SAR volumes.

**Why group messaging is blocked:**
The [WhatsApp Groups API](https://botpenguin.com/blogs/whatsapp-api-for-group-chat) requires *one of*:
- Official Business Account (OBA) status — Meta's verified green-checkmark tier, requiring significant platform presence, OR
- Handling **100,000+ business-initiated messages per 24-hour window**

SCCSSAR will never qualify on either criterion. This is not a temporary restriction — it is a deliberate enterprise-only gate.

Additional hard constraints even if the gate were cleared:
- API-managed groups are capped at **8 participants** — smaller than the SCCSSAR team roster.
- The API can only manage groups it creates; **existing groups cannot be retrofitted**.
- Interactive buttons and templates do not work in group contexts.

**The one viable API path — broadcast to individuals:**
Sending individual template messages to each team member's personal number (a broadcast list) does not require the Groups API and avoids the 8-member cap. This would technically work at SCCSSAR's scale. However, it trades one manual click for: collecting opt-in consent from ~20 team members, Meta template approval (subject to rejection), WhatsApp Business Account management, and per-message fees. The total setup and maintenance burden is not justified for a message that already takes ~15 seconds to send manually via the pre-filled `wa.me` link.

**References:**
- [WhatsApp Cloud API — Meta Developer Docs](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started)
- [WhatsApp API for Group Chat (BotPenguin)](https://botpenguin.com/blogs/whatsapp-api-for-group-chat)
- [WhatsApp Groups API for Business (Sanuker)](https://sanuker.com/whatsapp-groups-api-en/)
- [WhatsApp Business API Pricing 2026 (respond.io)](https://respond.io/blog/whatsapp-business-api-pricing)

---

#### D4H Integration (Phase 2)

**Auto-create on dispatch:** `/send-notification` orchestration calls `backend/d4h.py` to create a D4H incident record at the same time as the Everbridge notification and Slack channel. The incident is pre-filled with intake form data (subject name + age + at-risk factors, LKP, agency, event name). If the dispatcher selected K9 or UAS groups in the Everbridge send, the corresponding D4H sub-tabs are attached at creation time. Drone (UAS) attachment uses `/animal-attendances` for K9 handlers (per D4H lead-dev Dan Doyle's 2026-05-13 confirmation).

**Selective attendance mode (`fullTeam: false`)** — `POST /v3/team/{teamId}/incidents` includes `"fullTeam": False` so attendance starts EMPTY (no auto-staged REQUESTED records for every team member). Per-YES sync POSTs one ATTENDING record per responder. Pinned by CLAUDE.md Locked Decision; validated 2026-05-19 via `experiments/d4h/15_full_team_false_blank_attendance.py` A/B test. Omitting the field defaults to `true` server-side and re-introduces the async-init race + duplicate ATTENDING+REQUESTED rows + blank-name rendering bug.

**Per-YES attendance sync (`/d4h-sync-yes`):** Each YES response surfaced by the EB polling chain is dispatched as a single Cloud Tasks job to `POST /d4h-sync-yes` (queue `d4h-per-yes-sync`, `max_attempts=5`). The handler calls `POST /v3/team/{teamId}/attendance` with `{activityId, memberId, status: ATTENDING, startsAt, endsAt}`. Member lookup uses the EB-stored SAR email as the join key (see Identity model above). 5 retries covers transient D4H 5xx without burying a real outage — Cloud Tasks default of 100 retries would hide an outage for hours.

**Decline (NO) replies are not synced.** EB telemetry surfaces decline replies but the architectural decision (CLAUDE.md Locked Decision; backlog #442) is YES-only sync. Mapping NO → ABSENT would conflate "actively declined" with "phone off / hasn't read yet."

**Authentication:** D4H API v3 Bearer Token (Personal Access Token), stored in Secret Manager as `d4h-access-token`. Future improvement: least-privilege scope down + OAuth replacement when D4H ships it.

**Status visibility:** `GET /dispatch-status/{event_id}` returns the D4H-aware sync status (allowlist-only auth, not ownership-gated — enables team monitoring + shift handoff; CLAUDE.md Locked Decision).

#### Slack — Implementation Notes (Phase 1.8)

Slack was evaluated in February 2026 and implemented in Phase 1.8 Slacker. The integration is live — see the **Slack Integration** subsection above for the current architecture.

**Plan:** SCCSSAR holds IRS 501(c)(3) nonprofit status and qualifies for Slack for Nonprofits (free Pro plan via TechSoup). SCCSSAR-dev workspace is on the Pro plan.

**Rollout state:** `SLACK_MODE=full` on SCCSSAR dev (as of v1.10.0, July 2026) — real invites sent to all confirmed YES responders; Slack is the official incident coordination channel. The `_partition_arrivals_for_shadow()` 3-way classification helper is retained in the codebase for any future shadow-mode caller but is not invoked in full mode. Personal dev remains `SLACK_MODE=shadow` (hardcoded per CLAUDE.md Locked Decision).

**References:**
- [Slack for Good / Nonprofit program](https://slack.com/about/slack-for-good)
- [conversations.invite API method](https://docs.slack.dev/reference/methods/conversations.invite/)
- [chat.postMessage API method](https://api.slack.com/methods/chat.postMessage)

---

### 9. Authentication

Two distinct auth surfaces: how dispatchers authenticate to our app, and how our app authenticates to the external services it calls. All credentials are managed in GCP Secret Manager — see [§11 Secrets Management](#11-secrets-management) for the per-secret inventory.

#### How dispatchers authenticate to the app

Three layers, all must pass: OAuth consent screen (Testing-mode allowlist), server-side ID token verification, email allowlist. Full per-layer detail and rationale lives in [Security and IAM → Authentication Layers](#authentication-layers) below.

**Fail-fast at sign-in — `/verify` endpoint (PR #116):**
The frontend calls `GET /verify` immediately after Google OAuth completes, before the app UI is shown. The same `require_authorized_dispatcher` dependency used by `/ocr` runs — non-allowlist emails get HTTP 403 and the browser stays on the sign-in screen with a styled "Access denied" message. Prevents non-dispatchers from seeing the upload UI only to be blocked at submit. No Gemini, no Firestore, no file I/O in `/verify`.

The allowlist secret is `dispatch-authorized-emails`. Procedures to add/remove dispatchers: [docs/DEPLOYING.md](DEPLOYING.md) §9 "Add dispatchers".

#### How the app authenticates to external services

| Service | Auth model | Secret |
|---|---|---|
| **Everbridge** | Service account (HTTP Basic — `Authorization: Basic <base64 user:pass>`) — the SHO-SAR Dispatcher persona, group visibility granted out-of-band by the EB org admin | `everbridge-credentials` |
| **Slack** | Bot token (`xoxb-…`) installed by the workspace admin with scopes `channels:manage`, `groups:write`, `chat:write`, `users:read.email`, `pins:write`, `usergroups:read`. Workspace admin retains revoke capability. | `slack-bot-token` |
| **D4H** | API v3 Personal Access Token (Bearer). Generated by the D4H team Owner; future improvement is least-privilege scoping + OAuth replacement when D4H supports it. | `d4h-access-token` |
| **CalTopo** | HMAC-SHA256 signed requests against the CalTopo Team API. The signing secret is never sent over the wire — each request includes a per-request signature derived from the body + timestamp + path. Limits blast radius vs Bearer tokens. | `caltopo-team-id`, `caltopo-credential-id`, `caltopo-credential-secret` |
| **Google Maps Geocoding API** | API key restricted to the Geocoding API. Optional — the backend skips silently if absent. | `google-maps-api-key` |
| **US Census Geocoder / Open-Meteo Elevation** | Anonymous, keyless public APIs, used for the LPB environment line. Coordinates are rounded to 3 dp (~110 m) before either call, and their query parameters are redacted from logs. | n/a (keyless) |
| **Vertex AI (Gemini)** | Application Default Credentials via the Cloud Run runtime service account (`dispatch-runner`) — no API key. | n/a (ADC) |
| **Google Docs + Drive APIs** | Dispatcher's own OAuth access token (`drive.file` scope, ~1 hour TTL), forwarded in the POST body to `/create-doc`. No server-side credential. | n/a (per-request token) |
| **Cloud Tasks → Cloud Run polling endpoints** | OIDC token minted by the `everbridge-poll-sa` service account, validated by the endpoint (audience + token-email dual pin). | n/a (per-task OIDC) |

`terraform.tfvars`, `*.tfstate`, and `*.tfplan` files are gitignored — never committed.

---

### 10. Rate Limiting

Firestore-backed per-user rate limiter (`backend/rate_limit.py`) — `check_rate_limits(email)` applied as the first action on **every protected endpoint** (not just `/ocr`).

**Per-user windows (defaults, overridable via `OCR_RATE_LIMIT_*` env vars):**

| Window | Limit | Reset |
|---|---|---|
| Per minute | 5 requests | Rolling 60s |
| Per hour | 20 requests | Rolling 60min |
| Per day | 50 requests | UTC day |

**Global daily cap:** 200 requests/day across all users (env var `OCR_DAILY_GLOBAL_CAP`) — the hard ceiling protecting project budget. Excess returns HTTP 503 (reason hidden from caller); per-user exceeded returns HTTP 429 with `Retry-After`.

**Endpoints with rate-limit gating** (call `check_rate_limits(email)`): `/ocr`, `/create-map`, `/create-doc`, `/apply-staging-override`, `/everbridge-groups`, `/everbridge-contacts`, `/send-notification`, `/incident-status/{id}`, `/dispatch-status/{id}`, `/confirm-draft-sent/{id}`, `/close-incident-polling/{id}`. The OIDC-only Cloud Tasks endpoints (`/poll-incident`, `/d4h-sync-yes`, `/delete-template`) are NOT rate-limited — they're internal and queue-paced.

**Firestore collections used by the limiter:**
- `ocr_rate_limits` — per-user windows (doc ID = SHA-256 hash of email, truncated to 16 chars; never the raw email)
- `ocr_usage_limits` — global daily counter (single doc `"global"`)

Both are independent from the `incidents` collection (24h TTL, EB+Slack+D4H polling state) and from each other.

Rate limiting prevents runaway Vertex AI spend if a token is compromised, a user loops requests, or a misconfigured client hammers an endpoint.

---

### 11. Secrets Management

All credentials live in GCP Secret Manager and are injected into Cloud Run as environment variables via Terraform. [docs/DEPLOYING.md](DEPLOYING.md) §5 "Create the secrets" is the source-of-truth for setup, and [docs/OPERATIONS.md](OPERATIONS.md) §Secrets for rotation — this table is the architectural inventory.

| Secret name | Purpose | Phase introduced |
|-------------|---------|-----------------|
| `dispatch-authorized-emails` | Email allowlist for dispatcher access | 1.0 |
| `dispatch-google-client-id` | OAuth client ID injected into frontend at Docker build time | 1.0 |
| `caltopo-team-id` | CalTopo team identifier | 1.5q |
| `caltopo-credential-id` | CalTopo API credential ID | 1.5q |
| `caltopo-credential-secret` | CalTopo HMAC-SHA256 signing secret | 1.5q |
| `google-maps-api-key` | Google Maps Geocoding API key (Nominatim fallback) | 1.5z |
| `everbridge-credentials` | Everbridge service account `username:password` (base64) | 1.8 |
| `slack-bot-token` | Slack workspace bot OAuth token | 1.8 |
| `dispatch-safe-list` | JSON array of safe-listed dispatcher records (EB+Slack rollout guardrail) | 1.8 |
| `slack-so-coordinator-email` | SAR coordinator (Sheriff's Office) email — Slack invite target. Documented exception to the "always use @sccssar.org for Slack lookup" rule, since the SO coordinator is on the sheriff-department domain. | 1.8 |
| `d4h-access-token` | D4H API v3 Personal Access Token (Bearer) — incident auto-create + per-YES attendance sync. Per-env. Future improvement: least-privilege scope down + OAuth replacement when D4H supports it. | 1.8 (Phase 2) |

**Non-secret env vars** (set via Terraform `terraform.tfvars`, not Secret Manager): `EVERBRIDGE_MODE` (off/safe/full), `SLACK_MODE` (off/shadow/full), `ACTIVE_INCIDENTS_CHANNEL_ID` (the `#active-incidents` tally channel), `ACTIVE_INCIDENT_MGMT_USERGROUP_ID` (the `@active_incident_management` Slack user group whose members are pre-added to every incident channel — issue #579), `PROJECT_ID`, `CLOUD_RUN_SERVICE_URL`, `GEMINI_MODEL`, `LOG_LEVEL`, `ALLOWED_ORIGINS`, `OCR_RATE_LIMIT_*`, `MAX_UPLOAD_SIZE_MB`. **`DISPATCH_TURBO_TEST_LABEL`** (optional; set via `gcloud run services update --update-env-vars` rather than Terraform for now): when non-empty, prepends a test-label prefix to Dispatch Turbo DM text so pilot test messages are labeled before they reach real team members.

`terraform.tfvars`, `*.tfstate`, and `*.tfplan` files are gitignored. Never commit them.

#### Expiration monitoring (rotation health)

To prevent surprise credential expirations mid-incident, every monitored secret carries `rotated` (YYYY-MM-DD) and `period_days` labels in Secret Manager. The labels are set atomically by `bin/rotate-secret.sh <secret-name> <data-file> <period-days>` so the rotation date never drifts from the version — calling `gcloud secrets versions add` directly leaves the labels stale and is wrong.

**Backend** — `backend/secret_health.py` reads the labels at runtime, computes days-until-expiry per secret, and returns a `{secret_name: {days_remaining, period_days, rotated, warn}}` map. `warn=True` when `days_remaining <= _WARN_DAYS` (currently 30). The `/version` endpoint exposes this map as `token_health` so authenticated callers can inspect rotation state without Secret Manager IAM.

**Frontend** — `frontend/index.html` reads `token_health` from `/version` and shows a sticky yellow banner to the dispatcher when any monitored secret is within the warning window. The banner names the affected secret and the days remaining so the dispatcher can flag the rotation owner. `days_remaining=null` (no rotation labels yet — never rotated via the wrapper) is a silent state, not a warning.

**Monitored set** (`_MONITORED_SECRETS` registry in `secret_health.py`):

| Secret | Period | Status |
|---|---|---|
| `d4h-access-token` | 365 days | ✅ actively monitored |
| `everbridge-credentials` | 180 days (planned) | Registry entry commented — uncomment when EB rotation is operationalized |
| `slack-bot-token` | 365 days (planned) | Registry entry commented — uncomment when Slack rotation is operationalized |

**Planned (aspirational):** Slack-channel notification (e.g., to a `#secrets-rotation` admin channel) when a secret crosses the warning threshold — currently dispatcher-banner-only. Goal: surface expirations days before any dispatcher experiences an unexpected auth failure mid-incident, and reach the rotation owner even if no dispatch is in flight when the threshold is crossed.

---

### 11b. Cloud Tasks (Phase 1.8)

Cloud Tasks drives the EB+Slack polling loop. Two task types are scheduled, both targeted at Cloud Run endpoints with OIDC tokens (not dispatcher tokens).

| Property | Value |
|----------|-------|
| Queue name | `everbridge-poll-queue` |
| Region | `us-central1` |
| Max dispatches | 5 / second |
| Max concurrent | 10 |
| Retry max attempts | 3 |
| Retry backoff | 5 s → 10 s → 20 s (2× doublings) |
| Service account | `everbridge-poll-sa` (Cloud Run Invoker on `dispatch-console`) |
| Created by | Terraform — no manual setup |

| Task | Endpoint | When scheduled | Purpose |
|------|----------|----------------|---------|
| Poll cycle | `POST /poll-incident/{event_id}` | After `/send-notification`; re-enqueues itself while `polling_active=true` on Firestore | Read EB notification responder ACKs, diff against last known, post to Slack channel + tally |
| Template delete | `POST /delete-template/{template_id}` | +30 minutes after draft template creation (safe mode only) | Unconditional cleanup of EB template after manual-confirm window expires |

After 3 failed retries a task is dead-lettered and polling stops silently. See [docs/OPERATIONS.md](OPERATIONS.md) §"When something goes wrong" for diagnostic steps.

---

### 12. Infrastructure as Code (Terraform)

Three Terraform environments mirror the three GCP projects:

| Environment | Directory | GCP Project | Role |
|-------------|-----------|-------------|------|
| SCCSSAR Dev | `terraform/environments/sccssar-dev/` | `sar-dispatch-sccssar-dev` | Active development — all backlog work |
| Personal Dev | `terraform/environments/dev/` | `sar-dispatch-dev` | Dispatcher-facing stable — leave alone |
| Production | `terraform/environments/prod/` | `sar-dispatch-prod-20260218` | Production (not yet active) |

**State backend — GCS (per-environment, NOT local):** Each environment's `main.tf` declares `backend "gcs"` pointing at a per-project bucket (`<project-id>-tfstate`) with the prefix `dispatch-console`. Local `*.tfstate` files are gitignored. Buckets have versioning + uniform bucket-level access + public-access-prevention enforced (rationale: PR-K + PR-K2 / security review finding #18 — CLAUDE.md Locked Decision "Terraform state backend"). Created out-of-band via `gcloud storage buckets create` before `terraform init`.

**Switching environments — shell helpers** (paste into `~/.zshrc` or `~/.bashrc`):

```bash
tf-personal-dev() {
  gcloud config configurations activate personal-dev || return 1
  export GOOGLE_APPLICATION_CREDENTIALS=~/.config/gcloud/adc-personal-dev.json
  echo "[tf] active: $(gcloud config get-value account) → $(gcloud config get-value project)"
}

tf-sccssar-dev() {
  gcloud config configurations activate sccssar-dev || return 1
  export GOOGLE_APPLICATION_CREDENTIALS=~/.config/gcloud/adc-sccssar-dev.json
  echo "[tf] active: $(gcloud config get-value account) → $(gcloud config get-value project)"
}

tf-refresh() {
  # When ADC tokens expire — browser pops, cp into the per-profile ADC file
  gcloud auth application-default login
  local env=$(gcloud config configurations list --filter='is_active=true' --format='value(name)')
  cp ~/.config/gcloud/application_default_credentials.json \
     ~/.config/gcloud/adc-${env}.json
}
```

The per-profile ADC file pattern (vs the central one) is required because `GOOGLE_APPLICATION_CREDENTIALS` must point at a per-env file — `gcloud config set account` alone doesn't update ADC. Recovering a diverged state file is an operator procedure, not an architectural one.

**Important:** When new environment variables are added to `main.tf`, `terraform apply` must be run **separately** from the build scripts. `gcloud run deploy` (used in `build-sccssar-dev.sh` and `build-dev.sh`) redeploys the container image but preserves the existing Cloud Run service configuration — it does not pick up new env var blocks from Terraform. Always `terraform apply` first, then build. CalTopo secrets must exist in Secret Manager before `terraform apply` creates the Cloud Run service — service creation validates secret existence.

Never deploy directly to production without testing in dev first.

---

## Request Lifecycle

A complete form submission, step by step. **Typical total time from photo upload to result display: 45–60 seconds on a warm instance; 65–90 seconds if a cold start is involved.** See [Performance Considerations](#performance-considerations) for a breakdown by operation.

1. **Sign-in** — Dispatcher clicks "Sign in with Google." GSI library completes the OAuth flow and returns a Google ID token to the browser. The frontend immediately calls `GET /verify` — if the email is not in the allowlist, the browser stays on the sign-in screen with an "Access denied" message and the app UI is never shown. If authorized, the app UI is shown and the token (valid for ~1 hour) is stored for subsequent requests.

2. **Upload** — Dispatcher selects a JPEG photo or a digitally-filled PDF and clicks Submit. Frontend sends `multipart/form-data` POST to `/ocr` with `Authorization: Bearer {id_token}`.

3. **Token verification** — Backend verifies the Google ID token: cryptographic signature (using Google's public keys), `aud` matches our OAuth client ID, `iss` is Google, `exp` is in the future, `email_verified` is true.

4. **Allowlist check** — Token's email is checked against `AUTHORIZED_EMAILS`. If not found, returns HTTP 403.

5. **Format detection** — Magic bytes checked: `%PDF` → PDF path; `FF D8 FF` → JPEG path. Unknown formats rejected before any processing.

6. **Rate limit check** — Firestore is queried for this user's hourly request count and the global daily count. If either limit is exceeded, returns HTTP 429.

**JPEG path (handwritten photo):**

7a. **Image validation** — File size ≤10MB. Dimensions between 100×100 and 8000×8000 pixels. Invalid files are rejected before any AI call.

8a. **Pass 1 — Gemini OCR** — Image bytes and the structured extraction prompt (`SYSTEM_PROMPT`) are sent to Gemini 2.5 Flash via Vertex AI. Gemini extracts all form fields including Last Known Position. Address verification and spelling correction happen here. `finish_reason` is checked — if `MAX_TOKENS`, raises an error immediately (HTTP 502) rather than returning a truncated response. Typical latency: 8–15 seconds.

9a. **Geocoding** — Same as PDF path (see step 7b below).

10a. **Staging POI query** — Same as PDF path (see step 9b below).

11a. **Pass 2 — Gemini formatting** — A second Gemini call formats the full structured output using `SYSTEM_PROMPT`. The prompt includes the verified LKP coordinates (`__LKP_COORDS__`) and the real OSM POI candidates (`__STAGING_CANDIDATES__`) injected as INTERNAL context. Gemini selects staging candidates top-to-bottom (no reordering) and formats the complete output including the LPB Range Ring Analysis. `finish_reason` checked again; HTTP 502 on `MAX_TOKENS`. Typical latency: 10–20 seconds.

**PDF path (v2 digitally-filled form):**

7b. **AcroForm extraction** — `pdf_extract.py` reads AcroForm field values directly from the PDF using pymupdf. No image rendering, no OCR. Checkbox answers (Q1–Q12), MP name, DOB, dates, and all other form fields are read deterministically from the AcroForm overlay. `build_synthetic_summary()` formats them into the same labeled-field text format as Gemini Pass 1 output. Typical latency: <1 second.

8b. **Geocoding** — Backend sends the extracted LKP address to Nominatim (apartment/unit qualifiers stripped first). City/state cascade applied if needed. If Nominatim returns no result, falls back to the Google Maps Geocoding API (`_geocode_google_maps()`), which can resolve misspelled street names (e.g., "TRADEN" → "TRADAN"). Google Maps spelling correction is propagated throughout the summary text via server-side post-processing. Also geocodes the Residence address in parallel (with the same qualifier stripping and fallback logic). Returns `lat,lng` for both. Typical latency: 1–3 seconds.

9b. **Staging POI query** — Backend queries the active staging source (Geoapify on both live envs, Overpass as fallback — see §5) for points of interest within 1200m of the geocoded LKP coordinates. On zero candidates, retries once at 4828m (3.00 mi) and appends a widened-search note to the Event Log. Tier-sorts results by `(tier, distance)`. Mirror fallback chain applied if needed. Returns up to 12 candidates. Typical latency: <1s on Geoapify, 1–3s on Overpass (see §12); a widened retry adds one more source call.

10b. **PDF Gemini call — staging + Koester** — A single text-only Gemini call (`STAGING_KOESTER_PROMPT` + `extract_staging_and_koester()`) formats the staging recommendations and LPB Range Ring Analysis. No image is sent. `max_output_tokens=16384` required — staging candidate text + full Koester analysis exceeds 8192 tokens. `finish_reason` checked; HTTP 502 on `MAX_TOKENS`. Typical latency: 10–20 seconds.

**(Both paths converge here)**

11. **Post-processing pipeline** — Backend applies a series of deterministic fixes to the assembled output (see [Server-Side Post-Processing Pipeline](#server-side-post-processing-pipeline) below).

12. **map_data assembly** — Backend builds the structured `map_data` dict from parsed staging locations and Koester ring radii, for use by the `/create-map` endpoint.

13. **LKP_LL appended** — Backend appends `LKP_LL: lat,lng` on its own line at the end of the text response. This is machine-readable only (legacy — the frontend also uses `map_data.lkp` from the JSON).

14. **Response returned** — `JSONResponse({"text": str, "map_data": dict})` sent to browser.

15. **Display** — Frontend parses `data.text` into the editable textarea (stripping the `LKP_LL:` line before display), stores `data.map_data` for the Create Incident Map button, and shows the amber checkbox warning banner.

16. **Dispatcher review** — Dispatcher reads through the extracted text, compares checkboxes against the original photo, and makes any corrections in the textarea.

17. **Dispatch** — Dispatcher uses the action buttons (in workflow order — Map first so the Slack welcome message includes the CalTopo URL):
    - **Create Incident Map** — POSTs `map_data` to `/create-map`; on success, opens the new CalTopo map URL in a new tab and writes the URL back to the `CalTopo Map ID:` field in the textarea
    - **Everbridge + Slack + D4H** — Phase 1.8 automated dispatch (single combined button). Frontend calls `/everbridge-groups` (group typeahead) and `/everbridge-contacts` (auto-fill OCEAN# from dispatcher's `externalId`) on load. On Send: POSTs to `/send-notification` with selected groups + body. Backend orchestrates: EB notification event → live send (full mode) or draft template (safe mode) → Slack channel creation + welcome message + CalTopo follow-up + #active-incidents tally → D4H incident record auto-created (pre-filled with intake form data + K9/UAS tabs if selected) → Firestore `incidents` doc → Cloud Tasks first poll task. Polling cycles re-enqueue themselves; each YES surfaced by EB dispatches a `/d4h-sync-yes` Cloud Tasks job (`max_attempts=5`) and posts a diff to the Slack channel. Safe-mode draft path requires dispatcher to send the draft from EB UI then call `/confirm-draft-sent/{event_id}` with the notification ID.
    - **View in D4H** (helper) — opens the team D4H incidents page for manual reference; the auto-create + per-YES sync runs server-side regardless of this click
    - **Google Doc** — requests `drive.file` OAuth token via `initTokenClient()` (first use: consent dialog; subsequent: silent ~1 s), POSTs textarea content to `/create-doc`, opens the created Google Doc URL in a new tab; subsequent clicks open the same doc without re-creating; doc is automatically shared (writer access) with all authorized dispatchers
    - **Google Maps** (2nd row) — opens Google Maps centered on LKP address (reference tool)

18. **Cleanup** — The image bytes are garbage-collected after Pass 1. Nothing is persisted server-side. The dispatcher must copy the extracted text before closing the browser if they want a record.

---

## Data Flow and Privacy

**Platform note:** All Google Cloud services used in this application — Cloud Run, Vertex AI (Gemini), Firestore, Secret Manager, and Cloud Logging — operate within the same Google Cloud business account and are governed by Google Cloud's enterprise Terms of Service and Data Processing Addendum. Vertex AI processes data under Google's enterprise privacy commitments and does not use customer data to train its models.

### What data goes where

| Data | Stored? | Services it reaches |
|------|---------|-------------------|
| **Form image (JPEG)** | Never | Vertex AI (Gemini) only, in-memory during Pass 1. The raw photo — including any visible handwriting showing the missing person's name, description, and location — is transmitted to Gemini for OCR. Garbage-collected immediately after Pass 1 completes. |
| **Missing person name, DOB, age** | Never | Gemini (Pass 1 extraction). Nominatim: never (only address sent). Staging source (Geoapify / Overpass): never (only coordinates sent). CalTopo: yes — included in the Residence marker label and description (e.g., "Jane Doe, DOB 01/01/1980") so field teams can identify the marker on the map. Cloud Logging: never. |
| **Last Known Position address** | Never | Gemini (extracted in Pass 1). Nominatim (geocoding query — full street address sent). Google Maps Geocoding API (fallback geocoder — same street address, sent only when Nominatim returns no result). Staging source (Geoapify / Overpass): never (only the resulting lat/lng is sent). CalTopo: yes — in the LKP marker label. Cloud Logging: neighborhood/area name only (never full address). |
| **Residence address** | Never | Gemini (extracted in Pass 1). Nominatim (geocoding query — street address without apartment number). Google Maps Geocoding API (fallback — same conditions as LKP). CalTopo: yes — in the Residence marker label. Cloud Logging: never. |
| **At-risk factors, medical history** | Never | Gemini (Pass 1 and Pass 2 — used to determine Koester subject category). No other services receive this. Cloud Logging: never. |
| **Staging recommendations** | Never | Gemini (Pass 2 — formatted). CalTopo: yes — staging marker labels and descriptions. Slack: the first staging entry is posted as its own pinned message, carrying Apple and Google Maps links. |
| **LKP coordinates (lat/lng)** | Never | Staging source — Geoapify Places API and/or Overpass (query center point only — no address or name). US Census Geocoder (every dispatch) and Open-Meteo Elevation (only when classified rural), both **rounded to 3 dp (~110 m)**, for the LPB environment line. CalTopo (map centering and markers). Frontend (parsed for Google Maps link). Cloud Logging: yes — lat/lng logged at neighborhood granularity (e.g., `Geocoded LKP: 37.40, -121.88 (Berryessa, San Jose)`). Never full precision in logs. |
| **Google Doc content (textarea text — incident summary, staging, Koester)** | Yes — dispatcher's own Google Drive | Google Docs API (`documents.batchUpdate` — doc body). Google Drive API (`files.create` + `permissions.create` — file creation and sharing). Doc is created in the dispatcher's Drive under their account. Cloud Logging: doc ID only (not full URL, not content). Content includes MP name, LKP, residence, staging — not logged to Cloud Run. |
| **Everbridge notification body (MP info, staging, dispatcher OCEAN#)** | Yes — Everbridge tenant | Everbridge REST API (`POST /notifications/{org}` or `POST /notificationTemplates/{org}` in safe mode). Recipient SMS/email/voice paths managed by Everbridge per-contact policy. Subject to Everbridge enterprise terms. Cloud Logging: notification ID only — no body, no recipient list. |
| **Slack incident channel content (welcome line, MP name + age/gender/at-risk, staging links, CalTopo URL, responder confirmations)** | Yes — Slack workspace | Slack Web API (`conversations.create`, `conversations.invite`, `chat.postMessage`, `pins.add`). Channel is private; only invited members can read — the initial set (dispatcher + SO Coordinator + `@active_incident_management` group members) plus, in full mode, confirmed YES responders as they reply. Slack workspace is on the Pro plan via Slack for Nonprofits — message history is unlimited. Cloud Logging: channel ID + message timestamps only — no body. |
| **Incident polling state (Firestore `incidents` collection — Phase 1.8)** | Yes — 24 h TTL auto-delete | Per-incident Firestore doc keyed by EB `event_id`. Fields: `event_id`, `notification_id`, `polling_active`, `manual_stop_requested`, `manual_confirm_offered`, `responders` (dict of contact_id → last ACK status + timestamp), `contact_email_map` (snapshot at send time for shadow-mode partition), `slack_dm_sent_user_ids` (list of Slack user IDs that received a Dispatch Turbo DM — idempotency guard), `expire_at` (TTL field — 24 h after polling stops). Doc auto-deleted by Firestore TTL policy. Reads/writes by Cloud Run service account only. Used to drive Cloud Tasks polling cycles; never returned over `/ocr` response. |
| **EB+Slack safe-list (dispatcher names, contact IDs, emails)** | Yes — Secret Manager | `dispatch-safe-list` secret. JSON array of records with name + EB contact ID + email. Read at startup and on each `/send-notification` call (no caching beyond Cloud Run revision lifetime — see ops runbook for force-refresh). Used as send-time guardrail when `EVERBRIDGE_MODE=safe`. |
| **Rate limit counters** | Yes | Firestore `rate_limits` collection — request counts and timestamps only. No intake form data, no PII. |
| **Dispatcher email (auth)** | Yes | GCP Secret Manager (allowlist). Cloud Run (env var at startup). Cloud Logging: hashed identifier only. |
| **Cloud Run logs** | Yes | Operational events only (latency, error type, candidate counts, EB notification IDs, Slack channel IDs). No names, DOBs, addresses, case numbers, EB body content, Slack message body. See [Key Logs and Metrics](#key-logs-and-metrics). |

### Privacy design rationale

**Why minimal server-side storage:** SAR call-out forms contain PII and PHI (subject name, DOB, home address, medical history, mental health status). The OCR/geocoding pipeline holds this data in memory only — never written to disk or a database — so the largest blast radius (intake form data) has no server-side storage. The Phase 1.8 EB+Slack flow does write polling state to Firestore (`incidents` collection), but those documents are scoped to the EB `event_id`, never include the form image, and auto-delete after 24 hours via TTL. The 24h window serves three operational needs: drive Cloud Tasks polling without dispatcher action, give the D4H per-YES sync queue (`max_attempts=5`) its full retry budget so transient D4H 5xx are absorbed, and leave a replay window if D4H is briefly unavailable mid-incident. Cloud Run logs themselves carry no PII — enforced by `backend/test_pii_log_patterns.py` (Locked Decision: PII-pattern CI guard).

**Third-party services summary:** The third-party services that receive any incident data:
- **Vertex AI / Gemini** — receives the form image (Pass 1) and formatted address/coordinates (Pass 2). Subject to Google Cloud enterprise DPA.
- **Nominatim (OSM)** — receives street addresses for geocoding only (no names, no DOBs).
- **Google Maps Geocoding API** — receives street addresses (fallback when Nominatim fails). Subject to Google Maps Platform Terms. Only the LKP and Residence addresses are sent — no subject names, DOBs, or case numbers.
- **Geoapify Places API** — primary staging source; receives only GPS coordinates (no PII). Per-project API key in the `geoapify-api-key` secret.
- **Overpass API (OSM)** — staging fallback; receives only GPS coordinates (no PII).
- **CalTopo** — receives marker labels that include MP name, DOB, and addresses. Credentials are HMAC-SHA256 signed. Data is protected within the team's CalTopo account.
- **Google Docs / Drive APIs** — receives the full textarea content (incident summary, LPB questionnaire, staging, Koester analysis) when the dispatcher clicks "Google Doc". Data is stored in the dispatcher's own Google Drive under the dispatcher's Google account. Access token is short-lived (`drive.file` scope, ~1 hour), forwarded to the backend in the POST body. No service account; no server-side credential. Rate limiting applies via the shared Firestore limiter.
- **Everbridge** — receives the notification body (event name, MP info, staging, dispatcher OCEAN#) and recipient group selection. Subject to Everbridge enterprise terms (County of Santa Clara tenant). The `everbridge-credentials` service account is scoped to a single SHO-SAR Dispatcher persona with explicitly-granted group visibility.
- **Slack** — receives the welcome message body (MP name, age/gender/at-risk summary, staging links, CalTopo URL) and per-cycle responder updates. Channel is private; only invited members can read. Workspace is on Slack Pro via Slack for Nonprofits.

---

## Server-Side Post-Processing Pipeline

Applied to Gemini's Pass 2 output, in order, before returning the response. Each step is deterministic — it does not call AI. The philosophy: prompt engineering reduces error rate; server-side regex/normalization eliminates known residual errors deterministically. Both layers are required.

| Step | What it fixes | Phase introduced |
|------|--------------|-----------------|
| Markdown fence strip | Gemini sometimes wraps output in ` ```text ` blocks despite prompt prohibition | 1.5n Build 16 |
| UNCERTAIN label collapse | Raw `UNCERTAIN` template token leaking into checkbox output | 1.5n Build 16 |
| Parking en-dash normalization | `~5-10` (hyphen) → `~5–10` (en-dash) to match SAR formatting standard | 1.5n Build 16 |
| Event Name reconstruction | Prevents OCR truncation of event name; server reconstructs from parsed date + agency + LKP street name (strips apt/unit qualifier, then cardinal direction, then street type suffix) | 1.5n Build 16 (apt strip added 1.5x) |
| Cardoza Park normalization | Gemini persistently misreads handwritten `CARDOZA` as `Carozza`/`Carocza`/`Carooza`; regex `r"\bCar[ao]z+a\s+Park\b"` → `"Cardoza Park"` catches all variants | 1.5o |
| Staging Area for Resources pass-through | Officer's handwritten RP is copied verbatim from the form with verified spelling; if blank on form, remains blank | 1.5o |
| Address-less staging line filter + renumber | PASS 2: drops entries with no `" — "` separator; drops entries where `loc_part` has no leading digit and is not a park (e.g., "Milpitas Unified School District — School"); renumbers 1–7, discards beyond 7. PASS 3: appends officer-designated staging if not already present (can push total to 8 — this is correct). | 1.5n Build 17; extended PR #250 |
| Park address strip | Strips street address from park/open-space staging entries; parks display as `"Park Name, City"` only | 1.5o |
| Officer staging labeling (issue #244) | Officer staging is labeled in-place if it matches a recommendation, or appended last as "not among top recommendations" if it doesn't. LKP/Residence no longer appear in staging list. Supersedes the pre-#244 officer-auto-#1 and LKP-entry-2 behavior. | 1.5p → redesigned issue #244 |
| Gemini exclusion note strip | Gemini adding "Note: N location(s) excluded…" commentary that is inaccurate after server-side filtering | 1.5n Build 21 |
| LPB em-dash normalization | ` - ` (hyphen with spaces) → ` — ` (em-dash) in the Q-section only, matching SOP format | 1.5n Build 19 |
| Q4 ISO date revert | Gemini over-formatting MUPS date from `1/8/26` to `2026-01-08`; scoped to Q4 line only | 1.5n Build 19 |
| Dispatcher name injection | Auto-populates the `Dispatcher:` field with the last name extracted from the dispatcher's Google ID token | 1.5n Build 20 |
| Google Maps spelling correction propagation | When Google Maps Geocoding API corrects a street name (e.g., `TRADEN` → `TRADAN`), the corrected spelling is substituted throughout the summary text (LKP, Event Name, Event Log, staging entries). Applied after summary assembly so all occurrences are consistent. | 1.5z (PRs #180/#181) |

---

## Koester LPB Range Ring Analysis

The LPB (Lost Person Behavior) Range Ring Analysis appears as the final section in the dispatcher's output. It uses Robert Koester's published *Lost Person Behavior* methodology to estimate how far the missing person may have traveled from the Last Known Position.

> **Note (Phase 1.5z2):** Range rings are no longer drawn on the CalTopo map. The Koester percentile distances appear as **text only** in the Full Incident Summary section of the dispatcher textarea — for search-management planning use. The `_add_ring()` helper was removed from `backend/caltopo.py` after dispatcher feedback that the rings cluttered the tactical map for field teams. See [docs/design-decisions.md](design-decisions.md) → "CalTopo / Range rings removed (issue #244)".

### How it works

**This is a hardcoded lookup table, not a live database query.** The percentile distances for each Koester subject category are pre-populated in the Gemini prompt (`SYSTEM_PROMPT` in `backend/gemini.py`) as a structured reference table. Gemini does not look up these values externally — it reads them from the injected table in the prompt and outputs the correct values for the assigned category.

**Subject category assignment:** Gemini determines the subject category from the intake form data:

- Age (primary factor — e.g., Child 1–3, Child 4–6, Child 7–9, Child 10–12, Hiker, Despondent, Dementia/Alzheimer's, etc.)
- At-risk factors from the form (e.g., Alzheimer's diagnosis → Alzheimer's/Dementia category)
- Activity and context (e.g., hiking trail vs. urban street)

**Important age boundary:** Koester's Child categories max out at age 12. A 13-year-old subject is assigned to `Hiker` (the closest behavioral analog), NOT `Child 10-12`. Using Child 10-12 for a 13-year-old produces incorrect search distances.

**Percentile distances:** For the assigned category, Gemini outputs the 25th, 50th, and 75th percentile search radii in **miles-first** format (`X.X mi (X.X km)`). This matches how SCCSSAR field teams communicate search radius over radio.

**Local modifiers:** Gemini appends a constrained set of local modifiers (terrain type, road density, etc.) using a fixed vocabulary list. Free-form adjectives are not permitted — this prevents phrasing variation between runs.

### Where the data flows

1. **Gemini output** — The LPB section appears in the formatted text: category name, percentile distances with Koester labels, local modifiers. JPEG path: Gemini Pass 2. PDF path: `extract_staging_and_koester()` text-only call.

2. **Dispatcher textarea** — The Koester analysis is included in the summary textarea. Search managers use it for planning; it is not part of the Slack welcome message. (The two-section textarea layout it once sat in was retired in #614 — the textarea is now a single full summary.)

3. **CalTopo** — Not drawn on the map. The `_add_ring()` helper was removed in Phase 1.5z2 (issue #244). `map_data` no longer contains a `rings` key.

### What it does NOT do

- Does not query Koester's database or any external search-statistics service
- Does not adjust distances based on terrain, weather, or time elapsed — local modifiers are informational labels, not calculation inputs
- Does not calculate anything dynamically — it reads pre-defined values from the lookup table in the prompt
- **Does not draw rings on the CalTopo map** — removed Phase 1.5z2 by dispatcher request

---

## GCP Services and Configuration

### Services

| Service | Purpose | Key Config |
|---------|---------|-----------|
| Cloud Run gen2 | Backend hosting | Service: `dispatch-console`, region: `us-central1`, scales to zero |
| Vertex AI | Gemini 2.5 Flash inference | Region: `us-central1`, temperature=0.1, max_output_tokens=32768 (JPEG), 16384 (PDF) |
| Firestore | Rate limiting state + EB+Slack incidents | Collections: `rate_limits` (Phase 1.0), `incidents` (Phase 1.8 — 24 h TTL on `expire_at` for PII boundary) |
| Cloud Tasks | EB+Slack polling scheduler | Queue: `everbridge-poll-queue`, region: `us-central1`, OIDC auth via `everbridge-poll-sa`. See [§11b Cloud Tasks](#11b-cloud-tasks-phase-18) |
| Secret Manager | Credentials, allowlist, safe-list | See [§11 Secrets Management](#11-secrets-management) for the full secret inventory |
| Artifact Registry | Docker image storage | `us-central1-docker.pkg.dev/sar-dispatch-sccssar-dev/dispatch-console/` (SCCSSAR dev); `us-central1-docker.pkg.dev/sar-dispatch-dev/dispatch-console/` (personal dev) |
| Cloud Logging | Operational logs | No PII; operational events only |
| Google Maps Geocoding API | Fallback geocoder when Nominatim returns no result | Billed per request (~$5/1000 calls); key stored in `google-maps-api-key` secret; invoked only on Nominatim miss |

### GCP Projects

| Environment | Project ID | Project Number | Role |
|-------------|-----------|----------------|------|
| SCCSSAR Dev | `sar-dispatch-sccssar-dev` | `1010784158087` | Active development — all backlog work |
| Personal Dev | `sar-dispatch-dev` | `970461953836` | Dispatcher-facing stable — leave alone |
| Production | `sar-dispatch-prod-20260218` | `7211363548` | Production (not yet active) |

Default region: `us-central1`. When running `gcloud` commands, target SCCSSAR dev (`sar-dispatch-sccssar-dev`) for all development work. Do not modify personal dev unless explicitly rolling back or adding a dispatcher.

### Cloud Run Configuration

| Setting | Value | Notes |
|---------|-------|-------|
| Service name | `dispatch-console` | |
| Region | `us-central1` | |
| Image (SCCSSAR dev) | `us-central1-docker.pkg.dev/sar-dispatch-sccssar-dev/dispatch-console/dispatch-console:latest` | Active development |
| Image (personal dev) | `us-central1-docker.pkg.dev/sar-dispatch-dev/dispatch-console/dispatch-console:latest` | Dispatcher-facing stable |
| Auth | `allUsers` invoker | Org policy `restoreDefault` override applied at project level |
| Min instances | `0` | Scales to zero when idle; cold start ~20–40s |
| Startup probe | `initial_delay=10s`, `failure_threshold=8` | Handles 20–40s cold start |
| Liveness probe | `period=60s`, `timeout=10s`, `failure_threshold=5` | Long period prevents probe kills during Gemini calls (Pass 1 + Pass 2 can take 25–40s) |
| Generation | gen2 | Required for VPC egress and longer request timeouts |

---

## Security and IAM

### Authentication Layers

**Layer 1 — Google OAuth consent screen**

Controls who can even attempt a sign-in. Settings that matter:

- **User type: External** — allows non-Workspace Google accounts (e.g., personal Gmail). If set to Internal, only accounts in the developer's Workspace org can sign in.
- **Publishing status: Testing** — only explicitly listed test users can complete the OAuth flow. Maximum 100 test users. No Google review required.
- **Path to expand:** Click "Publish app" in GCP Console → Google Auth Platform → Audience. Since the app only uses `openid` and `email` scopes, Google review is typically fast (hours to days). After publishing, any verified Google account can reach the OAuth flow — the backend allowlist still gates API access.

**Layer 2 — Server-side ID token verification**

Every request to every endpoint (except `/health`) verifies:
- Cryptographic signature (using Google's rotating public keys)
- `aud` claim matches the OAuth client ID: `970461953836-9urcevpqb18q4v6quf06rr398br42mqp.apps.googleusercontent.com`
- `iss` claim is `accounts.google.com`
- `exp` claim is in the future (token not expired)
- `email_verified` is `true`

**Layer 3 — Email allowlist**

Passed as `AUTHORIZED_EMAILS` env var (loaded from Secret Manager at startup). Even a valid Google ID token is rejected if the email is not on the list.

**Why `allUsers` invoker + application-layer auth (not Cloud IAM auth):**

Cloud IAM authentication at the Cloud Run level requires callers to have a GCP service account or Workload Identity — not practical for volunteer dispatchers using personal Google accounts. GSI + server-side ID token verification gives real Google identity verification with no service account management overhead.

### Security Headers

Applied to every HTTP response:

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin
X-Permitted-Cross-Domain-Policies: none
Server: (suppressed)
```

### Rate Limiting Threat Model

The rate limiter (20 requests/user/hour) exists primarily to cap Vertex AI spend if a valid token is compromised or shared. It is not an anti-abuse layer — the allowlist is the anti-abuse layer.

### Image Validation

JPEG magic bytes (`FF D8 FF`) are checked before any processing. Size (≤10MB) and dimension bounds (100×100 min / 8000×8000 max) are enforced. This prevents malformed or oversized files from reaching the AI call.

### IAM Roles

| Role | Granted to | Why |
|------|-----------|-----|
| Cloud Run Invoker (`run.routes.invoke`) | `allUsers` | Allows unauthenticated HTTP access to the service endpoint. Identity verification is done in application code via GSI tokens. |
| Vertex AI User | Cloud Run service account | Allows the backend to call Gemini via Vertex AI. |
| Firestore User | Cloud Run service account | Allows rate limit reads/writes to Firestore. |
| Secret Manager Secret Accessor | Cloud Run service account | Allows reading `dispatch-authorized-emails` and CalTopo credentials at startup. |
| Artifact Registry Reader | Cloud Run service account | Allows Cloud Run to pull the container image on deploy. |

---

## Output Schema

### `/ocr` Response

```json
{
  "text": "<formatted text for textarea>",
  "map_data": {
    "lkp":       {"lat": 37.40087, "lng": -121.88387, "label": "LKP — 1234 Main St, San Jose, CA"},
    "residence": {"lat": 37.40100, "lng": -121.88400, "label": "Residence — 5678 Oak Ave, San Jose, CA", "description": "Jane Doe, DOB 01/01/1980\nSubject's residence"},
    "staging":   [
      {"lat": 37.401, "lng": -121.882, "label": "Officer-designated staging location — ...", "type": "officer"},
      {"lat": 37.402, "lng": -121.884, "label": "CVS Pharmacy — 456 Oak Ave", "type": "alternate"}
    ],
    "event_name": "2026-02-20 XXSO MAIN"
  }
}
```

`staging[].type` values: `"officer"` (uses `cp` CalTopo symbol), `"lkp"` (skipped — LKP has its own dedicated marker), `"alternate"` (uses `point` symbol). The `rings` key was removed in Phase 1.5z2 (issue #244) — the CalTopo `/create-map` endpoint no longer reads it.

### Gemini Output Sections (in order, inside `text`)

**Section 1 — Initial Incident Summary**

```
Event Name: [reconstructed server-side: YYYY-MM-DD AGENCY STREETNAME]
Event #: [from form]
Agency: [from form]
Contact: [name and phone]
Missing Person: [full name]
  At-Risk Factors: [list]
DOB: [date] (Age: [calculated])
Last Seen At: [date and time only]
Last Known Position: [verified street address]
Residence Address: [from form]
Last Seen Wearing: [description]
Staging Area for Resources: [officer's handwritten RP, or blank if not specified]
CalTopo Map ID: [blank — written back by frontend after Create Incident Map succeeds]
Dispatcher: [auto-filled: last name from Google ID token]
```

**Section 2 — Event Log**

Timestamped entries, one per line. Date format: `YYYY-MM-DD HH:MM`.

**Section 3 — LPB Questionnaire**

Q1–Q12, yes/no checkboxes. Format: `Q# - ANSWER - Question text`

Example: `Q1 - Yes - Familiar with area`

Answer appears before the question for at-a-glance scanning. If a checkbox is genuinely
ambiguous, outputs `NOT ANSWERED (flag for follow-up)`.

**Section 4 — Staging Area Recommendations**

```
1. Officer-designated staging location — [address or park name]. [details]

2. [address] — [name]. [distance from LKP]; Parking: ~N–M vehicles; Restrooms: Yes/Likely/No; Lighting: Well-lit/Moderate/Poor

3. Park Name, City. [distance from LKP]; Parking: ~N–M vehicles; ...
(up to 7 total entries: officer/LKP entries + up to 5–6 alternates)
```

`Staging Area for Resources:` immediately follows — officer's RP from form, or blank.

**Section 5 — LPB Range Ring Analysis**

Robert Koester Lost Person Behavior methodology. Names the subject category, cites
25th/50th/75th percentile distances in miles-first format (`X.X mi (X.X km)`), and lists
local modifiers using a constrained vocabulary.

### Machine-Readable Appended Line (legacy)

```
LKP_LL: 37.40087,-121.88387
```

Appended by the backend after Pass 2. Parsed by the frontend for the legacy Google Maps / CalTopo deep link. Stripped before display in the textarea. Never logged. (Superseded by `map_data.lkp` in the JSON response for the Create Incident Map workflow.)

### Firestore Rate Limit Documents

**Per-user counters — collection `ocr_rate_limits`:**
- Document ID: `_email_hash(email)` — SHA-256 of lowercase email, truncated to 16 hex chars (`backend/rate_limit.py`). Never the raw email.
- Fields are time-window-keyed counters, all integers:

| Field pattern | Window | Example |
|---|---|---|
| `m_{YYYY-MM-DDTHH:MM}` | Minute bucket | `m_2026-05-25T16:42` |
| `h_{YYYY-MM-DDTHH}` | Hour bucket | `h_2026-05-25T16` |
| `d_{YYYY-MM-DD}` | Day bucket | `d_2026-05-25` |

Each `check_rate_limits(email)` call atomically increments all three buckets in a single Firestore transaction. Old keys are not actively pruned — Firestore reads only the current windows.

**Global daily cap — collection `ocr_usage_limits`:**
- Document ID: literal `"global"` (single doc)
- Fields: `day` (string, `YYYY-MM-DD`), `total` (int — incremented per request, reset when `day` rolls over)
- Checked before the per-user check so a project-budget DoS doesn't waste per-user transaction work.

---

## Performance Considerations

| Operation | Typical latency | Notes |
|-----------|----------------|-------|
| Cold start | 20–40s | Cloud Run scales to zero when idle (`min_instances=0`). Startup probe (`initial_delay=10s`, `failure_threshold=8`) handles this. First dispatch after a long idle period will feel slow. |
| AcroForm extraction (PDF path) | <1s | Direct pymupdf field read — no image, no AI. |
| Pass 1 Gemini OCR (JPEG path) | 8–15s | Scales with image size and form complexity. |
| Nominatim geocode (LKP + Residence) | 1–3s | Free tier, 1 req/sec limit. Two calls per form (LKP + Residence), run in sequence. |
| Google Maps geocode (fallback) | 1–2s | Only called when Nominatim returns no result. Typically 0 calls per form on well-formed addresses. |
| Geoapify POI query (primary) | ~0.5–1.1s | Two concurrent category calls. Primary staging source on both live envs; ~half the Overpass path. |
| Overpass POI query (fallback) | 1–3s typical, p99 ≤ 12s | 12s timeout per mirror (cut from 18s in #551 based on 30d log analysis; max observed success was 11.92s). Worst case with both mirrors: ~24s. Reached only when Geoapify fails (sequential) or unset (default). |
| Staging + Koester Gemini call (PDF path) | 10–20s | Text-only. `max_output_tokens=16384` — staging candidate text + full Koester analysis routinely exceeds 8192. |
| Pass 2 Gemini format (JPEG path) | 10–20s | `max_output_tokens=32768` — raised from 16384 after confirmed MAX_TOKENS truncation on Torres form (PR #192). Scales with staging candidates and LPB detail. |
| Firestore rate check | 50–200ms | Negligible. |
| **Total typical (JPEG)** | **~45–60s** | End-to-end from upload to result display. |
| **Total typical (PDF)** | **~15–30s** | AcroForm read + geocode + Overpass + 1 Gemini call. Significantly faster than JPEG. |

**Cold start consideration:** At `min_instances=0`, the first dispatch after an idle period adds 20–40 seconds. This is acceptable at current SAR dispatch volumes (~5–10 dispatches/day). Setting `min_instances=1` would keep an instance warm at ~$20/month with no cold start delay. The team has not yet decided whether cold-start latency is operationally unacceptable — see backlog in `CLAUDE.md`.

**Scale-to-zero idle timeout:** Google does not publish a specific guaranteed idle timeout for Cloud Run — the duration after which an idle instance is terminated is managed by Google's infrastructure, varies by load, and is not user-configurable. Community reports suggest 5–15 minutes, but this is not a contractual number and may change. The practical implication: after a period of team inactivity (overnight, between activations), the next form submission will always incur a cold start. There is no way to predict exactly when the instance was terminated.

**Image size impact:** Gemini token cost (and latency) scales with image size. A typical phone photo is 3–8MB, well within the 10MB cap. Dispatchers should avoid extreme zoom or raw DSLR files.

---

## Known Limitations and Design Decisions

**OCR is probabilistic on handwritten forms.** Gemini's checkbox accuracy on handwritten v1 JPEG forms is roughly 50–80% AND non-deterministic — the same form can produce different answers across runs. This is the accepted limitation that drove the **v2 PDF AcroForm** rollout (100% deterministic questionnaire accuracy via direct field-value extraction, see §3a). The amber checkbox warning banner prompts dispatchers to verify all 12 checkboxes on the JPEG path. Long-tail: the v1 JPEG path remains live for forms photographed in the field where a PDF isn't available; v2 PDFs are the strongly-preferred path for typed/fillable intake.

**Slack is now the official incident coordination channel** on SCCSSAR dev (`SLACK_MODE=full` since v1.10.0). The `wa.me` deep-link button was retired in #614 and is no longer in the UI. See the **WhatsApp Business API — Investigation Summary** under Section 8 for why the WhatsApp API path was evaluated and rejected (groups API is enterprise-only; individual broadcast adds significant overhead for marginal gain).

**Staging location selection varies slightly across runs.** Gemini selects from the same OSM candidate pool on every run but may select different subsets when candidates are close in tier/distance. All selections are real OSM locations — no hallucination. Variance in which 5–6 of 12 candidates are selected is acceptable.

**Intake form data is session-scoped.** The form image and extracted text live only in the browser tab and the in-flight API response — by design, never written to disk or a database. The dispatcher must copy the extracted text before closing the tab if they need a record. (Active-incident polling state IS persisted in Firestore under a 24h TTL — see §8 Identity model and Data Flow and Privacy for the separate flow.)

**Zip codes not used as geocoding signals.** Handwritten zip codes on SAR forms are frequently smudged or misread. Street name + city is more reliable. Zip codes in the form's address fields are ignored during geocoding.

---

## Key Logs and Metrics

All log events are operational only — no PII.

### Key Log Events

| Log message | When emitted | What it tells you |
|------------|-------------|-------------------|
| `OCR request received` | Start of `/ocr` handler | Includes `content_length` |
| `Geocoded LKP` | After Nominatim call | Includes `lat`, `lng`, area name (not street address) |
| `Overpass staging candidates` | After Overpass query | Includes candidate count and radius |
| `Overpass [mirror] returned HTTP [N]` | Mirror fallback events | Which mirror was tried and what it returned |
| `Staging source compare` | Every lookup when `STAGING_SHADOW=on` (personal-dev) | Both source counts, name/tier1 overlap, both latencies, both ok flags — PII-safe |
| `Staging sequential \| source=geoapify` | Every lookup when `STAGING_SHADOW=off` (sccssar-dev), primary OK | Geoapify count + `geoapify_ms` + radius — the only per-dispatch latency signal on a flipped env |
| `Staging fallback \| primary=geoapify backup=overpass` | Geoapify failed → Overpass used | Backup ok flag; means the primary source was unavailable — investigate the Geoapify key/quota |
| `Vertex AI call complete` | After each Gemini pass | Includes model name, `latency_ms` (per-pass, not end-to-end), finish_reason |
| `finish_reason=MAX_TOKENS` | When Gemini output is truncated | Raised as RuntimeError → HTTP 502; dispatcher sees retry message |
| `Dispatcher name injected` | After post-processing | Confirms current build is running |
| `Staging line dropped (no address, not a park)` | Post-processing filter | Diagnostic for address-less staging entries |
| `OCR request complete` | End of successful `/ocr` request | Includes `total_ms` — full wall-clock time from request receipt through Pass 1 + geocoding + Overpass + Pass 2 + post-processing + map_data assembly |
| `CalTopo map created` | End of successful `/create-map` request | Includes `total_ms` — full map creation time (map POST + all marker and ring POSTs to CalTopo API); also includes map URL |
| `Google Doc created` | End of successful `/create-doc` request | Includes doc ID only (not full URL, not content); confirms doc was created and shared with all authorized dispatchers |
| `Google Maps geocoding used` | When Nominatim returns no result and Google Maps fallback is invoked | Includes the address sent; logged at INFO level — no PII beyond what Nominatim already received |
| `Google Maps corrected street name` | When Google Maps returns a spelling correction (e.g., `TRADEN` → `TRADAN`) | Confirms correction was propagated through the summary text |
| `Rate limit exceeded` | When a user hits the cap | Includes user identifier (not email) |
| `Unauthorized` | Auth rejection | Token invalid, expired, or email not in allowlist |

### Useful gcloud Log Queries

```bash
GCLOUD=/opt/homebrew/share/google-cloud-sdk/bin/gcloud

# Recent logs (all severities)
$GCLOUD logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=dispatch-console" \
  --limit=20 --project sar-dispatch-dev

# End-to-end form ingestion time (Pass 1 + geocode + Overpass + Pass 2 + post-processing)
$GCLOUD logging read 'textPayload:"OCR request complete"' \
  --limit=20 --project sar-dispatch-dev \
  --format="table(timestamp, textPayload)"

# CalTopo map creation time (map + marker + ring POSTs to CalTopo API)
$GCLOUD logging read 'textPayload:"CalTopo map created"' \
  --limit=20 --project sar-dispatch-dev \
  --format="table(timestamp, textPayload)"

# Per-pass Gemini latency and finish_reason (two entries per form submission)
$GCLOUD logging read "textPayload:\"Vertex AI call complete\"" \
  --limit=10 --project sar-dispatch-dev

# MAX_TOKENS truncation events
$GCLOUD logging read "textPayload:\"finish_reason\"" \
  --limit=10 --project sar-dispatch-dev

# Auth rejections
$GCLOUD logging read "textPayload:Unauthorized" \
  --limit=10 --project sar-dispatch-dev

# Overpass mirror fallback events
$GCLOUD logging read "textPayload:Overpass" \
  --limit=10 --project sar-dispatch-dev

# Rate limit hits
$GCLOUD logging read "textPayload:\"Rate limit\"" \
  --limit=10 --project sar-dispatch-dev

# Geocoding failures
$GCLOUD logging read "textPayload:\"Nominatim geocoding failed\"" \
  --limit=10 --project sar-dispatch-dev

# Google Maps fallback geocoding events (Nominatim miss → Maps API invoked)
$GCLOUD logging read "textPayload:\"Google Maps\"" \
  --limit=10 --project sar-dispatch-sccssar-dev
```

---

## Cost Profile

### Monthly Dev Estimates

| Service | Estimated cost | Basis |
|---------|---------------|-------|
| Cloud Run | ~$0 | Free tier: 2M requests/month; <10 dispatches/day = ~300/month |
| Firestore | ~$0 | Rate limiting only — minimal reads/writes |
| Vertex AI (Gemini 2.5 Flash) | ~$0.10–0.50 per 10 form submissions | Two-pass = 2 Gemini calls per form |
| Artifact Registry | ~$0 | Small image, low egress |
| Nominatim / Overpass | $0 | Free OSM services (Overpass now staging fallback only) |
| Geoapify Places API | ~$0 at SCCSSAR volumes | Free tier; 2 calls (civic + commercial) per staging lookup. Per-project key in `geoapify-api-key`. |
| Google Maps Geocoding API | ~$0 at SCCSSAR volumes | See pricing note below |
| Google Docs API | $0 | No per-request billing. Quota: 300 req/min/project. ~2 API calls per button click. |
| Google Drive API | $0 | No per-request billing. Quota: 1000 req/100sec/user. ~5 `permissions.create` calls per click (one per dispatcher). Doc lives in dispatcher's Drive — no server storage quota. |
| **Total** | **< $5/month** | At current usage (~10 dispatches/day max) |

**Google Maps Geocoding API pricing:** $5.00 per 1,000 requests. Google provides a **$200/month free credit** (shared across all Maps Platform APIs per billing account). At SCCSSAR's operational volume (2–4 activations/month, each triggering at most 2–3 geocode calls — LKP, Residence, occasional Pass B alternate), the monthly call count is far below the free tier threshold. The credit would only be consumed if Nominatim *systematically* fails (e.g., an Overpass mirror issue isn't failing Nominatim, this would be unusual) or if development testing generates hundreds of calls in a month. Google Maps is only invoked when Nominatim fails for a given address, so well-formed addresses never incur a Maps API charge.

**Monitor with a billing alert** (see [docs/OPERATIONS.md](OPERATIONS.md) §Cost): set a $5 budget alert on the Google Maps API line item. Any unexpected spike would indicate either a runaway loop in the backend or an externally-visible geocoding endpoint being abused.

**Cold-start tradeoff:** Setting `min_instances=1` keeps one instance warm 24/7 and eliminates cold-start delays at a cost of approximately $20/month. At current usage this is not justified. Revisit if dispatchers consistently report the first dispatch of a shift feels too slow.

### When Each Service Starts Costing Money

| Service | When cost begins | Trigger level |
|---------|-----------------|---------------|
| Cloud Run | After 2M requests/month | Not realistic at SCCSSAR scale |
| Firestore | After ~1M reads/month | Not realistic — rate limiting only |
| Vertex AI (Gemini) | Every request — no free tier | First form submission ($0.005–$0.012/form) |
| Google Maps Geocoding | After $200/month in calls | ~40,000 geocode calls/month; not realistic at operational volume |
| Geoapify Places API | After the free-tier daily request quota | Not realistic — 2 calls per staging lookup, 2–4 activations/month |
| Nominatim / Overpass | Never — free OSM | N/A |

### Cost Controls

- **Rate limiting** (10 req/user/hour) — primary control on Vertex AI spend. A compromised token cannot generate thousands of Gemini calls.
- **Global daily cap** (Firestore-backed) — second layer above the per-user limit.
- **Scale to zero** — no idle compute cost when the service is not being used.
- **Gemini temperature** does not affect cost — billed on input/output tokens, not temperature setting.
- **Google Maps API key restriction** — key is restricted to "Geocoding API" only in GCP Console. If the key is ever leaked, the attacker can only incur geocoding charges (not Maps JavaScript, Places, etc.), and the $200 free credit provides an additional buffer.

---

## Artifact Retention Policies

Automated retention policies are in place for all build artifacts. No manual cleanup should be required under normal operations.

| Artifact | Policy | Mechanism |
|----------|--------|-----------|
| **GitHub branches** | Auto-deleted on PR merge | GitHub repo setting: Settings → General → Pull Requests → "Automatically delete head branches" (enabled Feb 2026) |
| **Artifact Registry images** | 2 most recent image versions kept; untagged layer digests deleted after 7 days | GCP cleanup policies on `dispatch-console` repository in both environments, codified in Terraform (PR #113, Feb 2026): `keep-last-2-versions` (keepCount=2) + `delete-untagged-after-7-days`. Applied to both `sar-dispatch-sccssar-dev` and `sar-dispatch-dev`. |
| **Cloud Run revisions** | 10 most recent revisions retained per deploy | Post-deploy cleanup step in `build-sccssar-dev.sh` and `build-dev.sh`: lists revisions, iterates all but the 10 most recent via `while IFS= read -r rev` loop (note: `xargs` does NOT work — `gcloud run revisions delete` only accepts one revision name at a time). Raised from 2 → 10 per Curt's callout to preserve a longer rollback window. Cloud Run has no native max-revisions deploy flag — the cleanup step is the mechanism. |

**Rationale:** During active development (Feb 2026), the `dispatch-console` service accumulated 90 Cloud Run revisions and 255 Artifact Registry image entries from ~85 builds over 5 days. Without policies, storage and management overhead grows unboundedly. The `keep-last-2-versions` policy (updated from the original `keep-latest-tag` policy) ensures both the current AND prior revision always have a pullable image — when a new `:latest` push happens, the previous image becomes untagged and would be eligible for deletion after 7 days under `keep-latest-tag`, breaking image-level rollback. The 2-revision cap on Cloud Run matches the 2-image cap on Artifact Registry.

**If the cleanup policy needs to be verified:**
```bash
GCLOUD=/opt/homebrew/share/google-cloud-sdk/bin/gcloud

# Verify Artifact Registry cleanup policy — SCCSSAR dev
$GCLOUD artifacts repositories describe dispatch-console \
  --location us-central1 --project sar-dispatch-sccssar-dev \
  --format="yaml(cleanupPolicies)"

# Verify Artifact Registry cleanup policy — personal dev
$GCLOUD artifacts repositories describe dispatch-console \
  --location us-central1 --project sar-dispatch-dev \
  --format="yaml(cleanupPolicies)"

# Verify Cloud Run revision count (SCCSSAR dev)
$GCLOUD run revisions list \
  --service dispatch-console --region us-central1 --project sar-dispatch-sccssar-dev \
  --format="table(name,createTime.date('%Y-%m-%d %H:%M'))"
```

**If policies need to be recreated:** Run `terraform apply` in `terraform/environments/sccssar-dev/` and `terraform/environments/dev/` respectively — the `cleanup_policies` blocks are already in `main.tf` in both environments.

---

## FAQ

### What happens when two dispatchers submit forms at the same time?

**Short answer: both process in parallel — Dispatcher #2 is not queued behind Dispatcher #1.**

Cloud Run handles concurrent requests natively. FastAPI/uvicorn is async, so while a Pass 1 Gemini call is in flight for one dispatcher, the server can accept and begin processing another dispatcher's request in the same process. Cloud Run gen2 defaults to `--concurrency=80` (80 simultaneous requests per instance) and will spin up additional instances if demand exceeds that.

| Layer | Concurrent behavior |
|-------|-------------------|
| **Cloud Run** | Multiple requests handled concurrently per instance; auto-scales to additional instances if needed |
| **FastAPI/uvicorn** | Async I/O — awaiting a Gemini response for Dispatcher #1 does not block Dispatcher #2's request from starting |
| **Gemini (Vertex AI)** | Each request makes independent Gemini calls; no shared state |
| **Nominatim / Overpass** | Each request makes its own independent HTTP calls |
| **Rate limiter (Firestore)** | Per-user buckets keyed by email — two different dispatchers never share a rate limit counter |

**Where simultaneous submissions could run into limits:**

- **Vertex AI quota** — Two simultaneous form submissions = 4 Gemini calls at once (2 passes × 2 dispatchers). Gemini 2.5 Flash quota is generous enough that this is not a concern at SAR dispatch volumes. At >5 simultaneous submissions, monitor the Vertex AI quota dashboard.
- **Nominatim** — OSM's Nominatim has a soft 1 req/sec courtesy limit. Two simultaneous requests making 1–2 geocoding calls each is fine in practice.
- **Cold start after long idle** — If two dispatchers hit the service simultaneously after an idle period, Cloud Run may spin up two instances in parallel (both cold-starting at the same time) rather than one waiting for the other.

**Bottom line:** The app handles at least 5–10 simultaneous submissions without issue, which exceeds any realistic SAR dispatch scenario. The 45–60 second processing time is per-request wall-clock time, not queued time.

---

### What happens when the container/server crashes or restarts?

**Short answer: Cloud Run automatically restarts the container; the dispatcher sees a brief error and resubmits.**

Cloud Run monitors container health via the liveness probe (`period=60s, timeout=10s, failure_threshold=5`). If the container crashes or the liveness probe fails, Cloud Run terminates the unhealthy container and starts a replacement.

**During the restart (~20–60 seconds):**
- Incoming requests receive HTTP **503 Service Unavailable** from Cloud Run's load balancer
- The dispatcher sees an error page or a failed submission
- **In-flight requests at the moment of the crash are lost** — the dispatcher must resubmit from the beginning (re-upload the same photo)

**Why in-flight requests are lost:** The backend is stateless. All processing happens in memory — the JPEG bytes, the Gemini responses, the geocoding results — and nothing is written to disk or a database mid-request. When the container process dies, all of that in-memory state is gone. There is no resume-from-checkpoint capability and no partial state to recover.

**Recovery time:** The startup probe (`initial_delay=10s`, `failure_threshold=8`) gives a new container up to ~90 seconds to pass its health check. In practice, the service is typically ready to accept requests within 20–40 seconds (same as a normal cold start).

**What the dispatcher should do:** Wait 30–60 seconds, then re-upload the same photo. The form photo is still on their device and the OCR pipeline is stateless, so re-running loses nothing. If the crash happened mid-dispatch (after Send), the polling state in Firestore (24h TTL) lets Cloud Tasks pick the chain back up — see §11b Cloud Tasks for the per-queue retry budgets.

**Crash vs. slow response:** A single slow request (e.g., a 45-second Gemini call) does not trigger a restart. The liveness probe's long period (`60s`) and high threshold (`5 consecutive failures`) were specifically tuned to avoid killing the container during normal long-running Gemini calls.

---

*Last updated: September 2026 — version 1.11.71. Per-change history lives in `git log` + [CHANGELOG.md](../CHANGELOG.md).*
