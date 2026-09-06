---
name: dispatch-after-action
description: Guided after-action review of ONE Dispatch Turbo callout or live dispatch test. Reads Cloud Run logs first (latency, memory, staging provider, geocode sanity, warnings), then interviews the dispatcher in three rounds across intake/OCR, staging, Slack, D4H, CalTopo, process, corrections, and feature asks — then writes a findings report to gitignored research/ and drafts (never auto-files) GitHub issues. Trigger on "after action", "AAR", "post-callout review", "debrief the dispatch", "lessons learned", "we had a callout", "we had a real dispatch", "post-mortem the dispatch". Scope is the Dispatch Turbo SOFTWARE only — not incident management, search tactics, or field operations.
---

# Dispatch Turbo — After-Action Review

**Objective:** capture everything learnable from a single dispatch — one real callout, or one live end-to-end test — while it's still fresh, and convert it into tracked work (GitHub issues), durable context (MEMORY.md), and hardened rules (CLAUDE.md Locked Decisions) instead of losing it.

**Repo:** the Dispatch Turbo code checkout — work from there. (Maintainer note: since the
2026-09 migration that is the `SCCSSAR/SAR_dispatch_flow` clone, **not** the older personal
checkout, which is now the ops/tracker repo `SCCSSAR/SAR_dispatch_flow-ops`.)

> **This skill references maintainer-only files that are not published in this repository.**
> `docs/release-notes.md`, `docs/ops-runbook.md`, `docs/dispatcher-guide.md` and the
> `scripts/` helpers are withheld from the public tree and live in the ops repo. Steps that
> name them are runnable by the maintainer only; every other step works from this repo
> alone. Nothing here is a broken link — the files exist, just not here.

## Scope fence — read this before asking anything

This review covers **the Dispatch Turbo software and its integrations only**: the intake form, OCR, the staging pipeline, Everbridge, Slack, D4H, CalTopo, geocoding, and the dispatch console UI.

It does **not** cover incident management, search strategy, team assignments, field tactics, subject outcome, or agency coordination. If the dispatcher raises one of those, acknowledge it, note it in a single "Out of scope — passed along" line in the report, and steer back. A SAR unit has its own operational debrief process; this is not it, and blurring the two makes the report useless to both audiences.

**One dispatch per run.** If several callouts happened since the last review, run the skill once per dispatch — the log windows, the metrics, and the findings are all per-incident. Ask which one first.

## Hard rules

1. **Read-only against both environments.** Never run `build-dev.sh` / `build-sccssar-dev.sh`, never deploy, never `terraform apply`. This is an observation task. Fixes that come out of it go through the normal branch → commit → PR → Bill-merges → build workflow in a *separate* session or a later step, never inline here.
2. **PII stays in `research/`.** Real callouts carry subject name, DOB, physical description, home address, and reporting-party contact info — and the Event Name itself embeds street plus agency. **Responder and team-member names count too** (a named responder who was missed, declined, or needed a manual add is still a person). The full report is written to gitignored `research/after-action/` (see Step 5). Anything that flows *outward* — GitHub issues, MEMORY.md, CLAUDE.md, `docs/release-notes.md`, PR bodies — must be PII-free: describe the *shape* of the input ("a v2 JPEG whose residence line omitted the city and whose street name was written as one word"), never the content. This is the "No PII in logs" guarantee (CLAUDE.md Critical Privacy & Security #3) extended to after-action artifacts; the repo is bound for public release (issue #332) and git history is permanent.
3. **Do not file GitHub issues without explicit confirmation.** Draft the bodies, show them, wait for a yes. Filing creates visible tickets others act on. See `feedback_no_premature_issue_filing` — this rule has been violated before. When a review produces many issues, show them in ONE batch for a single go/no-go rather than one round-trip per issue.
4. **Don't classify a dispatch as "real" or "test" on your own.** Ask. Event names get reused across testing sessions and a mutual-aid callout can look like a typo (the 2026-07-18 out-of-county mutual-aid dispatch was a real callout that looked like noise).
5. **Distinguish "new" from "known."** Before writing up any finding as novel, grep MEMORY.md, CLAUDE.md, `docs/release-notes.md`, and `gh issue list --state all --search "<term>"`. A rediscovered known issue is still worth a line in the report ("recurred, N-th occurrence") but it is not a new issue, and filing it as one creates duplicates.
6. **Ask for the intake form.** The photographed or PDF form is the single most valuable artifact in the review — it explains OCR findings that logs alone make unintelligible. Ask the dispatcher to hand it over and save it into the report directory alongside `report.md`. On the 2026-07-24 review the form is what turned "staging landed in the wrong city" into a precise root cause.

## Step 0 — Identify the incident, and check auth FIRST

### 0a. Auth preflight — both environments, before asking anything

Do this before any interview question. If the logs are unavailable, the whole shape of the review changes and you need to know now, not after the dispatcher has answered six questions.

**Every project ID, gcloud configuration name and operator account comes from
`deploy.env` in the repo root** — the gitignored file described by `deploy.env.template`.
Never hardcode them here or in a command you run; read them, so this skill works for
whoever is running it. If `deploy.env` is missing, say so and stop: without it you cannot
know which projects to query.

```bash
# Read both environments' settings out of deploy.env.
for env in dev sccssar-dev; do
  eval "$(ENV_NAME=$env bash -c '. ./deploy.env; \
    printf "CFG=%q; ACCT=%q; PROJ=%q\n" "$GCLOUD_CONFIG" "$OPERATOR_ACCOUNT" "$PROJECT"')"
  printf '%s: config=%s account=%s project=%s\n' "$env" "$CFG" "$ACCT" "$PROJ"
done
```

```bash
GCLOUD="${GCLOUD:-$(command -v gcloud)}"
ORIG=$($GCLOUD config configurations list --filter='is_active=true' --format='value(name)')
for env in dev sccssar-dev; do
  CFG=$(ENV_NAME=$env bash -c '. ./deploy.env; printf "%s" "$GCLOUD_CONFIG"')
  $GCLOUD config configurations activate "$CFG" >/dev/null 2>&1
  if $GCLOUD auth print-access-token >/dev/null 2>&1; then echo "$CFG: OK"; else echo "$CFG: EXPIRED"; fi
done
$GCLOUD config configurations activate "$ORIG" >/dev/null 2>&1
```

**`gcloud auth login` is an interactive browser flow — you cannot run it. The dispatcher must.** When a config reports EXPIRED, print the exact pair below and ask them to run it, then re-check. Reviewing a dispatch on one environment only needs that environment's auth; say so rather than blocking on both.

Build the pair from `deploy.env` rather than reciting it — substitute that
environment's `GCLOUD_CONFIG` and `OPERATOR_ACCOUNT`:

```
gcloud config configurations activate <GCLOUD_CONFIG> && gcloud auth login <OPERATOR_ACCOUNT>
```

**Activate the profile FIRST, authenticate SECOND** — `gcloud auth login` writes `core/account` into whichever config is active, so authenticating to "fix" a 403 corrupts the profile you were in (bitten 2026-07-17 and 2026-07-19). After the dispatcher re-auths, verify the config's account did not drift before trusting it. If an environment returns `PERMISSION_DENIED`, its `core/account` has probably drifted to another environment's account: `$GCLOUD config set account <OPERATOR_ACCOUNT> --configuration <GCLOUD_CONFIG>` and retry, taking both values from `deploy.env`. Full recipes: `docs/ops-runbook.md`. Always restore the original active config when the review ends.

If auth genuinely cannot be obtained, say the log half is degraded and proceed to the interview — but **never guess at what the logs would have said.**

### 0b. Enumerate recent dispatches so the dispatcher can pick

Don't ask "which dispatch?" cold — show them the list. Unbounded by incident window, ~14 days, per env:

```bash
$GCLOUD logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=dispatch-console AND "create-map request" AND timestamp>="<14d-ago-UTC>"' \
  --project <PROJECT> --limit=100 \
  --format="value(timestamp,jsonPayload.message,resource.labels.revision_name)"
```

Timestamps are UTC — convert to Pacific before showing them, or a 2026-07-19T05:44Z hit will look like the wrong day (it's the evening of 07-18 PT). **The event name in this line is truncated to 40 characters** (`event[:40]` in the handler), so `'2026-07-18 XXSO Joseph D. Grant County P'` is log truncation, not an event-name bug — don't report it as one.

### 0c. Confirm identity

- **Which dispatch** — date, approximate local time, event name.
- **Which environment** — `sccssar-dev` (EB `full` / Slack `full` — **production, real dispatchers, real pages**) or personal-dev (EB `safe` / Slack `shadow`). Confirm against the `Feature flags loaded` line in Pull E, never assume.
- **Real callout or test** (Hard rule 4).
- **Who dispatched** — firsthand or secondhand. Secondhand answers get marked as such; they shouldn't drive a Locked Decision.

Then pin the build, because every finding's interpretation depends on which code ran:

```bash
git log --oneline -3
cat VERSION
```

Compare against Pull A's serving revision and the `App version: <x.y.z> (phase=…, sha=…)` startup line. Per Session Rule #8, never assume the deployed build matches HEAD.

## Step 1 — Pull the logs BEFORE the interview

Logs first is deliberate: it lets you ask *"I see the LKP geocoded to a point 8 miles outside the city — did the staging list look wrong to you?"* instead of *"anything unusual?"*, and it catches what the dispatcher never saw. Present log findings during the interview as priming, not afterward as a separate report.

### Window

`create-map request` fires **mid-dispatch** — after OCR and after the dispatcher's textarea review, which can take many minutes. So do **not** anchor −15 min on it. Use **create-map − 45 min → create-map + 90 min**, then confirm empirically that `OCR request received` falls inside the lower bound and widen if it doesn't. (On 2026-07-24 the upload was 9 minutes before create-map; a longer review would have fallen outside a 15-minute window.) The +90 covers the polling chain, late YES arrivals, and any post-dispatch correction.

Explicit UTC bounds only — never `--freshness`, which is anchored to run time and can't express an incident boundary:

```python
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
PT = ZoneInfo("America/Los_Angeles"); UTC = ZoneInfo("UTC")
createmap_pt = datetime(2026, 7, 24, 11, 56, tzinfo=PT)   # from Pull A / Step 0b
lo = (createmap_pt - timedelta(minutes=45)).astimezone(UTC).isoformat()
hi = (createmap_pt + timedelta(minutes=90)).astimezone(UTC).isoformat()
print(lo, hi)
```

### Pull A — pin the incident and its serving revision

```bash
$GCLOUD logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=dispatch-console AND "create-map request" AND timestamp>="<lo>" AND timestamp<="<hi>"' \
  --project <PROJECT> --limit=50 \
  --format="value(timestamp,jsonPayload.message,resource.labels.revision_name)"
```

`create-map request | sub=… event='<Event Name>'` is the one line every successful dispatch emits regardless of revision or staging source. **There is no happy-path "dispatch complete" log line** — `/send-notification` emits warnings only and nothing on success. So Pull A anchors the whole timeline, and dispatch-time facts (EB event id, Slack channel, tally ts) come from the Firestore incident doc, not from logs. See "Could not measure."

### Pull B — the dispatch itself, in full

The single highest-value pull: every application log line for the ~10 minutes around the dispatch, in order. This is where the geocode queries, staging anchors, Pass B entries, event-name reconstruction, and CalTopo calls all live.

```bash
$GCLOUD logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=dispatch-console AND timestamp>="<ocr_start>" AND timestamp<="<createmap+90s>" AND jsonPayload.message!=""' \
  --project <PROJECT> --limit=300 --format="value(timestamp,severity,jsonPayload.message)"
```

Read the full geocode URLs. `httpx` logs every outbound request at INFO, so the Nominatim / Google Maps / Geoapify / CalTopo / EB / D4H / Slack calls are all here with their **query strings and coordinates** — which is how you check geocode sanity (below) and spot label bleed-through such as a staging entry geocoded as `q=CalTopo Map ID:, CA`.

### Pull C — OCR latency and memory

For a **same-day** review, `scripts/oom-check` is the convenience path:

```bash
scripts/oom-check 1d sccssar-dev     # or: scripts/oom-check 1d dev
```

It surfaces OOMs (`Memory limit` in `textPayload`), cold-start `startup rss_baseline_mib=…`, and `OCR request complete | … total_ms=… rss_post_mib=… delta_mib=… gc_recovered_mib=… gc_collected=…`.

**But `oom-check` accepts only `--freshness`, so it cannot target an older incident's window** — for anything not from today it returns days of undifferentiated lines. For an older incident use the explicit-window form instead:

```bash
$GCLOUD logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=dispatch-console AND ("OCR request received" OR "OCR request complete" OR "startup rss_baseline_mib" OR "Memory limit") AND timestamp>="<lo>" AND timestamp<="<hi>"' \
  --project <PROJECT> --limit=50 --format="value(timestamp,jsonPayload.message,textPayload)"
```

Read: `total_ms` (dispatcher-perceived wait), `content_length` (upload size), `delta_mib`, `gc_recovered_mib`, any OOM at all (one reopens #519), and whether a cold start immediately preceded the upload (it inflates `total_ms`).

### Pull D — staging provider telemetry

```bash
$GCLOUD logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=dispatch-console AND ("Staging source compare" OR "Staging sequential" OR "Staging fallback" OR "Geoapify staging candidates" OR "Overpass staging candidates" OR "apply-staging-override") AND timestamp>="<lo>" AND timestamp<="<hi>"' \
  --project <PROJECT> --limit=200 --format="value(timestamp,jsonPayload.message)"
```

Parse with regex, not field position: `geoapify_ms=(\d+)`, `overpass_ms=(\d+)`, `geoapify_count=(\d+)`, `geoapify_ok=(True|False)`, `radius_m=(\d+)`, and from the override line `outcome=(\w+)`, `nearby_count=(\d+)`, `mode=(\w+)`.

- **sccssar-dev** (`STAGING_SHADOW=off`, sequential — production): `Staging sequential | source=geoapify …` on success. A `Staging fallback | primary=geoapify backup=overpass …` line means **Geoapify failed in production** — the highest-value thing this pull can find. Target zero.
- **personal-dev** (`STAGING_SHADOW=on`, parallel shadow): `Staging source compare | …` carries both providers.
- **A revision whose `Feature flags loaded` lacks `STAGING_SOURCE=`** is pre-Geoapify code and logs only `Overpass staging candidates | …`. Zero Geoapify hits then means *old revision*, not failure — this blind spot hid the mutual-aid dispatch on 2026-07-19.
- `radius_m=1200` is the initial OCR-time lookup; `radius_m=300` is an `/apply-staging-override` re-anchor. **Multiple override lines mean the dispatcher was fighting the tool** — count them and ask why.

Sanity baseline (2026-07-18 soak): Geoapify typically sub-second, ~500 ms; one real Overpass outage took 20.5 s while Geoapify covered it in ~507 ms.

### Pull E — every warning and error

```bash
$GCLOUD logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=dispatch-console AND severity>=WARNING AND timestamp>="<lo>" AND timestamp<="<hi>"' \
  --project <PROJECT> --limit=200 \
  --format="value(timestamp,severity,jsonPayload.message,textPayload,httpRequest.status,httpRequest.requestUrl)"
```

**`httpRequest.status` and `httpRequest.requestUrl` are mandatory in the format string.** Cloud Run emits *request* logs that have **no `jsonPayload` and no `textPayload`** — with the older format string they render as completely blank rows and you silently lose them. On 2026-07-24 two blank rows were 4xx responses that, paired with two `auth` warnings, revealed a mid-incident ID-token expiry. When a row still looks empty, re-pull that second as `--format=json` and inspect `httpRequest`.

| Log text | What it means |
|---|---|
| `Token verification failed` + nearby 4xx request logs | dispatcher's Google ID token expired mid-incident (~1 h) — issue #455 |
| `no_eb_email … replied YES but has no email` | responder NOT auto-invited to Slack; also absent from D4H (both key on email) |
| `All Overpass mirrors exhausted` | staging had no OSM source; issues #453 / #484 |
| `Geoapify /v2/places returned HTTP …` / `Geoapify API failed` | primary staging provider degraded |
| `Staging dispatcher failed` | staging blew up wholesale — dispatcher saw no recommendations |
| `LKP not recorded — falling back to Residence address` | blank-LKP path exercised |
| `Staging Pass B geocode failed (Nominatim + Google Maps)` | staging text unresolvable |
| `CalTopo map creation failed` / `CalTopo rate limited` | CalTopo leg failed |
| `D4H create failed` / `raised unexpected` / `post-create partial failure` | D4H leg failed or half-succeeded |
| `Poll-incident: doc not found … chain ends` | polling chain terminated early — arrivals may have stopped being tracked |
| `Memory limit` (textPayload) | OOM — reopens #519 immediately |

Triage each hit as **expected-and-benign**, **known** (cite the issue, count the recurrence), or **new**. Use the bracket-pair technique for anything ambiguous (`feedback-cloud-run-log-bracket-pair-technique`).

### Pull F — flags and revision context

```bash
$GCLOUD logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=dispatch-console AND ("Feature flags loaded" OR "App version:") AND timestamp>="<day-start>"' \
  --project <PROJECT> --limit=20 --format="value(timestamp,resource.labels.revision_name,jsonPayload.message)"
$GCLOUD run revisions list --service dispatch-console --project <PROJECT> --region us-central1 \
  --format="table(metadata.name,metadata.creationTimestamp)" --limit=6
```

`Feature flags loaded: EVERBRIDGE_MODE=… SLACK_MODE=… STAGING_SOURCE=… STAGING_SHADOW=…` plus `App version: <x.y.z> (phase=…, sha=…)` are ground truth for what ran. These only appear on a cold start, so widen the window to the whole day if the incident didn't trigger one.

### Log-noise warning

An hour of EB polling produces ~240 `poll_task[post_discovery] ENQUEUED` lines plus ~240 httpx `GET https://api.everbridge.net/rest/notifications/…` lines, which will swamp any broad pull. When searching the post-dispatch window for responder events, exclude them:

```bash
... --format="value(timestamp,severity,jsonPayload.message)" | grep -vE "everbridge\.net|poll_task\[post_discovery\]"
```

## Step 1.5 — Standing regression checks

Run these every time, whether or not the dispatcher mentions anything. Each exists because it has failed before, silently, in a way the dispatcher couldn't see.

1. **Geocode sanity — does the LKP coordinate actually land in the requested city?** Take the Nominatim/Google Maps query string from Pull B and the returned lat/lng, and check they agree. **This is the check that would have caught the worst finding this project has recorded.** On 2026-07-24 an LKP whose two-word street name had been written as one word returned a coordinate in a **different city ~8 miles away**: the one-word form does not exist in the requested city, and Nominatim matched a same-named street elsewhere while ignoring both the city and the ZIP that were present in the query. `_house_number_consistent()` passed because the wrong city had that house number too, and Google Maps — the spelling-correction fallback — is only consulted on Nominatim *failure*, never on a confident *wrong answer*. Cross-check the staging candidates' city names against the LKP's expected city; a list of addresses in a city nobody mentioned is the tell.
2. **Did the dispatcher have to correct or retract anything after dispatching?** Ask explicitly (Round 3) and look for the signature: repeated `apply-staging-override` lines, a re-edited event name, or an EB/Slack correction. On 2026-07-24 a stale city string survived the dispatcher's manual edits into the Everbridge body and **responders were paged to the wrong city** — the tool logged nothing wrong at all. No log line will ever surface this class; only the question will.
3. **A responder replied YES but never landed in the Slack channel.** Two distinct causes, don't conflate them:
   - `no_eb_email` — the EB contact has no email, so there was nothing to look up. Handled by design: not invited, ⚠️ posted to the channel, dispatcher adds manually. **Live-validated on a real callout 2026-07-24 and praised by the dispatcher.** The fix is an ops one (add the responder's `@sccssar.org` email to their EB contact), not code.
   - A `users.lookupByEmail` raise or `invite_user` failure at poll time is **permanent, not self-healing** — `_apply_responder_diff` persists the arrival *before* the Slack action, so the next 15-second cycle treats them as known and never retries (verified 2026-07-22 against `backend/main.py:7420-7476`; the in-code "self-heal" comment is wrong). Any occurrence is a real responder who lost channel access on a real search — report it loudly.
4. **EB YES count vs D4H ATTENDING rows.** These diverge whenever a responder's email is missing or mismatched — and `no_eb_email` drops them from **both** Slack and D4H, since both key on email (confirmed 2026-07-24: 4 YES, 3 D4H rows). Also 2 of 11 dropped on the 2026-07-06 callout. Count `D4H per-YES sync done` lines and compare.
5. **The tri-count tally matched Everbridge.** `✅N confirmed ❌M declined ⏳K no response` (#592/#596) should reconcile exactly. EB's `confirmed` means *responded at all* (Yes **or** No), which is why the counts parse `allDetails[]` rather than `confirmedCount`. Note a responder can reply NO and later YES — `allDetails[]` carries every response, so the later YES surfaces as a new arrival on the next poll. That's correct behavior and worth explaining if the dispatcher is surprised by it.
6. **Which staging location was actually used — and by whom.** Two separate questions: what the *dispatcher* selected, and where the *field* ultimately staged. On 2026-07-24 the dispatcher used none of the recommendations (hand-searched Google Maps), and first-on-scene then relocated staging again because the chosen site was too small. A pattern of "not #1" is the strongest signal available on the ranking heuristic, and nothing else measures it.
7. **Secrets in logs.** `httpx` logs full request URLs at INFO, so any API key passed as a query parameter is written to Cloud Run logs in plaintext. Confirmed 2026-07-24 for the Google Maps key. Grep the window for `key=`, `token=`, `signature=`, `apiKey=` and report anything found.
8. **Auth/token expiry during the incident.** `Token verification failed` plus 4xx request logs roughly an hour after the dispatcher opened the page means the Google ID token expired while the browser was still polling (issue #455). Only visible if Pull E includes `httpRequest.status`.
9. **CalTopo marker sanity.** Exactly one `cp`; LKP `placemark2`; residence `hut` present even on geocode failure; no marker on Null Island; officer entry blue `point` unless sole entry (wilderness case). **Also check for markers far outside the search area** — a bad LKP geocode leaves the LKP/residence markers stranded even after a staging override re-anchors staging, and on 2026-07-24 another dispatcher deleted one by hand as a distractor.
10. **Deployed version vs HEAD.** Pull A's revision and the `App version:` line against `VERSION` and `git log`. State any gap explicitly.
11. **Form-version prefix in the event log.** Entry 2 must read `v1 Intake form processed…` or `v2 …`. A v2 form misread as v1 produces wrong checkbox answers with no other diagnostic trail.

## Step 2 — The interview, three rounds

Lead each round with what the logs already showed, then ask. Keep it conversational; follow up when an answer is interesting rather than marching through the list. Record answers verbatim-ish — the dispatcher's own words about what felt wrong are the highest-value content in the whole document, and paraphrasing sands off the detail that later turns out to matter.

**Round 1 — Intake and processing**
- Ask for the form (Hard rule 6) if you don't have it yet.
- Anything about **the form itself** we haven't seen before? Handwriting, layout, a field used in a new way, an unusual agency, mutual aid, scan quality, blank or contradictory fields, an annotation-filled PDF? **Missing or partial data from the requesting agency** — a residence line with no city, a street name written as one word, an abbreviation — is a first-class finding, not a footnote; it's usually the root of a geocoding failure.
- Anything new in **how Dispatch Turbo processed it** — OCR misreads, checkbox inversions, wrong age or DOB, wrong event name, wrong agency, Koester bracket, LPB answers?
- **Which fields did you edit** in the textarea, and were you correcting the tool or adding context it couldn't have known? (Closest thing to a real OCR-accuracy measurement.)
- **Did you check the checkbox answers against the form?** If not, say so in the report — it's unrecoverable after the fact and shouldn't be recorded as "no problems found."
- How long did it feel from upload to dispatch, versus usual?

**Round 2 — Integrations**
- **Staging** — were the recommendations good? Was #1 one you'd have picked? Anything absurd, too far, closed, inaccessible? Enough options? **Was the site big enough** for the resources that showed up? Did the wilderness/no-POI case come up? If the logs show overrides, why did you override?
- **Slack** — anything novel or unexpected? Channel creation, who got pre-added, who was missing, invites, the pinned welcome, the CalTopo follow-up unfurl, DMs, the `#active-incidents` tally, at-risk text rendering, responder confusion?
- **D4H** — incident created cleanly? Attendance populated as responders replied? Roles right? Cleanup needed afterward? Blank-name or duplicate rows?
- **CalTopo** — map created, markers right, base layer right, title clean, comments present, link worked from a phone? **Did anyone else edit or delete anything on the map?**

**Round 3 — Process, corrections, and asks**
- **Did you have to correct, retract, or re-send anything after dispatching?** (Standing check 2. Ask this every time — it is the highest-consequence question in the review and no log line will surface it.)
- Anything else unusual — timing, network, browser, phone vs laptop, a repeated step, a hang, a refresh that lost work?
- **Did you have to do anything manually that the tool was supposed to do?** (Manual-intervention count is the sharpest single quality metric.)
- **Was there anything you chose not to use because you didn't trust it?** Did you fall back to a pre-Turbo manual workflow at any point? (Trust is the actual adoption blocker. A dispatcher opening Google Maps by hand mid-dispatch is a red alert regardless of what the logs say.)
- Did anyone call or text you with a question the tool should have already answered?
- **What worked? Did anyone praise anything, or say something was better than before?** Ask this explicitly — every other question in this skill is failure-shaped, so positives go unrecorded unless invited, and that quietly distorts prioritization. On 2026-07-24 responders volunteered that the instant Slack invite plus real information on replying YES was great, and that it bought tolerance for a staging change mid-dispatch. Losing that would have made a correctness fix look strictly more important than the feedback loop it depends on.
- **Any new features requested or mentioned** — by you, another dispatcher, search management, a responder, or an outside agency? Capture who asked and the underlying need, not just the proposed solution.

## Step 3 — Log findings the dispatcher didn't mention

State plainly anything the logs surfaced that didn't come up — a fallback they didn't feel, a warning that never reached the UI, a latency spike, a wrong coordinate they interpreted as "no results nearby." These are the most valuable findings in the review, because they're invisible from the dispatcher's seat and they grow silently until they bite during a real search.

**Where the dispatcher's own root-cause theory and the logs disagree, say so plainly and show the evidence.** On 2026-07-24 the dispatcher attributed the wrong-city anchor to a missing city on the residence line; the logs showed the city *and* a ZIP were present in the geocode query, which pointed at a different and more tractable fix. Getting this right is the difference between fixing the cause and hardening the wrong layer.

Also state what you **could not** measure, and why. Known gaps as of 2026-07-24:

- **No end-to-end dispatch-duration telemetry.** The product's premise is 20-35 min → 2-3 min, and there is no completion log line, so only OCR→create-map is derivable. Any post-dispatch correction tail is entirely invisible. Offer to file an issue for a `send-notification complete | total_ms=…` line.
- **No log of which staging entry was chosen** (only `/apply-staging-override` outcome/mode when an override happens).
- **No log of textarea edits**, so OCR-correction counts are self-reported.
- **Slack channel membership and tally text** need Slack API or Firestore reads, not Cloud Run logs.

## Step 4 — Classify every finding

Assign exactly one disposition. Resist the pull toward filing everything.

- **Bug** → draft a GitHub issue. If it touches `backend/main.py` handlers or an integration client, note that the fix PR needs the Failure-mode Discipline six-question pass plus a `feature-dev:code-reviewer` run.
- **Feature request** → draft an issue capturing the *need* and who asked, priority-tagged (🔴/🟡/🔵).
- **Known recurrence** → no new issue; comment on the existing one and count the recurrence in the report.
- **Locked-Decision candidate** → a failure mode with a guardrail worth pinning forever. Needs **code + test pin + CLAUDE.md** to survive. Propose the row, don't write it unilaterally.
- **Ops fix, not code** → e.g. a missing email on an Everbridge contact record. Name the action and the owner.
- **Doc fix** → `docs/dispatcher-guide.md`, `architecture.md`, `ops-runbook.md`. There is **no md→pdf tooling** here, so a `dispatcher-guide.md` edit requires regenerating `dispatcher-guide.pdf` by hand.
- **Validation / no action** → something that worked, especially a path awaiting live proof. Record it; it's how untested-landmine items get closed.
- **Accepted / no action** → record it with the reason, so it isn't re-litigated next quarter.
- **Out of scope** → the single passed-along line.

## Step 5 — Write the report

`research/after-action/<YYYY-MM-DD>-<short-slug>/report.md` — create the dirs, and save the intake form alongside it. **`research/` is gitignored; never commit it.** Full detail including PII is acceptable *here and only here*.

```markdown
# After-action — <event name>, <YYYY-MM-DD>

**Dispatch:** <date, OCR time / dispatch time PT> · **Env:** <sccssar-dev (PRODUCTION) | personal-dev> · **REAL CALLOUT | test**
**Serving revision:** <rev> · **VERSION:** <x.y.z> · **Deployed sha:** <sha> · **repo main at review:** <sha>
**Modes:** EB <mode> / Slack <mode> / staging <source> / shadow <on|off>
**Dispatcher:** <who> (firsthand|secondhand) · **Reviewed:** <date>
**Intake:** <v1|v2, handwritten|typed, JPEG|PDF, size>
**EB notification:** <id> · **D4H activity:** <id> · **CalTopo:** <map id> · **event_id:** <id>

## Headline
<2-5 sentences: did it work, and what's the one thing to fix.>

## Alerts
<Step 1.5 results, most severe first, with ✅ for the checks that passed. "None" is welcome.>

## Measurements
| Metric | Value | Notes |
|---|---|---|
| OCR total_ms | | cold start? upload size? |
| RSS delta / gc recovered | | OOMs: |
| Staging provider / latency / candidate count | | fallbacks? |
| Override attempts | | how many returned zero? |
| Staging entry used — dispatcher / field | | #1? relocated on arrival? |
| EB confirmed / declined / no-response | | matched EB UI? |
| Slack channel vs YES count | | gap? |
| D4H ATTENDING vs YES count | | gap? |
| Manual interventions | | enumerate them |
| Perceived time to dispatch | | plus any correction tail |
| CalTopo | | map id, marker count, latency |

## Root cause
<For the primary finding: the data, what the code did with it, which guard passed and why, and which layer should have caught it. Quote the actual log lines and query strings.>

## Operational sequence
<Numbered, if the dispatcher had to recover from something. This is the part that turns a bug into a priority.>

## Interview
### Intake and processing
### Staging
### Slack
### D4H
### CalTopo
### Process
### What worked — positive signals
### Feature asks

## Log findings not seen by the dispatcher

## Could not measure

## Findings and dispositions
| # | Finding | Disposition | Tracked as |
|---|---|---|---|

## Out of scope — passed along
```

Then write the **PII-free** distillates:

- **MEMORY.md** — one "Recent sessions" row. Bump "Current state" if material state changed. Durable context gets its own `project-*` / `feedback-*` file, linked.
- **GitHub issues** — show every drafted body in one batch, get one go/no-go, then file. Never put a closing keyword adjacent to `#N` unless you intend GitHub to close that issue on merge (guardrail #534 — this has auto-closed a live issue before).
- **CLAUDE.md** — only if a Locked Decision or Session Rule was genuinely discovered or refined. Most reviews shouldn't touch it. Propose, don't apply.
- **`docs/release-notes.md`** — only if a new VERSION was actually deployed (Session Rule #11c). A review alone deploys nothing.

Repo-file edits go on a branch, chained in one Bash call per Session Rule #14. Never `git add -A`. Never self-merge.

## Step 6 — Close out

Restore the original gcloud config. Report in-session: headline, alerts, findings table, what got filed versus drafted, what's still open. Then the copy-pasteable next-session prompt (Session Rule #11a) naming the immediate next action.

## Growing this skill

When a review surfaces a check that *should* have been standing — a metric nobody was watching, a log string worth grepping every time, a question that unlocked something — add it to Step 1.5 or Step 2 in the same session. Every numbered check here exists because something failed silently once, and the 2026-07-24 XXSO MAIN review alone added checks 1, 2, 7, 8, the positive-signals question, and most of the Pull-E format string. Same growth pattern as `backend/test_doc_parity.py`.
