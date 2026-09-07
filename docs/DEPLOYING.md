# Deploying the Dispatch Console

This guide takes you from an empty Google Cloud project to a running dispatch console for
your own team. It assumes you are comfortable with `gcloud` and Terraform, and it makes no
assumptions about which downstream systems you use.

Budget about 90 minutes for a first deployment. Most of that is waiting for APIs to enable
and the first container image to build.

> **Start with the pipeline, not the integrations.** The OCR and staging half of this app
> works with nothing but a GCP project and a Gemini model. Everbridge, Slack, CalTopo and
> D4H are each independent and each optional — an integration you have no credentials for
> is simply not offered in the UI. Get a form to parse first. Add the systems your team
> actually uses after that, one at a time, so a failure has one possible cause.

---

## 1. Prerequisites

Install these on your local machine:

- The `gcloud` CLI, authenticated.
- Docker.
- Terraform.
- Python 3.11 or later, to run the test suite.

Create these accounts:

- A Google Cloud project with billing enabled.
- A Google account that will be the first authorized dispatcher.

---

## 2. Enable the required APIs

Enable these APIs in your project:

```bash
gcloud services enable \
  run.googleapis.com \
  aiplatform.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  cloudtasks.googleapis.com \
  iamcredentials.googleapis.com \
  --project <your-project-id>
```

Enable `docs.googleapis.com` and `drive.googleapis.com` as well if you want the Working
Notes feature.

API enablement is not instant. If a later step reports that an API "has not been used in
project ... before or it is disabled", wait one minute and repeat the step.

---

## 3. Create the Firestore database

1. Create a Firestore database in Native mode.
2. Choose the same region you will deploy Cloud Run to.

Firestore holds the rate-limit counters and the incident state that the responder polling
depends on. It does not hold form data.

---

## 4. Configure OAuth

The app authenticates dispatchers with Google Sign-In and checks the result against an
email allowlist on the server.

1. Configure the OAuth consent screen for your organization.
2. Create an OAuth client ID of type **Web application**.
3. Add your Cloud Run service URL to the authorized JavaScript origins.
4. Record the client ID. You supply it at build time.

If your consent screen stays in Testing mode, add each dispatcher as a Test User. A
dispatcher who is on the backend allowlist but not on the Test Users list cannot sign in.

---

## 5. Create the secrets

Create a Secret Manager secret for each integration you intend to use. Only the first one
is required.

| Secret | Required | What it holds |
|---|---|---|
| `dispatch-authorized-emails` | Yes | Comma-separated dispatcher email addresses |
| `dispatch-google-client-id` | Yes | The OAuth client ID from step 4 |
| `geoapify-api-key` | Effectively yes | Staging POI lookup. Optional only in the sense that the app still starts without it |
| `google-maps-api-key` | No | Address spelling-correction fallback and intersection geocoding |
| `caltopo-credential-id`, `caltopo-credential-secret`, `caltopo-team-id` | No | CalTopo Team API, for incident maps |
| `everbridge-credentials` | No | Everbridge REST API, for notification and polling |
| `slack-bot-token`, `slack-so-coordinator-email` | No | Slack incident channels |
| `d4h-access-token` | No | D4H incident records and attendance sync |
| `dispatch-safe-list` | No | Addresses that may receive notifications while testing |

Restrict every API key you create to the specific APIs it needs. A geocoding key that can
call any Google API is a billing incident waiting to happen.

> **Start on Geoapify, not Overpass.** The staging POI lookup can run against either, and
> the code still defaults to Overpass because that is what it was built on — but set
> `STAGING_SOURCE=geoapify` from your first deploy and treat the Geoapify key as required.
> Overpass is free public infrastructure and it behaves like it: the mirrors rate-limit,
> they go down, the mirror list itself churns, and this app has an open pair of issues
> (#453, #484) about staging quality collapsing during an Overpass outage rather than
> failing visibly. It also drove enough memory pressure to need its own recurring
> out-of-memory watch. Beyond reliability, Geoapify returns structured street addresses;
> OSM frequently does not, and the staging line's text is what a responder's phone
> searches for. A recommendation without a navigable address is not a recommendation.
> Overpass stays in the tree as a fallback for the case where Geoapify fails, which is the
> job it is now good at.

> **On rotation.** Add new secret versions through `bin/rotate-secret.sh`, not
> `gcloud secrets versions add`. The wrapper updates the rotation labels at the same time,
> and those labels are what the app's own rotation banner reads. Call `gcloud` directly and
> the version moves while the label stays behind, which is worse than no label at all —
> it tells you a secret is fresh when it is not.

---

## 6. Configure Terraform

1. Copy `terraform/environments/dev/terraform.tfvars.template` to `terraform.tfvars`.
2. Set your project ID, region, and the values for the integrations you enabled.
3. Create a GCS bucket for Terraform state.
4. Turn on object versioning on that bucket.
5. Run `terraform init`.
6. Run `terraform apply`.

Keep `terraform.tfvars` out of version control. It is already in `.gitignore`.

### Environment variables that are not secrets

Most configuration reaches Cloud Run as a secret reference. These do not — they are plain
values set in `main.tf`, and they are easy to miss because nothing fails loudly when they
are absent (each defaults to an empty string).

| Variable | Purpose |
|---|---|
| `GCP_PROJECT` | Project ID used when constructing Cloud Tasks queue paths |
| `GCP_REGION` | Region for those queues; defaults to `us-central1` |
| `CLOUD_TASKS_SERVICE_ACCOUNT` | Service account whose OIDC token the polling tasks carry. The polling endpoints pin this address, so a mismatch makes every poll 403 |
| `EVERBRIDGE_ORG_ID` | Everbridge organization the notifications are created in |
| `EVERBRIDGE_CALLER_ID` | Caller ID presented on Everbridge voice paths |
| `EVERBRIDGE_DELIVER_PATHS` | Which Everbridge delivery paths a notification uses (SMS, voice, email) |
| `EVERBRIDGE_CATEGORY_INCOUNTY` | Everbridge category ID applied to in-county call-outs |
| `EVERBRIDGE_CATEGORY_MUTUALAID` | Everbridge category ID applied to mutual-aid call-outs |
| `EVERBRIDGE_SUPPRESSED_GROUP_IDS` | Groups that must never be paged, even if selected — a safety stop, so set it before your first live send |

The Everbridge values are specific to your organization; read them from your own Everbridge
account rather than copying another team's.

**Any change to an environment variable requires `terraform apply`.** The build scripts do
not read Terraform configuration. A merged `.tf` change that has not been applied is a
change that has not happened, and the resulting deployment looks completely healthy.

---

## 7. Build and deploy

1. Copy one of the `build-*.sh` scripts and edit the project ID and service name.
2. Run your build script.

The script runs the test suite before it builds. A failing test stops the deploy.

The build passes the OAuth client ID in as a build argument, so the image is specific to
one environment. Do not promote an image between environments that use different OAuth
clients.

---

## 8. Verify the deployment

1. Open the service URL and confirm the sign-in page loads.
2. Sign in with an authorized account.
3. Request `/version` and confirm the version and the integration modes.
4. Upload a sample form and confirm the extracted text appears.

`/version` reports the mode of each integration. Confirm each one reads what you expect
before you dispatch anything real.

> **Confirm the code, not the label.** The version string in the footer is passed in as a
> build argument, and the build context is snapshotted later in the script than the
> argument is read. A footer can therefore report the right version over the wrong code.
> When it matters, grep the served page for a string that only the new build contains, or
> read the module out of the image by digest. "The version matches" is not evidence.

---

## 9. Add dispatchers

To add a dispatcher, do both of these:

1. Add the email address to the `dispatch-authorized-emails` secret as a new version.
2. Add the same address as an OAuth Test User, if your consent screen is in Testing mode.

Cloud Run reads a secret value when a container starts. Existing containers keep the old
allowlist until they cycle. Deploy a new revision when a dispatcher must have access
immediately.

---

## 10. Adapt it to your form

The pipeline is general. The form is not. An agency-neutral call-out form ships in
`forms/` — flat and fillable — as a starting point. See [`docs/FORMS.md`](FORMS.md) for
what it contains, what you must change for your agency, and what you must not change.

1. Rewrite the prompt in `backend/gemini.py` to match your form's fields and layout.
2. Update the staging tiers in `backend/main.py` if your team prefers different venues.
3. Replace or remove the lost-person-behavior tables if your team uses different statistics.
4. Add your local normalization rules for handwriting your form reliably produces.

> **Route fillable PDFs away from the model.** If your form exists as a fillable PDF, read
> the AcroForm fields directly — this codebase does, in `pdf_extract.py`. Deterministic
> extraction is effectively perfect; multimodal OCR of a handwritten checkbox grid is not,
> and no amount of prompt engineering closes that gap. The highest-leverage change most
> teams can make is to the form, not to the prompt.

---

## Troubleshooting

**If sign-in fails with `origin_mismatch`:** the OAuth client ID built into the image does
not match the service URL. Confirm the authorized JavaScript origins, then rebuild.

**If a dispatcher cannot sign in but is on the allowlist:** add them as an OAuth Test User,
or deploy a new revision so the container reloads the secret.

**If an API reports that it "has not been used in project ... before":** the API is not
enabled, or enablement has not propagated. Wait one minute and repeat the step.

**If an environment variable change has no effect:** run `terraform apply`. The build
scripts do not apply Terraform changes.

**If staging recommendations are empty:** confirm the POI provider key is set. Confirm the
address geocoded to the region you expect.
